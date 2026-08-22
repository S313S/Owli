from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.plan_factory import make_plan_dict
from tests.test_m3h_ledger import _store


def test_空数组只有带结构化_reason_才绕过_min_items(tmp_path: Path):
    from app.adapters import validation

    output = tmp_path / "runs" / "r" / "goals" / "goal-1" / "empty.json"
    output.parent.mkdir(parents=True)
    output.write_text("[]", encoding="utf-8")

    def ctx(reason):
        return validation.Ctx(
            output_path=output, output_format="json", research_id="r",
            goal_id="goal-1", agent_id="collector",
            read_text=lambda: output.read_text(encoding="utf-8"),
            read_json=lambda: json.loads(output.read_text(encoding="utf-8")),
            store=None, source_domains=frozenset(), runs_root=tmp_path / "runs",
            missing_reason=reason,
        )

    assert validation.validate(
        ctx("empty_result"), ["file_exists", "json_array_min_items:1"]
    ).verdict is validation.Verdict.PASS
    assert validation.validate(
        ctx(None), ["file_exists", "json_array_min_items:1"]
    ).verdict is validation.Verdict.FAIL


def test_执行失败回灌原样保留_unmet_与_capability_denials():
    from app.orchestrator.scheduler import _failure_feedback

    conclusion = SimpleNamespace(
        status="blocked", unmet=["缺少可核验原文"],
        capability_denials=["source.web_search 不可用"],
    )
    feedback = _failure_feedback(SimpleNamespace(
        engine_error=None, conclusion_error=None, conclusion=conclusion,
        validation=SimpleNamespace(results=[]),
    ))
    assert '["缺少可核验原文"]' in feedback
    assert '["source.web_search 不可用"]' in feedback


@pytest.mark.parametrize("reason", ["empty_result", "tool_unavailable"])
def test_空结果与工具不可用立即_missing_且不烧重试(tmp_path: Path, reason: str):
    from app.orchestrator.scheduler import Scheduler, TaskRunResult
    from app.plan.model import Plan

    store = _store(tmp_path)
    source = make_plan_dict()
    source["research_id"] = "r-ledger"
    source["baseline"] = None
    source["goals"] = source["goals"][:1]
    source["goals"][0]["retry_policy"].update(
        max_attempts_per_round=3, max_rounds=2, ask_engine_switch_at=3,
    )
    plan = Plan.from_dict(source)
    calls = []
    events = []

    async def run_task(agent, context):
        calls.append(context.attempt)
        return TaskRunResult(
            False, context.engine, chapter_status="missing", reason=reason,
            actual_count=0,
        )

    async def scenario():
        scheduler = Scheduler(
            plan, run_task, events.append,
            lambda: datetime(2026, 8, 22, tzinfo=timezone.utc),
            lambda delay, callback: None, chapter_ledger=store,
        )
        await scheduler.start()
        return scheduler

    scheduler = asyncio.run(scenario())
    row = store.list_chapters("r-ledger")[0]
    assert calls == [1]
    assert row["status"] == "missing" and row["reason"] == reason
    assert scheduler.goal_statuses["goal-1"] == "awaiting_intervention"


def test_配额章_deferred_后仅补采一轮_仍不成则_missing(tmp_path: Path):
    from app.orchestrator.scheduler import Scheduler, TaskRunResult
    from app.plan.model import Plan

    store = _store(tmp_path)
    source = make_plan_dict()
    source["research_id"] = "r-ledger"
    source["baseline"] = None
    source["goals"] = source["goals"][:1]
    plan = Plan.from_dict(source)
    calls = []

    async def run_task(agent, context):
        calls.append(context.attempt)
        return TaskRunResult(
            False, context.engine, chapter_status="deferred",
            reason="quota_exhausted", actual_count=0,
        )

    scheduler = Scheduler(
        plan, run_task, lambda event: None,
        lambda: datetime(2026, 8, 22, tzinfo=timezone.utc),
        lambda delay, callback: None, chapter_ledger=store,
    )
    asyncio.run(scheduler.start())
    row = store.list_chapters("r-ledger")[0]
    assert calls == [1, 2]
    assert row["status"] == "missing"
    assert row["reason"] == "quota_exhausted"
    assert row["attempts"] == 2


def test_运行期按结构化_429_event_把源级配额判为_deferred(tmp_path: Path):
    from app.adapters.events import ItemKind, NormalizedEvent
    from app.orchestrator.runtime import RuntimeCoordinator
    from app.plan.model import Plan

    source = make_plan_dict()
    source["research_id"] = "r-runtime-429"
    source["goals"] = source["goals"][:1]
    source["baseline"] = None
    plan = Plan.from_dict(source)
    agent = plan.goals[0].agents[0]

    class Store:
        def list_chapters(self, research_id):
            return []

    class Events:
        async def publish(self, research_id, payload):
            return None

    class Adapter:
        async def run(self, task, ctx, on_event=None):
            await on_event(NormalizedEvent(
                engine="source.product_hunt", thread_id=None, turn_id=None,
                item_kind=ItemKind.ERROR, text="quota", is_error=True,
                raw={"http_status": 429}, route_state="BACKOFF",
                cause="rate_limit",
            ))
            return SimpleNamespace(
                succeeded=False, conclusion=None, events=[], engine_error=None,
                conclusion_error=None,
                validation=SimpleNamespace(results=[]),
            )

    coordinator = RuntimeCoordinator(
        store=Store(), event_buffer=Events(), researches={}, cards={},
        adapter_factory=lambda: Adapter(), runs_root=tmp_path / "runs",
        routing_utc_clock=lambda: datetime(2026, 8, 22, tzinfo=timezone.utc),
    )
    coordinator._adapters[plan.research_id] = Adapter()

    async def sink(event):
        return None

    result = asyncio.run(coordinator._run_task(
        plan, agent,
        SimpleNamespace(
            goal_id="goal-1", attempt=1, engine="codex",
            failure_feedback=None, on_event=sink,
        ),
    ))
    assert result.chapter_status == "deferred"
    assert result.reason == "quota_exhausted"
