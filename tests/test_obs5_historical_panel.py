from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

from tests.test_m4b_history_replay import SCHEMA_PATH, _seed_history


def _write_transcript(
    runs_root: Path, research_id: str, goal_id: str, chapter: str, engine: str,
) -> None:
    path = runs_root / research_id / "goals" / goal_id / f"{chapter}.transcript.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "ts": 1.0,
        "seq": 1,
        "engine": engine,
        "agent": "reliability-auditor",
        "event": {"type": "assistant", "text": "已完成一批审计"},
    }, ensure_ascii=False) + "\n", encoding="utf-8")


def _snapshot(application, research_id: str) -> dict:
    async def exercise() -> dict:
        async with application.router.lifespan_context(application):
            transport = httpx.ASGITransport(app=application)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                response = await client.get(f"/api/researches/{research_id}")
                assert response.status_code == 200, response.text
                return response.json()["data"]

    return asyncio.run(exercise())


def test_历史快照列出计划外两类评级章且跨_goal_不撞键(tmp_path: Path) -> None:
    from app.api.main import create_app

    database, research_id, _ = _seed_history(tmp_path)
    runs_root = tmp_path / "runs"
    _write_transcript(runs_root, research_id, "goal-1", "firsthand-audit", "Claude")
    _write_transcript(runs_root, research_id, "goal-2", "firsthand-audit", "Claude")
    _write_transcript(runs_root, research_id, "goal-2", "reliability-backfill", "Codex")
    application = create_app(
        database, SCHEMA_PATH, runs_root=runs_root, engine_probe=lambda: {},
    )

    snapshot = _snapshot(application, research_id)

    assert snapshot["run_panel_sections"] == [
        {"id": "goal-1/firsthand-audit", "goal_id": "goal-1",
         "chapter": "firsthand-audit", "name": "一手性审计", "engine": "Claude",
         "status": "done"},
        {"id": "goal-2/firsthand-audit", "goal_id": "goal-2",
         "chapter": "firsthand-audit", "name": "一手性审计", "engine": "Claude",
         "status": "done"},
        {"id": "goal-2/reliability-backfill", "goal_id": "goal-2",
         "chapter": "reliability-backfill", "name": "可靠度回填", "engine": "Codex",
         "status": "done"},
    ]
    assert sum(len(goal["agents"]) for goal in snapshot["goals"]) == 2


def test_活态快照也列出计划外评级章并保持轮询状态(tmp_path: Path) -> None:
    from app.api.main import create_app

    database, research_id, _ = _seed_history(tmp_path)
    runs_root = tmp_path / "runs"
    _write_transcript(runs_root, research_id, "goal-1", "firsthand-audit", "Claude")
    application = create_app(
        database, SCHEMA_PATH, runs_root=runs_root, engine_probe=lambda: {},
    )
    application.state.researches[research_id] = {
        "research_id": research_id,
        "status": "running",
        "goals": [],
        "usage": {},
    }

    snapshot = _snapshot(application, research_id)

    assert snapshot["run_panel_sections"] == [{
        "id": "goal-1/firsthand-audit",
        "goal_id": "goal-1",
        "chapter": "firsthand-audit",
        "name": "一手性审计",
        "engine": "Claude",
        "status": "running",
    }]


def test_面板按独立_section_取流且保留计划外章的心跳(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1] / "web" / "src"
    panel = (root / "RunPanel.tsx").read_text(encoding="utf-8")
    types = (root / "types.ts").read_text(encoding="utf-8")
    stream = (root / "useResearchStream.ts").read_text(encoding="utf-8")

    assert "run_panel_sections?: RunPanelSection[]" in types
    assert "snapshot.run_panel_sections" in panel
    assert "tab.section" in panel
    assert "sectionKey" in stream
