from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
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


def _one_chapter_plan(*, fmt: str, validators: list[str]):
    """单 goal / 单章的最小计划：章终态直接落章节账本，便于断言成品而不是原料。"""
    from app.plan.model import Plan

    source = make_plan_dict()
    source["research_id"] = "r-ledger"
    source["baseline"] = None
    source["goals"] = source["goals"][:1]
    source["goals"][0]["depends_on"] = []
    agent = source["goals"][0]["agents"][0]
    agent["agent_id"] = "consistency-check"
    suffix = "json" if fmt == "json" else "md"
    path = f"goals/goal-1/consistency-check.{suffix}"
    agent["output"] = {
        "format": fmt, "shape": "array" if fmt == "json" else "object",
        "path": path, "validators": list(validators),
    }
    agent["chapter"]["chapter_id"] = "ch-3"
    agent["chapter"]["closing"]["output"] = {"path": path}
    source["goals"][0]["deliverable"]["format"] = fmt
    source["goals"][0]["deliverable"]["path"] = path
    return Plan.from_dict(source)


def _run_one_chapter(tmp_path: Path, plan, behavior):
    """用真实 Scheduler + RuntimeCoordinator._run_task 跑一章，返回 (账本行, coordinator)。"""
    from app.orchestrator.runtime import RuntimeCoordinator
    from app.orchestrator.scheduler import Scheduler

    store = _store(tmp_path)

    async def publish(research_id, payload):
        return None

    coordinator = RuntimeCoordinator(
        store=store,
        event_buffer=SimpleNamespace(publish=publish),
        researches={},
        cards={},
        runs_root=tmp_path / "runs",
        auto_confirm=True,
        routing_utc_clock=lambda: datetime(2026, 8, 22, tzinfo=timezone.utc),
    )
    coordinator.researches[plan.research_id] = coordinator._state_from_plan(plan)
    coordinator._adapters[plan.research_id] = SimpleNamespace(run=behavior)

    scheduler = Scheduler(
        plan,
        lambda agent, context: coordinator._run_task(plan, agent, context),
        lambda event: None,
        lambda: datetime(2026, 8, 22, tzinfo=timezone.utc),
        lambda delay, callback: None,
        chapter_ledger=store,
    )
    asyncio.run(scheduler.start())
    row = next(
        item for item in store.list_chapters(plan.research_id)
        if item["chapter_id"] == "ch-3"
    )
    return row, coordinator


def test_partial_合法产物视为章_done_并保留_unmet(tmp_path: Path):
    """断成品：章终态必须是 done，unmet 必须被 _record_unmet() 落盘。

    只断 EngineRunResult.succeeded 是断原料 —— 6b 整跑里 succeeded 为 True
    的章仍被 runtime 的 reason 短路判成 missing（缺陷 A）。
    """
    from app.adapters import validation
    from app.adapters.contracts import EngineRunResult, OwliResult

    plan = _one_chapter_plan(fmt="markdown", validators=["file_exists", "sections_exist:结论"])

    async def behavior(task, ctx, on_event=None):
        task.output_path.parent.mkdir(parents=True, exist_ok=True)
        task.output_path.write_text(
            "# 一致性检查\n\n## 结论\n\n- 仅能基于 DC1 单边对齐 [S01]\n",
            encoding="utf-8",
        )
        return EngineRunResult(
            conclusion=OwliResult(
                "partial", str(task.output_path), "部分完成", [], ["缺少 HN 样本"], [],
                "empty_result",
            ),
            conclusion_error=None,
            validation=validation.validate(ctx, list(task.validators)),
            events=[],
            permission_denials=[],
        )

    row, coordinator = _run_one_chapter(tmp_path, plan, behavior)

    assert row["status"] == "done" and row["reason"] is None
    unmet_file = (
        tmp_path / "runs" / "r-ledger" / "goals" / "goal-1"
        / ".owli-unmet-consistency-check.json"
    )
    assert unmet_file.is_file()
    payload = json.loads(unmet_file.read_text(encoding="utf-8"))
    assert payload["goal_id"] == "goal-1"
    assert payload["chapter_id"] == "ch-3"
    assert payload["unmet"] == ["缺少 HN 样本"]
    assert payload["reason"] == "empty_result"
    assert coordinator._unmet_items("r-ledger") == [payload]


def test_产物为空数组的_empty_result_仍判_missing_不记_unmet(tmp_path: Path):
    """硬约束 4a 不被放宽：产物真空时 succeeded 为 False，仍走 reason 短路。"""
    from app.adapters import validation
    from app.adapters.contracts import EngineRunResult, OwliResult

    plan = _one_chapter_plan(
        fmt="json", validators=["file_exists", "json_array_min_items:1"],
    )

    async def behavior(task, ctx, on_event=None):
        task.output_path.parent.mkdir(parents=True, exist_ok=True)
        task.output_path.write_text("[]", encoding="utf-8")
        empty_ctx = validation.Ctx(**{**vars(ctx), "missing_reason": "empty_result"})
        return EngineRunResult(
            conclusion=OwliResult(
                "partial", str(task.output_path), "零命中", [], [], [], "empty_result",
            ),
            conclusion_error=None,
            validation=validation.validate(empty_ctx, list(task.validators)),
            events=[],
            permission_denials=[],
        )

    row, coordinator = _run_one_chapter(tmp_path, plan, behavior)

    assert row["status"] == "missing" and row["reason"] == "empty_result"
    assert row["actual_count"] == 0
    assert coordinator._unmet_items("r-ledger") == []


def _partial_json_behavior(items: list[dict[str, object]], reason: str):
    """partial + unmet 非空 + validators 全 PASS ⇒ succeeded=True（真实引擎的 X 源形态）。"""
    from app.adapters import validation
    from app.adapters.contracts import EngineRunResult, OwliResult

    async def behavior(task, ctx, on_event=None):
        task.output_path.parent.mkdir(parents=True, exist_ok=True)
        task.output_path.write_text(
            json.dumps(items, ensure_ascii=False), encoding="utf-8",
        )
        run_ctx = validation.Ctx(**{**vars(ctx), "missing_reason": reason})
        result = EngineRunResult(
            conclusion=OwliResult(
                "partial", str(task.output_path), "X 工具不可用", [],
                ["X 平台样本缺失"], [], reason,
            ),
            conclusion_error=None,
            validation=validation.validate(run_ctx, list(task.validators)),
            events=[],
            permission_denials=[],
        )
        assert result.succeeded, "用例前提：这一支必须 succeeded=True"
        return result

    return behavior


def test_空数组的_partial_即使_succeeded_也判_missing(tmp_path: Path):
    """D-005 回归：succeeded=True 不能替 4a 把关，空产物必须落 missing/<reason>。"""
    plan = _one_chapter_plan(
        fmt="json", validators=["file_exists", "json_array_min_items:1"],
    )
    row, coordinator = _run_one_chapter(
        tmp_path, plan, _partial_json_behavior([], "tool_unavailable"),
    )

    assert row["status"] == "missing" and row["reason"] == "tool_unavailable"
    assert row["actual_count"] == 0
    assert row["attempts"] == 1  # 不烧重试
    assert coordinator._unmet_items("r-ledger") == []  # 空产物不记 unmet


def test_非空数组的_partial_仍判_done_并记_unmet(tmp_path: Path):
    """对照组：同构造但产物非空 ⇒ 保持 D-001 的 A 修复语义 done + _record_unmet()。"""
    plan = _one_chapter_plan(
        fmt="json", validators=["file_exists", "json_array_min_items:1"],
    )
    row, coordinator = _run_one_chapter(
        tmp_path, plan,
        _partial_json_behavior(
            [{"title": "讯飞输入法方言支持", "url": "https://example.com/a"}],
            "tool_unavailable",
        ),
    )

    assert row["status"] == "done" and row["reason"] is None
    assert row["actual_count"] == 1
    items = coordinator._unmet_items("r-ledger")
    assert [item["unmet"] for item in items] == [["X 平台样本缺失"]]


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
    current = [datetime(2026, 8, 22, tzinfo=timezone.utc)]

    def timer(delay, callback):
        if delay <= 15:
            current[0] += timedelta(seconds=delay)
            callback()
        return object()

    async def run_task(agent, context):
        calls.append(context.attempt)
        return TaskRunResult(
            False, context.engine, chapter_status="deferred",
            reason="quota_exhausted", actual_count=0,
        )

    scheduler = Scheduler(
        plan, run_task, lambda event: None,
        lambda: current[0], timer, chapter_ledger=store,
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
