from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import httpx


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "app" / "store" / "schema.sql"


def _seed_history(tmp_path: Path, *, status: str = "completed") -> tuple[Path, str, Path]:
    from app.store.dao import Store

    database = tmp_path / "owli.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))

    research_id = f"r-history-{status}"
    report_path = tmp_path / "runs" / research_id / "goals" / "goal-2" / "report.md"
    report_path.parent.mkdir(parents=True)
    report_path.write_text("# 历史报告\n\n这是已落盘的报告正文。", encoding="utf-8")
    plan_snapshot = {
        "research_id": research_id,
        "goals": [
            {
                "goal_id": "goal-1",
                "title": "资料采集",
                "agents": [{
                    "agent_id": "data-collection",
                    "display_name": "资料采集",
                    "engine": "codex",
                    "task": "采集可复核资料",
                    "chapter": {"chapter_id": "chapter-1"},
                }],
            },
            {
                "goal_id": "goal-2",
                "title": "报告成稿",
                "agents": [{
                    "agent_id": "report-writing",
                    "display_name": "报告撰写",
                    "engine": "claude",
                    "task": "按证据撰写报告",
                    "chapter": {"chapter_id": "chapter-2"},
                }],
            },
        ],
    }
    store = Store(database)
    store.create_report(
        id=research_id,
        title="历史调研",
        research_question="重启后能否回放？",
        created_at="2026-08-24T00:00:00Z",
        completed_at="2026-08-24T01:00:00Z",
        status=status,
        summary="历史执行摘要",
        summary_line="历史一句话摘要",
        plan_snapshot=plan_snapshot,
        report_path=str(report_path),
    )
    store.ensure_chapters(
        research_id,
        [
            {"goal_id": "goal-1", "chapter_id": "chapter-1"},
            {"goal_id": "goal-2", "chapter_id": "chapter-2"},
        ],
        updated_at="2026-08-24T00:10:00Z",
    )
    store.finish_chapter(
        research_id,
        "goal-1",
        "chapter-1",
        status="done",
        reason=None,
        actual_output_path="goals/goal-1/chapter-1.md",
        actual_count=3,
        updated_at="2026-08-24T00:20:00Z",
    )
    store.finish_chapter(
        research_id,
        "goal-2",
        "chapter-2",
        status="missing",
        reason="timeout",
        actual_output_path=None,
        actual_count=None,
        engine_error="章执行超过墙钟",
        conclusion_error=None,
        updated_at="2026-08-24T00:30:00Z",
    )
    return database, research_id, report_path


def test_历史详情从_store_重建只读快照且不写回内存(tmp_path: Path) -> None:
    from app.api.main import create_app

    database, research_id, report_path = _seed_history(tmp_path)
    application = create_app(
        database,
        SCHEMA_PATH,
        runs_root=tmp_path / "runs",
        engine_probe=lambda: {},
    )

    async def exercise() -> httpx.Response:
        async with application.router.lifespan_context(application):
            transport = httpx.ASGITransport(app=application)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.get(f"/api/researches/{research_id}")

    response = asyncio.run(exercise())

    assert response.status_code == 200, response.text
    snapshot = response.json()["data"]
    assert snapshot["snapshot_source"] == "store"
    assert snapshot["status"] == "completed"
    assert snapshot["status_label"] == "已完成"
    assert snapshot["progress"] == {
        "done": 2,
        "total": 2,
        "summary": "历史一句话摘要",
    }
    assert snapshot["report_path"] == str(report_path)
    assert snapshot["summary"] == "历史执行摘要"
    assert snapshot["report_content"].startswith("# 历史报告")
    assert snapshot["actions"] == []
    assert snapshot["cards"] == []
    assert snapshot["events"] == []
    assert [goal["status"] for goal in snapshot["goals"]] == ["done", "failed"]
    assert snapshot["goals"][0]["agents"] == [{
        "id": "data-collection",
        "name": "资料采集",
        "engine": "codex",
        "status": "done",
        "activity": "采集可复核资料",
    }]
    assert snapshot["goals"][1]["agents"] == [{
        "id": "report-writing",
        "name": "报告撰写",
        "engine": "claude",
        "status": "missing",
        "activity": "按证据撰写报告",
    }]
    assert [chapter["status"] for chapter in snapshot["chapters"]] == ["done", "missing"]
    assert snapshot["missing"] == [
        {
            "goal_id": "goal-2",
            "chapter_id": "chapter-2",
            "status": "missing",
            "reason": "timeout",
            "error": "章执行超过墙钟",
        }
    ]
    assert research_id not in application.state.researches


def test_运行中研究仍走原有内存同步路径(tmp_path: Path) -> None:
    from app.api.main import create_app

    application = create_app(tmp_path / "owli.db", SCHEMA_PATH, engine_probe=lambda: {})
    research_id = "r-live"
    expected = {"research_id": research_id, "status": "running", "marker": "runtime"}
    application.state.researches[research_id] = {"research_id": research_id}
    application.state.runtime.sync_state_with_scheduler = lambda value: (
        expected if value == research_id else None
    )

    async def exercise() -> httpx.Response:
        async with application.router.lifespan_context(application):
            transport = httpx.ASGITransport(app=application)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.get(f"/api/researches/{research_id}")

    response = asyncio.run(exercise())

    assert response.status_code == 200
    assert response.json()["data"] == expected


class _ConnectedRequest:
    async def is_disconnected(self) -> bool:
        return False


def test_历史_SSE_只推_store_快照后正常结束且不读事件缓冲(tmp_path: Path) -> None:
    from app.api.events import ResearchEventBuffer
    from app.api.main import create_app

    database, research_id, _ = _seed_history(tmp_path, status="failed")
    buffer = ResearchEventBuffer()

    async def forbidden(*args, **kwargs):
        raise AssertionError("历史 SSE 不得读取内存事件缓冲")

    buffer.replay_after = forbidden  # type: ignore[method-assign]
    buffer.wait_after = forbidden  # type: ignore[method-assign]
    application = create_app(
        database,
        SCHEMA_PATH,
        event_buffer=buffer,
        runs_root=tmp_path / "runs",
        engine_probe=lambda: {},
    )
    route = next(
        route
        for route in application.routes
        if getattr(route, "path", None) == "/api/researches/{research_id}/events"
    )

    async def exercise() -> tuple[str, str]:
        response = await route.endpoint(research_id, _ConnectedRequest(), None, None)
        iterator = response.body_iterator
        connected = await anext(iterator)
        snapshot_event = await anext(iterator)
        try:
            await anext(iterator)
        except StopAsyncIteration:
            pass
        else:
            raise AssertionError("历史 SSE 在 research_snapshot 后没有结束")
        return connected, snapshot_event

    connected, snapshot_event = asyncio.run(exercise())

    assert "event: stream_connected\n" in connected
    assert "event: research_snapshot\n" in snapshot_event
    payload = json.loads(
        next(line[6:] for line in snapshot_event.splitlines() if line.startswith("data: "))
    )
    assert payload["type"] == "research_snapshot"
    assert payload["data"]["snapshot_source"] == "store"
    assert payload["data"]["status"] == "failed"
    assert research_id not in application.state.researches
