"""§DLV-1 货 1：报告结构化只读 API 与证据清单 API。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

from tests.test_m4b_history_replay import SCHEMA_PATH, _seed_history

URL_A = "https://www.xiaohongshu.com/explore/a1"
URL_B = "https://www.douyin.com/video/b2"
URL_C = "https://www.zhihu.com/p/c3"
NOTES = "权威1:平台社区基线 · 时效2:近期 · 交叉?:缺断言血缘簇 · 完整1:文案 · 无关1:商单密度"

SECTION_MD = "\n".join([
    "# 采集节标题", "", "正文引用 [S01] 与 [S02]。", "", "## 结论", "",
    "- 结论一 [S01][S02]", "- 结论二 [S02]", "", "## 信息源", "",
    f"- [S01] [小红书笔记](%s)（fetched_at=2026-08-28T08:22:50Z）" % URL_A,
    f"- [S02] [抖音视频](%s)（fetched_at=2026-08-28T08:22:50Z）" % URL_B,
    "", "## 缺失清单", "", "- 此处缺失：goal-1/ch-3；原因：timeout",
])
PLACEHOLDER_MD = "## goal-2｜第二节\n\n- 此处缺失：goal-2/ch-4/sec-2；原因：timeout"


def _seed_evidence(database: Path, research_id: str) -> None:
    from app.store.dao import Store

    store = Store(database)
    rows = [
        ("ev-1", "xhs", URL_A, "小红书笔记", 1),
        ("ev-2", "douyin", URL_B, "抖音视频", 2),
        ("ev-3", "web_search", URL_C, "未引用文章", None),
    ]
    for ev_id, platform, url, title, citation in rows:
        store.add_evidence(
            id=ev_id, report_id=research_id, platform=platform, permalink=url,
            fetched_at="2026-08-28T08:22:50Z", title=title, goal_id="goal-1",
            fetch_method="third_party_api", content_excerpt=f"{title} 摘要",
            score_authority=1, score_freshness=2, score_crossref=None,
            score_completeness=1, score_independence=1, rating_notes=NOTES,
            rated_by=f"baseline:{platform}@v1", raw_metrics={"digg_count": 3},
            citation_no=citation,
        )


def _write_json_report(report_path: Path) -> None:
    document = {
        "title": "JSON 成稿", "chapter_id": "ch-4",
        "sections": [
            {"section_id": "ch-4/sec-1", "goal_id": "goal-1", "title": "第一节", "markdown": SECTION_MD},
            {"section_id": "ch-4/sec-2", "goal_id": "goal-2", "title": "第二节", "markdown": PLACEHOLDER_MD},
        ],
        "缺失清单": [{"goal_id": "goal-2", "chapter_id": "ch-4/sec-2", "reason": "timeout",
                     "text": "此处缺失：goal-2/ch-4/sec-2；原因：timeout"}],
        "收尾注释": {"决策天平": []},
    }
    report_path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")


def _get(application, path: str) -> httpx.Response:
    async def exercise() -> httpx.Response:
        async with application.router.lifespan_context(application):
            transport = httpx.ASGITransport(app=application)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.get(path)
    return asyncio.run(exercise())


def _app(tmp_path: Path, database: Path):
    from app.api.main import create_app

    return create_app(database, SCHEMA_PATH, runs_root=tmp_path / "runs", engine_probe=lambda: {})


def test_json_成稿的结构化报告与证据清单(tmp_path: Path) -> None:
    database, research_id, report_path = _seed_history(tmp_path)
    _write_json_report(report_path)
    _seed_evidence(database, research_id)
    application = _app(tmp_path, database)

    response = _get(application, f"/api/researches/{research_id}/report")
    assert response.status_code == 200, response.text
    view = response.json()["data"]
    assert view["format"] == "json"
    assert view["title"] == "JSON 成稿"
    assert [s["section_id"] for s in view["sections"]] == ["ch-4/sec-1", "ch-4/sec-2"]
    assert view["sections"][0]["placeholder"] is False
    assert "## 结论" not in view["sections"][0]["markdown"]
    assert view["sections"][1]["placeholder"] is True
    assert view["sections"][1]["missing_reason"] == "timeout"
    assert view["conclusions"] == ["结论一 [S01][S02]", "结论二 [S02]"]
    assert [(s["citation_no"], s["permalink"]) for s in view["sources"]] == [(1, URL_A), (2, URL_B)]
    assert view["citations"] == {"cited": [1, 2], "listed": [1, 2], "dangling": []}
    assert [(m["goal_id"], m["chapter_id"], m["reason"]) for m in view["missing"]] == [
        ("goal-2", "ch-4/sec-2", "timeout"), ("goal-1", "ch-3", "timeout"),
    ]
    assert view["exports"] == [] and view["feishu"]["status"] == "pending"

    response = _get(application, f"/api/researches/{research_id}/evidence")
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert [item["citation_no"] for item in data["items"]] == [1, 2, None]
    assert data["counts"] == {
        "total": 3, "cited": 2,
        "by_platform": {"xhs": 1, "douyin": 1, "web_search": 1}, "by_grade": {"?": 3},
        # §CMT-1 货 5：多一路帖/评论计数，报告页据此筛选
        "by_kind": {"post": 3},
    }
    first = data["items"][0]
    assert first["permalink"] == URL_A and first["score_crossref"] is None
    assert first["rating_notes"] == NOTES and first["raw_metrics"] == {"digg_count": 3}
    assert data["score_fields"] == [
        "score_authority", "score_freshness", "score_crossref",
        "score_completeness", "score_independence",
    ]


def test_markdown_成稿也能解析且缺产物返回404(tmp_path: Path) -> None:
    database, research_id, report_path = _seed_history(tmp_path)
    report_path.write_text(f"# MD 成稿\n\n{SECTION_MD.split(chr(10), 2)[2]}", encoding="utf-8")
    application = _app(tmp_path, database)

    view = _get(application, f"/api/researches/{research_id}/report").json()["data"]
    assert view["format"] == "markdown" and view["title"] == "MD 成稿"
    assert len(view["sections"]) == 1 and len(view["sources"]) == 2
    assert view["conclusions"][0] == "结论一 [S01][S02]"
    assert view["missing"] == [{"goal_id": "goal-1", "chapter_id": "ch-3", "reason": "timeout",
                                "text": "此处缺失：goal-1/ch-3；原因：timeout"}]

    report_path.unlink()
    assert _get(application, f"/api/researches/{research_id}/report").status_code == 404
    assert _get(application, "/api/researches/r-nope/report").status_code == 404
    assert _get(application, "/api/researches/r-nope/evidence").status_code == 404


def test_rd1_视图层剥掉写手吐出的HTML注释():
    """§RD-1：`<!-- q-1：两者兼顾 -->` 之类批注不能原样显示给读者。"""
    from app.report.render import parse_report

    text = (
        "# 题\n\n正文一句。<!-- q-1：两者兼顾 -->\n\n## 结论\n"
        "- 结论 A <!-- q-1 -->[S01]\n- <!-- q-1: 多行\n注释 -->结论 B[S01]\n\n"
        "## 信息源\n- [S01] [标题](https://example.com/a)\n"
    )
    view = parse_report(text)
    assert "<!--" not in view["sections"][0]["markdown"]
    assert all("<!--" not in c for c in view["conclusions"]), view["conclusions"]
    assert [c.split("[")[0].strip() for c in view["conclusions"]] == ["结论 A", "结论 B"]
