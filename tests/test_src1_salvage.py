"""§SRC-1 货 4：章超时/失败不再把已落盘的产物整章作废。"""

from __future__ import annotations

import pathlib


def _salvage_case(tmp_path, status: str, reason: str | None):
    """建一个「产物已落盘、章却是 status」的现场，跑一次投影。"""

    import asyncio
    import json
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from app.orchestrator.runtime import RuntimeCoordinator
    from app.store.dao import Store

    import sqlite3

    tmp_path.mkdir(parents=True, exist_ok=True)
    database_path = tmp_path / "owli.db"
    schema = pathlib.Path(__file__).resolve().parents[1] / "app/store/schema.sql"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(schema.read_text(encoding="utf-8"))
    store = Store(database_path)
    store.create_report(
        id="r-src1", title="SRC-1 货 4", research_question="超时章的产物要不要捡",
        created_at="2026-08-28T00:00:00+00:00",
    )
    runs_root = tmp_path / "runs"
    artifact = runs_root / "r-src1/goals/goal-1/data-collection.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text(json.dumps([
        {
            "platform": "web_search",
            "permalink": "https://help.aliyun.com/zh/tingwu/what-is-tingwu",
            "fetched_at": "2026-08-28T05:33:00+00:00",
            "title": "通义听悟是什么", "summary": "官方帮助页摘要",
        },
        {
            "platform": "web_search",
            "permalink": "https://www.iflyrec.com/help",
            "fetched_at": "2026-08-28T05:33:00+00:00",
            "title": "讯飞听见帮助中心", "summary": "官方帮助页摘要",
        },
    ], ensure_ascii=False), encoding="utf-8")
    store.ensure_chapters(
        "r-src1", [{"goal_id": "goal-1", "chapter_id": "ch-1"}],
        updated_at="2026-08-28T00:00:00Z",
    )
    store.finish_chapter(
        "r-src1", "goal-1", "ch-1", status=status, reason=reason,
        actual_output_path=str(artifact), actual_count=2,
        updated_at="2026-08-28T00:00:01Z",
    )
    published: list[dict] = []

    class _Buffer:
        async def publish(self, research_id, payload):
            published.append(dict(payload))

    runtime = RuntimeCoordinator(
        store=store, event_buffer=_Buffer(), researches={}, cards={},
        adapter_factory=lambda: object(), runs_root=runs_root,
        routing_utc_clock=lambda: datetime.now(timezone.utc),
    )
    goal = SimpleNamespace(
        goal_id="goal-1",
        agents=[SimpleNamespace(
            agent_id="data-collection",
            chapter={"chapter_id": "ch-1"},
            output={"format": "json", "path": "goals/goal-1/data-collection.json"},
            capability={"sources": ["web_search"]},
        )],
    )
    import asyncio as _asyncio
    _asyncio.run(runtime._persist_goal_evidence(
        SimpleNamespace(research_id="r-src1"), goal,
    ))
    return published, store.list_evidence("r-src1")


def test_超时章的已落盘产物被捡回入库并逐条留痕(tmp_path) -> None:
    """诊断根因：第 6 轮 goal-1 四章全 timeout，盘上 20 条证据一条没进库。"""

    published, rows = _salvage_case(tmp_path, "missing", "timeout")

    assert len(rows) == 2
    for row in rows:
        assert row["extra"]["from_incomplete_chapter"] is True
        assert row["extra"]["incomplete_chapter_id"] == "ch-1"
        assert row["extra"]["incomplete_chapter_status"] == "missing"
        assert row["extra"]["incomplete_chapter_reason"] == "timeout"


def test_捡回来这件事不许静默发生(tmp_path) -> None:
    published, _ = _salvage_case(tmp_path, "missing", "timeout")

    events = [
        event for event in published
        if event["type"] == "evidence_salvaged_from_incomplete_chapter"
    ]
    assert len(events) == 1
    data = events[0]["data"]
    assert data["goal_id"] == "goal-1"
    assert data["count"] == 2
    assert data["chapters"] == [
        {"chapter_id": "ch-1", "status": "missing", "reason": "timeout", "count": "2"}
    ]


def test_deferred章同样捡_done章不打留痕(tmp_path) -> None:
    _, deferred_rows = _salvage_case(tmp_path / "a", "deferred", "tool_unavailable")
    assert len(deferred_rows) == 2
    assert all(row["extra"]["from_incomplete_chapter"] for row in deferred_rows)

    published, done_rows = _salvage_case(tmp_path / "b", "done", None)
    assert len(done_rows) == 2
    assert all("from_incomplete_chapter" not in row["extra"] for row in done_rows)
    assert not [
        event for event in published
        if event["type"] == "evidence_salvaged_from_incomplete_chapter"
    ]
