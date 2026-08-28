"""§X-1 货 2：136s 门槛挡住重试时事件可见，与真墙钟超时分得开（reason 闭集零改动）。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from tests.test_m3h_fix7 import _run_budget


def test_剩余节墙钟不足136秒时事件带resume_floor且真实原因保留(tmp_path: Path) -> None:
    from app.orchestrator.sectioning import SECTION_RESUME_COST_FLOOR_SECONDS

    run = _run_budget(tmp_path, budget_seconds=330, step=200)

    assert run.rows["ch-1/sec-2"]["reason"] == "timeout"  # 闭集口径不变
    errors = [e["data"] for e in run.events if e["type"] == "section_error"]
    assert len(errors) == 1
    assert errors[0]["timeout_kind"] == "resume_floor"
    assert errors[0]["original_reason"] not in {None, "timeout"}
    skipped = [e["data"] for e in run.events if e["type"] == "section_retry_skipped"]
    assert len(skipped) == 1
    assert skipped[0]["chapter_id"] == "ch-1/sec-2" and skipped[0]["attempt"] == 1
    assert skipped[0]["original_reason"] == errors[0]["original_reason"]
    assert skipped[0]["resume_floor_seconds"] == SECTION_RESUME_COST_FLOOR_SECONDS == 136.0
    assert skipped[0]["remaining_seconds"] - skipped[0]["retry_delay"] < 136.0
    assert not [e for e in run.events if e["type"] == "section_retry"]


def test_非断连的引擎超时标为engine_timeout且不发skipped(tmp_path: Path) -> None:
    run = _run_budget(
        tmp_path, budget_seconds=100_000, step=300,
        engine_error="Claude 任务超时（300 秒），已终止并要求整任务重跑",
    )
    errors = [e["data"] for e in run.events if e["type"] == "section_error"]
    assert errors[0]["timeout_kind"] == "engine_timeout"
    assert errors[0]["original_reason"] == "timeout"
    assert not [e for e in run.events if e["type"] == "section_retry_skipped"]


def test_真墙钟到点事件标为wall_clock(tmp_path: Path) -> None:
    from app.orchestrator.sectioning import _finish_section_timeout

    finished: list[dict] = []
    events: list[dict] = []
    store = SimpleNamespace(
        finish_chapter=lambda rid, gid, cid, **kw: finished.append({"chapter_id": cid, **kw}),
    )
    section = {"section_id": "ch-1/sec-2", "title": "第二节", "goal_id": "goal-3"}
    section_path = tmp_path / "goals" / "goal-3" / "ch-1" / "sec-2.md"
    section_path.parent.mkdir(parents=True)
    asyncio.run(_finish_section_timeout(
        plan=SimpleNamespace(research_id="r-ledger"),
        context=SimpleNamespace(goal_id="goal-3"),
        section=section, section_path=section_path, store=store,
        now_iso=lambda: "2026-08-29T00:00:00Z", on_event=events.append,
    ))

    assert finished[0]["status"] == "missing" and finished[0]["reason"] == "timeout"
    errors = [e["data"] for e in events if e["type"] == "section_error"]
    assert errors[0]["timeout_kind"] == "wall_clock"
    assert errors[0]["original_reason"] == "timeout"
    assert not [e for e in events if e["type"] == "section_retry_skipped"]
