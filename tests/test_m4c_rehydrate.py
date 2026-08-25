from __future__ import annotations

import asyncio
import copy
import sqlite3
from pathlib import Path

import httpx

from tests.plan_factory import make_agent, make_plan_dict


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "app" / "store" / "schema.sql"


def _seed_interrupted_research(tmp_path: Path) -> tuple[Path, dict]:
    from app.store.dao import Store

    database = tmp_path / "owli.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))

    source = make_plan_dict()
    source["research_id"] = "r-rehydrate"
    source["status"] = "approved"
    source["approved_at"] = "2026-08-25T02:00:00+00:00"
    source["goals"] = source["goals"][:1]
    source["baseline"]["goals"] = copy.deepcopy(source["goals"])
    first = source["goals"][0]["agents"][0]
    first["chapter"]["chapter_id"] = "ch-done"
    second = make_agent("agent-resume", "goal-1")
    second["chapter"]["chapter_id"] = "ch-resume"
    second["output"]["path"] = "goals/goal-1/agent-resume.md"
    second["chapter"]["closing"]["output"]["path"] = second["output"]["path"]
    source["goals"][0]["agents"] = [first, second]

    store = Store(database)
    store.create_report(
        id=source["research_id"],
        title=source["title"],
        research_question=source["research_question"],
        created_at=source["created_at"],
        status="running",
        plan_snapshot=source,
        extra={"scale": "fast"},
    )
    store.ensure_chapters(
        source["research_id"],
        [
            {"goal_id": "goal-1", "chapter_id": "ch-done"},
            {"goal_id": "goal-1", "chapter_id": "ch-resume"},
        ],
        updated_at="2026-08-25T02:01:00+00:00",
    )
    store.finish_chapter(
        source["research_id"],
        "goal-1",
        "ch-done",
        status="done",
        reason=None,
        actual_output_path="goals/goal-1/agent-1.md",
        actual_count=1,
        updated_at="2026-08-25T02:02:00+00:00",
    )
    assert store.start_chapter(
        source["research_id"],
        "goal-1",
        "ch-resume",
        engine="claude",
        updated_at="2026-08-25T02:03:00+00:00",
    )
    return database, source


def test_lifespan_重建运行态但不自动开跑_用户_resume_后只续未完成章(
    tmp_path: Path,
) -> None:
    from app.api.main import create_app
    from app.orchestrator.scheduler import TaskRunResult

    database, source = _seed_interrupted_research(tmp_path)
    adapter_instances: list[object] = []

    def adapter_factory() -> object:
        adapter = object()
        adapter_instances.append(adapter)
        return adapter

    application = create_app(
        database,
        SCHEMA_PATH,
        runs_root=tmp_path / "runs",
        adapter_factory=adapter_factory,
        engine_probe=lambda: {},
        auto_confirm=False,
    )
    calls: list[str] = []

    async def run_task(plan, agent, context):
        calls.append(agent.agent_id)
        return TaskRunResult(
            True,
            engine=context.engine,
            actual_output_path=agent.output["path"],
            actual_count=1,
        )

    application.state.runtime._run_task = run_task

    async def exercise() -> tuple[dict, dict, list[dict], int]:
        async with application.router.lifespan_context(application):
            assert calls == []
            transport = httpx.ASGITransport(app=application)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                before = await client.get("/api/researches/r-rehydrate")
                resumed = await client.post(
                    "/api/researches/r-rehydrate/resume",
                    headers={"X-Request-ID": "req-rehydrate-resume"},
                )
                scheduler = application.state.runtime.scheduler_for("r-rehydrate")
                await scheduler.wait_idle()
                chapters = application.state.store.list_chapters("r-rehydrate")
                report = application.state.store.get_report("r-rehydrate")
                return before.json()["data"], resumed.json()["data"], chapters, report["plan_snapshot"]["plan_rev"]

    before, resumed, chapters, plan_rev = asyncio.run(exercise())

    assert len(adapter_instances) == 1
    assert before["status"] == "paused"
    assert "snapshot_source" not in before
    assert [action["id"] for action in before["actions"]] == ["resume", "stop"]
    assert before["progress"] == {
        "done": 0,
        "total": 1,
        "summary": "已从章节账本恢复，等待用户继续",
    }
    assert before["goals"][0]["agents"][0]["status"] == "done"
    assert before["goals"][0]["agents"][1]["status"] == "queued"
    assert resumed["status"] == "running"
    assert calls == ["agent-resume"]
    assert {row["chapter_id"]: row["status"] for row in chapters} == {
        "ch-done": "done",
        "ch-resume": "done",
    }
    assert plan_rev == source["plan_rev"]


def test_lifespan_不把终态研究重建为活研究(tmp_path: Path) -> None:
    from app.api.main import create_app

    database, _ = _seed_interrupted_research(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE reports SET status='completed' WHERE id='r-rehydrate'"
        )
    application = create_app(database, SCHEMA_PATH, engine_probe=lambda: {})

    async def exercise() -> dict:
        async with application.router.lifespan_context(application):
            transport = httpx.ASGITransport(app=application)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/api/researches/r-rehydrate")
                assert "r-rehydrate" not in application.state.researches
                return response.json()["data"]

    snapshot = asyncio.run(exercise())
    assert snapshot["snapshot_source"] == "store"
    assert snapshot["actions"] == []
