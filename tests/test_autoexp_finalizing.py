"""§AUTO-EXP 货 4：收尾期对外是「收尾中」运行态，completed 只在报告落盘（finish_report）后。

库侧不动：收尾全程 reports.status 保持 running，进程重启后回落库态（判据 5 边界）。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from tests.test_m3h_finalize import _plan, _write
from tests.test_m3h_ledger import _store


def test_收尾中运行态_completed只在报告落盘后(tmp_path: Path, monkeypatch) -> None:
    from app.orchestrator import runtime as runtime_module
    from app.orchestrator.runtime import RuntimeCoordinator

    plan = _plan(report_format="markdown")
    artifact = tmp_path / "runs" / "r-ledger" / "goals" / "goal-3" / "report.md"
    _write(artifact, "# 结论\n\n- 收尾期语义。\n\n# 信息源\n\n- 无。\n")
    store = _store(tmp_path)
    events: list[dict] = []

    async def publish(research_id, payload):
        events.append(payload)

    monkeypatch.setattr(runtime_module, "load_plan", lambda store_, rid: plan)
    coordinator = RuntimeCoordinator(
        store=store, event_buffer=SimpleNamespace(publish=publish), researches={}, cards={},
        runs_root=tmp_path / "runs", auto_confirm=True,
        routing_utc_clock=lambda: datetime(2026, 8, 31, tzinfo=timezone.utc),
    )
    coordinator.researches[plan.research_id] = coordinator._state_from_plan(plan)
    coordinator._schedulers[plan.research_id] = SimpleNamespace(
        status="completed",
        goal_statuses={"goal-1": "done", "goal-2": "done", "goal-3": "done"},
    )
    gate, entered = asyncio.Event(), asyncio.Event()

    async def slow_backfill(research_id):  # 评级回填是收尾主要耗时段（X-1 实测分钟级）
        entered.set()
        await gate.wait()

    monkeypatch.setattr(coordinator, "_backfill_ratings_on_finalize", slow_backfill)

    async def scenario() -> None:
        task = asyncio.create_task(coordinator._finalize_if_terminal(plan.research_id))
        await asyncio.wait_for(entered.wait(), timeout=2)
        # 收尾中途：API 侧「收尾中」，且调度器的 completed 不准覆盖它。
        state = coordinator.sync_state_with_scheduler(plan.research_id)
        assert state["status"] == "finalizing" and state["status_label"] == "收尾中"
        assert state["actions"] == []
        row = store.get_report("r-ledger")
        assert row["status"] == "running" and not row["report_path"]  # 库态=重启回落态
        gate.set()
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(scenario())
    row = store.get_report("r-ledger")
    assert row["status"] == "completed" and row["report_path"]
    assert coordinator.researches["r-ledger"]["status"] == "completed"
    updates = [e["data"]["status"] for e in events if e.get("type") == "research_update"]
    assert updates == ["finalizing", "completed"]  # 事件序：先收尾中、后完成


def test_无原因取消_研究判failed_且不自动导出(tmp_path: Path, monkeypatch) -> None:
    """§AUTO-EXP 货 5（判据 6）：cancelled_without_reason=True 时报告在不在都判 failed，
    summary_line 带原因；failed 研究不触发自动导出。"""
    from app.orchestrator import runtime as runtime_module
    from app.orchestrator.runtime import RuntimeCoordinator

    plan = _plan(report_format="markdown")
    _write(tmp_path / "runs" / "r-ledger" / "goals" / "goal-3" / "report.md",
           "# 结论\n\n- 取消前已成稿。\n\n# 信息源\n\n- 无。\n")
    store = _store(tmp_path)
    events: list[dict] = []

    async def publish(research_id, payload):
        events.append(payload)

    monkeypatch.setattr(runtime_module, "load_plan", lambda store_, rid: plan)
    coordinator = RuntimeCoordinator(
        store=store, event_buffer=SimpleNamespace(publish=publish), researches={}, cards={},
        runs_root=tmp_path / "runs", auto_confirm=True,
        routing_utc_clock=lambda: datetime(2026, 8, 31, tzinfo=timezone.utc),
    )
    coordinator.researches["r-ledger"] = coordinator._state_from_plan(plan)
    coordinator._schedulers["r-ledger"] = SimpleNamespace(
        status="completed", cancelled_without_reason=True,
        goal_statuses={"goal-1": "failed", "goal-2": "done", "goal-3": "done"},
    )
    asyncio.run(coordinator._finalize_if_terminal("r-ledger"))

    row = store.get_report("r-ledger")
    assert row["status"] == "failed"
    assert "agent_run_cancelled" in row["summary_line"]
    assert coordinator.researches["r-ledger"]["status"] == "failed"
    assert coordinator.researches["r-ledger"]["status_label"] == "执行失败"
    types = [e.get("type") for e in events]
    assert "export_done" not in types and "feishu_sync_started" not in types
