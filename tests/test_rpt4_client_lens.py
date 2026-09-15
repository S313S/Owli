"""§RPT-4 正式稿客户视角修正：五货的用例。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from app.store.dao import Store
from app.store.schema import initialize_database_if_empty

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "app" / "store" / "schema.sql"


# ---------------------------------------------------------------- 货 1 状态如实

def _seed(tmp_path: Path, *, second: str = "missing") -> tuple[Path, str]:
    database = tmp_path / "owli.db"
    initialize_database_if_empty(database, SCHEMA_PATH)
    research_id = "r-rpt4"
    plan = {"goals": [{"goal_id": "goal-1", "title": "采集", "agents": [
        {"agent_id": "xhs-doubao", "chapter": {"chapter_id": "ch-1", "chapter_type": "collection"},
         "capability": {"sources": ["xhs"]}, "entity": "豆包"},
        {"agent_id": "douyin-doubao", "chapter": {"chapter_id": "ch-2", "chapter_type": "collection"},
         "capability": {"sources": ["douyin"]}, "entity": "豆包"},
    ]}]}
    store = Store(database)
    store.create_report(id=research_id, title="国内大家对豆包的看法", research_question="国内大家对豆包的看法",
                        created_at="2026-09-14T00:00:00Z", status="completed", plan_snapshot=plan)
    store.ensure_chapters(research_id, [{"goal_id": "goal-1", "chapter_id": "ch-1"},
                                        {"goal_id": "goal-1", "chapter_id": "ch-2"}],
                          updated_at="2026-09-14T00:00:00Z")
    store.finish_chapter(research_id, "goal-1", "ch-1", status=second,
                         reason="timeout" if second == "missing" else None,
                         actual_output_path=None, actual_count=None, engine_error=None,
                         conclusion_error=None, updated_at="2026-09-14T00:10:00Z")
    store.finish_chapter(research_id, "goal-1", "ch-2", status="done", reason=None,
                         actual_output_path="goals/goal-1/ch-2.md", actual_count=1,
                         updated_at="2026-09-14T00:10:00Z")
    store.upsert_evidence_batch([
        {"id": f"ev-{i}", "report_id": research_id, "platform": "xhs", "agent_name": "xhs-doubao",
         "permalink": f"https://www.xiaohongshu.com/explore/{i}", "fetched_at": "2026-09-14T00:00:00Z"}
        for i in range(3)
    ] + [{"id": "ev-d", "report_id": research_id, "platform": "douyin", "agent_name": "douyin-doubao",
          "permalink": "https://www.douyin.com/video/1", "fetched_at": "2026-09-14T00:00:00Z"}])
    return database, research_id


def _get(database: Path, research_id: str, tmp_path: Path) -> dict:
    from app.api.main import create_app

    application = create_app(database, SCHEMA_PATH, runs_root=tmp_path / "runs", engine_probe=lambda: {})

    async def exercise() -> httpx.Response:
        async with application.router.lifespan_context(application):
            transport = httpx.ASGITransport(app=application)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.get(f"/api/researches/{research_id}")

    response = asyncio.run(exercise())
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_货1_已完成但有段超时没写成_快照带未写成读数且标签不动(tmp_path: Path) -> None:
    database, research_id = _seed(tmp_path)
    snapshot = _get(database, research_id, tmp_path)
    assert snapshot["status_label"] == "已完成"
    # 条数与附录缺失清单同一把尺子：按 agent_name 数这一章入库的行（3 条小红书），抖音那章 done 不算。
    assert snapshot["unwritten"] == {"sections": 1, "timeouts": 1, "yielded": 3}


def test_货1_没有缺段的研究不挂黄条(tmp_path: Path) -> None:
    database, research_id = _seed(tmp_path, second="done")
    assert _get(database, research_id, tmp_path)["unwritten"] is None
