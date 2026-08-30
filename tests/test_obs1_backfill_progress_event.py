"""§OBS-1：货 2 回填批次进度事件 reliability_backfill_progress。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace


def _progress_store(tmp_path: Path, count: int):
    from tests.test_m4fork_followup import _database, _evidence

    _, store = _database(tmp_path)
    store.create_report(id="r-obs", title="进度", research_question="回填进度事件",
                        created_at="2026-08-31T00:00:00Z")
    store.upsert_evidence_batch([
        _evidence("r-obs", f"{i:03d}", permalink=f"https://e.example/{i}", extra={})
        for i in range(count)
    ])
    return store


def test_货2_回填每批完成发进度事件且载荷字段齐(tmp_path):
    from app.reliability.backfill import backfill_report
    from tests.test_m4fork_followup import BackfillEngine

    store = _progress_store(tmp_path, 30)
    events: list[dict] = []

    async def on_event(payload):
        events.append(payload)

    result = asyncio.run(backfill_report(
        store, "r-obs", adapter=BackfillEngine(), runs_root=tmp_path / "runs",
        on_event=on_event,
    ))

    progress = [e["data"] for e in events
                if e["type"] == "reliability_backfill_progress"]
    assert [p["batch_number"] for p in progress] == [1, 2]
    assert all(p["batch_total"] == 2 for p in progress)
    assert [p["batch_rows"] for p in progress] == [25, 5]
    assert progress[-1]["rated_total"] == result.rated == 30
    assert progress[-1]["failed_total"] == 0
    assert all(p["batch_seconds"] >= 0 for p in progress)
    assert progress[0]["report_id"] == "r-obs"
    assert progress[0]["goal_id"] == "goal-1"


def test_货2_失败批也发进度事件并计入failed(tmp_path):
    from app.reliability.backfill import backfill_report

    class FailingEngine:
        async def run(self, task, ctx, on_event=None):
            del task, ctx, on_event
            return SimpleNamespace(succeeded=False)

    store = _progress_store(tmp_path, 5)
    events: list[dict] = []

    async def on_event(payload):
        events.append(payload)

    result = asyncio.run(backfill_report(
        store, "r-obs", adapter=FailingEngine(), runs_root=tmp_path / "runs",
        on_event=on_event,
    ))

    progress = [e["data"] for e in events
                if e["type"] == "reliability_backfill_progress"]
    assert len(progress) == 1
    assert progress[0]["failed_total"] == 5 == result.failed
    assert progress[0]["rated_total"] == 0


def test_货2_收尾回填期间事件流有进度且先于done(tmp_path, monkeypatch):
    from tests.test_x1_rating_backfill import LabelAuditor, _finalize

    _, events, _ = _finalize(tmp_path, monkeypatch, adapter=LabelAuditor())

    progress = [i for i, e in enumerate(events)
                if e.get("type") == "reliability_backfill_progress"]
    done = [i for i, e in enumerate(events)
            if e.get("type") == "reliability_backfill_done"]
    assert len(progress) == 1 and len(done) == 1
    assert progress[0] < done[0]
    data = events[progress[0]]["data"]
    assert data["batch_number"] == 1
    assert data["batch_total"] == 1
    assert data["batch_rows"] == 1
