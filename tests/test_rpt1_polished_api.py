"""§RPT-1 货 3：正式稿的三个接口与后台整理的失败路径。

判据落在接口返回与库里的登记上，不落在日志上。整理本身用桩，不付引擎调用。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.report.polish.run import artifact_paths

ROOT = Path(__file__).resolve().parents[1]
RESEARCH_ID = "r-rpt1"
WORK_DRAFT = """# 工作稿

## 结论

- 豆包讨论量最大[S01]

## 信息源

- [S01] [某帖](https://example.com/a)（fetched_at=2026-09-01T00:00:00Z）
"""


def _app(tmp_path: Path):
    from app.api.main import create_app

    runs_root = tmp_path / "runs"
    goal = runs_root / RESEARCH_ID / "goals" / "goal-1"
    goal.mkdir(parents=True)
    (goal / "goal-1-report.md").write_text(WORK_DRAFT, encoding="utf-8")
    app = create_app(tmp_path / "owli.db", ROOT / "app" / "store" / "schema.sql",
                     runs_root=runs_root, engine_probe=lambda: {"claude": {"status": "available"}})
    # schema 平时在 lifespan 里建；这里要先塞一行报告，所以提前建一次（重复建是幂等的）。
    from app.adapters.selfcheck import initialize_and_check

    initialize_and_check(tmp_path / "owli.db", ROOT / "app" / "store" / "schema.sql")
    store = app.state.store
    store.create_report(id=RESEARCH_ID, title="国内大家对豆包的看法",
                        research_question="国内大家对豆包的看法", created_at="2026-09-05T00:00:00Z",
                        status="completed", report_path="goals/goal-1/goal-1-report.md",
                        plan_snapshot={"research_question": "国内大家对豆包的看法",
                                       "subjects": ["豆包"], "entities": [], "goals": []})
    return app, runs_root, store


def _route(app, path: str):
    return next(route.endpoint for route in app.routes
                if getattr(route, "path", None) == path)


def _run(app, coro_factory):
    async def go():
        async with app.router.lifespan_context(app):
            return await coro_factory()

    return asyncio.run(go())


def test_report_templates_lists_three_with_default_first(tmp_path: Path) -> None:
    app, _, _ = _app(tmp_path)
    body = _run(app, _route(app, "/api/report-templates"))
    names = [t["name"] for t in body["data"]["templates"]]
    assert names[0] == "consulting" and len(names) == 3
    for item in body["data"]["templates"]:
        assert item["title"] and item["description"] and item["sections"]


def test_unknown_template_is_400_not_500(tmp_path: Path) -> None:
    from fastapi import HTTPException

    app, _, _ = _app(tmp_path)
    endpoint = _route(app, "/api/researches/{research_id}/export")
    with pytest.raises(HTTPException) as caught:
        _run(app, lambda: endpoint(RESEARCH_ID, {"kind": "polished", "template": "no-such"}))
    assert caught.value.status_code == 400


def test_polished_get_is_404_before_anything_is_generated(tmp_path: Path) -> None:
    from fastapi import HTTPException

    app, _, _ = _app(tmp_path)
    endpoint = _route(app, "/api/researches/{research_id}/polished")
    with pytest.raises(HTTPException) as caught:
        _run(app, lambda: endpoint(RESEARCH_ID, "consulting"))
    assert caught.value.status_code == 404


def test_polished_get_returns_markdown_tables_and_workdraft_sources(tmp_path: Path) -> None:
    """sources 与工作稿同形，前端角标卡才能零改动复用。"""
    app, runs_root, _ = _app(tmp_path)
    md_path, tables_path = artifact_paths(runs_root, RESEARCH_ID, "consulting")
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text("# 执行摘要\n\n豆包讨论量最大[S01]。\n", encoding="utf-8")
    tables_path.write_text(json.dumps({"tables": {"platform_mix": {"rows": []}}}), encoding="utf-8")
    body = _run(app, lambda: _route(app, "/api/researches/{research_id}/polished")(
        RESEARCH_ID, "consulting"))
    data = body["data"]
    assert data["template"] == "consulting" and "执行摘要" in data["markdown"]
    assert "platform_mix" in data["tables"]
    assert [s["citation_no"] for s in data["sources"]] == [1]
    assert data["sources"][0]["mark"] == "S01"


def test_polished_export_starts_background_task_and_records_on_success(tmp_path: Path,
                                                                      monkeypatch) -> None:
    app, runs_root, store = _app(tmp_path)
    md_path, _ = artifact_paths(runs_root, RESEARCH_ID, "consulting")

    async def fake_polish(store_, research_id, runs, text, *, template=None, **kwargs):
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text("# 执行摘要\n\n正文[S01]。\n", encoding="utf-8")
        return {"status": "ok", "template": template, "path": str(md_path),
                "tables_path": "", "attempts": 1, "offpool": []}

    monkeypatch.setattr("app.report.polish.run.polish", fake_polish)
    endpoint = _route(app, "/api/researches/{research_id}/export")

    async def go():
        async with app.router.lifespan_context(app):
            body = await endpoint(RESEARCH_ID, {"kind": "polished"})
            await asyncio.sleep(0)  # 让后台任务跑到底
            await asyncio.sleep(0)
            return body

    body = asyncio.run(go())
    assert body["data"] == {"kind": "polished", "template": "consulting", "status": "started",
                            "title": "调研报告（咨询体）"}
    exports = (store.get_report(RESEARCH_ID)["extra"] or {}).get("exports") or []
    assert [e["kind"] for e in exports] == ["polished"]
    assert exports[0]["url"].endswith(".polished.consulting.md")


def test_engine_failure_leaves_research_completed_and_records_a_failed_export(tmp_path: Path,
                                                                             monkeypatch) -> None:
    """货 3 判据：整理炸了，研究状态一个字不改，登记里留一条无 url 的失败记录。"""
    app, _, store = _app(tmp_path)

    async def boom(*args, **kwargs):
        raise RuntimeError("引擎不可用")

    monkeypatch.setattr("app.report.polish.run.polish", boom)
    endpoint = _route(app, "/api/researches/{research_id}/export")

    async def go():
        async with app.router.lifespan_context(app):
            await endpoint(RESEARCH_ID, {"kind": "polished"})
            for _ in range(4):
                await asyncio.sleep(0)

    asyncio.run(go())
    report = store.get_report(RESEARCH_ID)
    assert report["status"] == "completed"
    exports = (report["extra"] or {}).get("exports") or []
    assert len(exports) == 1 and exports[0]["url"] is None
    assert "引擎不可用" in exports[0]["desc"]


def test_offpool_failure_is_recorded_with_the_offending_marks(tmp_path: Path, monkeypatch) -> None:
    app, _, store = _app(tmp_path)

    async def offpool(store_, research_id, runs, text, *, template=None, **kwargs):
        return {"status": "failed", "template": template, "path": "", "tables_path": "",
                "attempts": 2, "offpool": ["S64"], "errors": []}

    monkeypatch.setattr("app.report.polish.run.polish", offpool)
    endpoint = _route(app, "/api/researches/{research_id}/export")

    async def go():
        async with app.router.lifespan_context(app):
            await endpoint(RESEARCH_ID, {"kind": "polished"})
            for _ in range(4):
                await asyncio.sleep(0)

    asyncio.run(go())
    exports = (store.get_report(RESEARCH_ID)["extra"] or {}).get("exports") or []
    assert exports and "S64" in exports[0]["desc"] and exports[0]["url"] is None
