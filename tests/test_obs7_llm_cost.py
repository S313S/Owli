"""§OBS-7 货 1：模型费「标价折算」与按实际引擎分桶。"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.plan_factory import make_plan_dict

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "app" / "store" / "schema.sql"

CODEX_USAGE = {
    "input_tokens": 1_000_000,
    "cached_input_tokens": 800_000,
    "cache_creation_input_tokens": 0,
    "cache_write_input_tokens": 0,
    "output_tokens": 10_000,
    "reasoning_output_tokens": 2_000,
    "cost_usd": None,
}
CLAUDE_USAGE = {
    "input_tokens": 1_000,
    "cached_input_tokens": 1_000_000,
    "cache_creation_input_tokens": 100_000,
    "cache_write_input_tokens": 0,
    "output_tokens": 20_000,
    "reasoning_output_tokens": 0,
    "cost_usd": None,
}


def _store(tmp_path: Path):
    from app.store.dao import Store

    database = tmp_path / "owli.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(SCHEMA.read_text(encoding="utf-8"))
    store = Store(database)
    store.create_report(id="r-cost", title="费用", research_question="费用观测",
                        created_at="2026-09-13T00:00:00Z")
    return store


def test_Codex_标价折算_缓存含在输入里且推理含在输出里() -> None:
    from app.observability.pricing import estimate_cost_usd

    # (200k 新输入 × 1.25 + 800k 缓存 × 0.125 + 10k 输出 × 10) / 1M
    assert estimate_cost_usd("Codex", CODEX_USAGE) == pytest.approx(0.25 + 0.1 + 0.1)


def test_Claude_标价折算_四项分列计价() -> None:
    from app.observability.pricing import estimate_cost_usd

    # (1k × 5 + 1M 缓存读 × 0.5 + 100k 缓存写 × 10 + 20k 输出 × 25) / 1M
    assert estimate_cost_usd("claude", CLAUDE_USAGE) == pytest.approx(0.005 + 0.5 + 1.0 + 0.5)


def test_不认识的引擎不瞎折算_引擎报了价就原样用() -> None:
    from app.observability.pricing import priced_usage

    usage, source = priced_usage("gemini", CODEX_USAGE)
    assert usage["cost_usd"] is None and source is None
    usage, source = priced_usage("claude", {**CLAUDE_USAGE, "cost_usd": 0.42})
    assert usage["cost_usd"] == 0.42 and source == "reported"
    usage, source = priced_usage("codex", CODEX_USAGE)
    assert source == "estimated" and usage["cost_usd"] == pytest.approx(0.45)


def test_账本分列引擎报价与标价折算_并按实际引擎分桶(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.ensure_chapters("r-cost", [{"goal_id": "goal-1", "chapter_id": "ch-2"}],
                          updated_at="2026-09-13T00:00:01Z")
    store.record_chapter_usage("r-cost", "goal-1", "ch-2", {**CLAUDE_USAGE, "cost_usd": 1.9},
                               engine="claude", cost_source="reported")
    store.record_chapter_usage("r-cost", "goal-1", "ch-2", {**CODEX_USAGE, "cost_usd": 0.45},
                               engine="codex", cost_source="estimated")

    usage = store.list_chapters("r-cost")[0]["extra"]["usage"]
    assert usage["calls"] == 2
    assert usage["costed_calls"] == 1 and usage["cost_usd"] == pytest.approx(1.9)
    assert usage["estimated_calls"] == 1 and usage["estimated_cost_usd"] == pytest.approx(0.45)
    assert usage["by_engine"]["claude"]["costed_calls"] == 1
    assert usage["by_engine"]["codex"]["estimated_calls"] == 1
    assert usage["by_engine"]["codex"]["cost_usd"] is None

    aggregate = store.aggregate_research_usage("r-cost")
    assert aggregate["estimated_cost_usd"] == pytest.approx(0.45)
    assert aggregate["by_engine"]["codex"]["estimated_calls"] == 1
    assert aggregate["by_engine"]["claude"]["cost_usd"] == pytest.approx(1.9)


def test_价格来源闭集_声明折算必须带金额(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.ensure_chapters("r-cost", [{"goal_id": "goal-1", "chapter_id": "ch-2"}],
                          updated_at="2026-09-13T00:00:01Z")
    with pytest.raises(ValueError, match="闭集"):
        store.record_chapter_usage("r-cost", "goal-1", "ch-2", CODEX_USAGE, cost_source="paid")
    with pytest.raises(ValueError, match="必须带 cost_usd"):
        store.record_chapter_usage("r-cost", "goal-1", "ch-2", CODEX_USAGE, cost_source="estimated")


def test_计划给Claude_实际让路给Codex跑_账本记实际引擎且每次调用都有价(tmp_path: Path) -> None:
    from app.adapters.events import ItemKind, NormalizedEvent
    from app.api.events import ResearchEventBuffer
    from app.observability.pricing import estimate_cost_usd
    from app.orchestrator.runtime import RuntimeCoordinator
    from app.orchestrator.scheduler import TaskRunResult
    from app.plan.model import Plan

    store = _store(tmp_path)
    source = make_plan_dict()
    source["research_id"] = "r-cost"
    source["baseline"] = None
    source["goals"] = source["goals"][:1]
    source["goals"][0]["agents"] = source["goals"][0]["agents"][:1]
    plan = Plan.from_dict(source)
    agent = plan.goals[0].agents[0]
    chapter_id = str(agent.chapter["chapter_id"])
    store.ensure_chapters("r-cost", [{"goal_id": "goal-1", "chapter_id": chapter_id}],
                          updated_at="2026-09-13T00:00:01Z")
    store.start_chapter("r-cost", "goal-1", chapter_id, engine="claude",
                        updated_at="2026-09-13T00:00:02Z")

    class Adapter:
        async def run(self, task, ctx, on_event):
            del task, ctx
            for engine, usage in (("Codex", CODEX_USAGE), ("Claude", {**CLAUDE_USAGE, "cost_usd": 1.9})):
                await on_event(NormalizedEvent(
                    engine=engine, thread_id="t", turn_id="u", item_kind=ItemKind.DONE,
                    text="", is_error=False, raw={"type": "turn.completed"}, usage=usage,
                ))
            return TaskRunResult(True, engine="codex")

    states = {"r-cost": {"usage": {}}}
    coordinator = RuntimeCoordinator(
        store=store, event_buffer=ResearchEventBuffer(), researches=states, cards={},
        adapter_factory=Adapter, runs_root=tmp_path / "runs", auto_confirm=False,
        routing_utc_clock=lambda: None,
    )
    coordinator._adapters["r-cost"] = Adapter()

    async def consume(_event):
        return None

    context = SimpleNamespace(goal_id="goal-1", engine="claude", attempt=1, on_event=consume,
                              deadline_at=None, failure_feedback=None)
    asyncio.run(coordinator._run_task(plan, agent, context))

    row = store.list_chapters("r-cost")[0]
    usage = row["extra"]["usage"]
    assert row["engine"] == "claude"                       # 账本的计划引擎列不动
    assert usage["calls"] == usage["costed_calls"] + usage["estimated_calls"] == 2
    assert usage["by_engine"]["codex"]["estimated_cost_usd"] == pytest.approx(
        estimate_cost_usd("codex", CODEX_USAGE))
    assert usage["by_engine"]["claude"]["cost_usd"] == pytest.approx(1.9)
    replay = asyncio.run(coordinator.events.replay_after("r-cost", None))
    first = next(e.payload for e in replay.events if e.payload.get("type") == "normalized_event")
    assert first["data"]["usage"]["cost_usd"] is None      # 事件里保留引擎原话，不改写
