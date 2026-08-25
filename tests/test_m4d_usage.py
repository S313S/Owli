from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import claude_agent_sdk as claude_sdk

from tests.plan_factory import make_plan_dict


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "app" / "store" / "schema.sql"


def _store(tmp_path: Path):
    from app.store.dao import Store

    database = tmp_path / "owli.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(SCHEMA.read_text(encoding="utf-8"))
    store = Store(database)
    store.create_report(
        id="r-usage",
        title="LLM usage",
        research_question="运行期实测用量",
        created_at="2026-08-25T00:00:00Z",
    )
    return store


def test_Claude只在_ResultMessage_抬升聚合_usage避免消息级重复计数() -> None:
    from app.adapters.events import ItemKind, normalize_claude_event

    assistant = claude_sdk.AssistantMessage(
        content=[claude_sdk.TextBlock("中间输出")],
        model="claude-opus-4-7",
        usage={"input_tokens": 3, "output_tokens": 5},
    )
    result = claude_sdk.ResultMessage(
        subtype="success",
        duration_ms=100,
        duration_api_ms=90,
        is_error=False,
        num_turns=2,
        session_id="session-1",
        total_cost_usd=0.09417375,
        usage={
            "input_tokens": 8,
            "cache_creation_input_tokens": 11127,
            "cache_read_input_tokens": 6390,
            "output_tokens": 780,
        },
    )

    assistant_event = normalize_claude_event(assistant, sdk=claude_sdk)[0]
    done_event = normalize_claude_event(result, sdk=claude_sdk)[0]

    assert assistant_event.usage is None
    assert done_event.item_kind is ItemKind.DONE
    assert done_event.usage == {
        "input_tokens": 8,
        "cached_input_tokens": 6390,
        "cache_creation_input_tokens": 11127,
        "cache_write_input_tokens": 0,
        "output_tokens": 780,
        "reasoning_output_tokens": 0,
        "cost_usd": 0.09417375,
    }

    failed = claude_sdk.ResultMessage(
        subtype="error_during_execution",
        duration_ms=100,
        duration_api_ms=90,
        is_error=True,
        num_turns=1,
        session_id="session-2",
        total_cost_usd=0.001975,
        usage={
            "input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "output_tokens": 0,
        },
    )
    failed_event = normalize_claude_event(failed, sdk=claude_sdk)[0]
    assert failed_event.item_kind is ItemKind.ERROR
    assert failed_event.usage is not None
    assert failed_event.usage["cost_usd"] == 0.001975


def test_Codex_turn_completed抬升为同形_usage且成本保持空值() -> None:
    from app.adapters.events import ItemKind, normalize_codex_event

    event = normalize_codex_event({
        "type": "turn.completed",
        "usage": {
            "input_tokens": 72131,
            "cached_input_tokens": 63744,
            "cache_write_input_tokens": 0,
            "output_tokens": 440,
            "reasoning_output_tokens": 59,
        },
    })[0]

    assert event.item_kind is ItemKind.DONE
    assert event.usage == {
        "input_tokens": 72131,
        "cached_input_tokens": 63744,
        "cache_creation_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "output_tokens": 440,
        "reasoning_output_tokens": 59,
        "cost_usd": None,
    }


def test_章账本_usage累加且_research聚合逐字段等于各章之和(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.ensure_chapters(
        "r-usage",
        [
            {"goal_id": "goal-1", "chapter_id": "ch-claude"},
            {"goal_id": "goal-2", "chapter_id": "ch-codex"},
        ],
        updated_at="2026-08-25T00:00:01Z",
    )
    claude = {
        "input_tokens": 8,
        "cached_input_tokens": 6390,
        "cache_creation_input_tokens": 11127,
        "cache_write_input_tokens": 0,
        "output_tokens": 780,
        "reasoning_output_tokens": 0,
        "cost_usd": 0.09417375,
    }
    codex = {
        "input_tokens": 72131,
        "cached_input_tokens": 63744,
        "cache_creation_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "output_tokens": 440,
        "reasoning_output_tokens": 59,
        "cost_usd": None,
    }

    store.record_chapter_usage("r-usage", "goal-1", "ch-claude", claude)
    store.record_chapter_usage("r-usage", "goal-2", "ch-codex", codex)

    chapters = store.list_chapters("r-usage")
    per_chapter = [row["extra"]["usage"] for row in chapters]
    aggregate = store.aggregate_research_usage("r-usage")
    additive = (
        "input_tokens",
        "cached_input_tokens",
        "cache_creation_input_tokens",
        "cache_write_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "cost_usd",
        "calls",
        "costed_calls",
    )
    for key in additive:
        assert aggregate[key] == sum((item[key] or 0) for item in per_chapter)
    assert aggregate == {
        "input_tokens": 72139,
        "cached_input_tokens": 70134,
        "cache_creation_input_tokens": 11127,
        "cache_write_input_tokens": 0,
        "output_tokens": 1220,
        "reasoning_output_tokens": 59,
        "cost_usd": 0.09417375,
        "calls": 2,
        "costed_calls": 1,
    }


def test_Runtime收到归一化usage后立即落当前章并更新工作板聚合(tmp_path: Path) -> None:
    from app.adapters.events import ItemKind, NormalizedEvent
    from app.api.events import ResearchEventBuffer
    from app.orchestrator.runtime import RuntimeCoordinator
    from app.orchestrator.scheduler import TaskRunResult
    from app.plan.model import Plan

    store = _store(tmp_path)
    source = make_plan_dict()
    source["research_id"] = "r-usage"
    source["baseline"] = None
    source["goals"] = source["goals"][:1]
    source["goals"][0]["agents"] = source["goals"][0]["agents"][:1]
    plan = Plan.from_dict(source)
    agent = plan.goals[0].agents[0]
    chapter_id = str(agent.chapter["chapter_id"])
    store.ensure_chapters(
        "r-usage",
        [{"goal_id": "goal-1", "chapter_id": chapter_id}],
        updated_at="2026-08-25T00:00:01Z",
    )
    store.start_chapter(
        "r-usage", "goal-1", chapter_id,
        engine=agent.engine,
        updated_at="2026-08-25T00:00:02Z",
    )
    usage = {
        "input_tokens": 10,
        "cached_input_tokens": 2,
        "cache_creation_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "output_tokens": 4,
        "reasoning_output_tokens": 1,
        "cost_usd": None,
    }

    class Adapter:
        async def run(self, task, ctx, on_event):
            del task, ctx
            await on_event(NormalizedEvent(
                engine="Codex",
                thread_id="thread-1",
                turn_id="turn-1",
                item_kind=ItemKind.DONE,
                text="",
                is_error=False,
                raw={"type": "turn.completed"},
                usage=usage,
            ))
            return TaskRunResult(True, engine="codex")

    states = {"r-usage": {"usage": {}}}
    coordinator = RuntimeCoordinator(
        store=store,
        event_buffer=ResearchEventBuffer(),
        researches=states,
        cards={},
        adapter_factory=Adapter,
        runs_root=tmp_path / "runs",
        auto_confirm=False,
        routing_utc_clock=lambda: None,
    )
    coordinator._adapters["r-usage"] = Adapter()

    async def consume(_event):
        return None

    context = SimpleNamespace(
        goal_id="goal-1",
        engine=agent.engine,
        attempt=1,
        on_event=consume,
        deadline_at=None,
        failure_feedback=None,
    )
    asyncio.run(coordinator._run_task(plan, agent, context))

    row = store.list_chapters("r-usage")[0]
    assert row["extra"]["usage"]["input_tokens"] == 10
    assert row["extra"]["usage"]["calls"] == 1
    assert states["r-usage"]["usage"] == store.aggregate_research_usage("r-usage")

    def reject_usage(*_args, **_kwargs):
        raise RuntimeError("计量存储暂不可用")

    store.record_chapter_usage = reject_usage  # type: ignore[method-assign]
    result = asyncio.run(coordinator._run_task(plan, agent, context))
    replay = asyncio.run(coordinator.events.replay_after("r-usage", None))
    terminal = replay.events[-1].payload
    assert result.succeeded is True
    assert terminal["type"] == "normalized_event"
    assert terminal["data"]["usage"] == usage
    assert terminal["data"]["research_usage"] is None


def test_工作板源码展示research级LLM实测用量() -> None:
    types = (ROOT / "web" / "src" / "types.ts").read_text(encoding="utf-8")
    live = (ROOT / "web" / "src" / "WorkboardPage.tsx").read_text(encoding="utf-8")
    history = (ROOT / "web" / "src" / "HistoricalResearchView.tsx").read_text(
        encoding="utf-8"
    )
    stream = (ROOT / "web" / "src" / "useResearchStream.ts").read_text(
        encoding="utf-8"
    )

    assert "usage: LlmUsage" in types
    assert "LLM 实测用量" in live
    assert "snapshot.usage" in live
    assert "LLM 实测用量" in history
    assert "snapshot.usage" in history
    assert "data.usage ?? current?.usage ?? EMPTY_LLM_USAGE" in stream
