"""D-015：源内重复证据不得因唯一键冲突中止采集章。"""

from __future__ import annotations

import asyncio
import ast
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]


class ImmediateGate:
    def wait(self) -> None:
        return None


def _capability(source_id: str):
    from app.adapters.capability import Capability

    return Capability(
        tools=(f"source.{source_id}",),
        sources=(source_id,),
        network="sources_only",
    )


def _store(tmp_path: Path, report_id: str):
    from app.store.dao import Store
    from app.store.schema import initialize_database_if_empty

    database = tmp_path / "owli.db"
    initialize_database_if_empty(database, ROOT / "app/store/schema.sql")
    store = Store(database)
    store.create_report(
        id=report_id,
        title="D-015 重复证据验证",
        research_question="重复命中是否中止采集章",
        created_at="2026-08-28T00:00:00Z",
    )
    store.ensure_chapters(
        report_id,
        [{"goal_id": "goal-1", "chapter_id": "ch-1"}],
        updated_at="2026-08-28T00:00:00Z",
    )
    store.start_chapter(
        report_id, "goal-1", "ch-1",
        engine="codex", updated_at="2026-08-28T00:00:01Z",
    )
    return store


def _finish_done(store, report_id: str, actual_count: int) -> dict:
    store.finish_chapter(
        report_id, "goal-1", "ch-1",
        status="done", reason=None, actual_output_path="evidence.json",
        actual_count=actual_count, updated_at="2026-08-28T00:00:02Z",
    )
    return store.list_chapters(report_id)[0]


def test_所有信息源禁止调用冲突即回滚的批量写入() -> None:
    offenders: list[str] = []
    for path in sorted((ROOT / "app/sources").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            is_forbidden_call = isinstance(node, ast.Call) and (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_evidence_batch"
                or isinstance(node.func, ast.Name)
                and node.func.id == "add_evidence_batch"
            )
            if is_forbidden_call:
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")

    assert not offenders, (
        "信息源不得调用 add_evidence_batch；请改用 upsert_evidence_batch。"
        "\n违规调用：\n" + "\n".join(offenders)
    )


def test_小红书同章重复_permalink_只入库一次且按实采条数_done(tmp_path: Path) -> None:
    from app.adapters.source_mcp import SourceToolAdapter
    from app.sources import xhs

    report_id = "r-d015-xhs"
    store = _store(tmp_path, report_id)
    events: list[dict] = []
    note = {
        "model_type": "note",
        "note": {
            "id": "note-shared", "title": "重复笔记", "desc": "真实正文",
            "type": "normal", "xsec_token": "signed=", "liked_count": 9,
            "comments_count": 3, "collected_count": 2,
            "user": {"nickname": "作者", "userid": "user-1"},
        },
    }

    def http_get(url, headers, timeout):
        del url, headers, timeout
        return xhs.HttpResponse(200, {
            "code": 200,
            "data": {"code": 200, "success": True,
                     "data": {"items": [note]}, "next_page": None},
        })

    def source(query, window, **kwargs):
        return xhs.search(
            query, window, token="runtime-secret", http_get=http_get,
            rate_gate=ImmediateGate(),
            now=lambda: datetime(2026, 8, 28, tzinfo=timezone.utc),
            **kwargs,
        )

    adapter = SourceToolAdapter({"source.xhs": source}, store=store)
    hits = []
    for query in ("第一组关键词", "第二组重叠关键词"):
        hits.extend(asyncio.run(adapter.call(
            "source.xhs", query, "30d", research_id=report_id,
            goal_id="goal-1", agent_id="data-collection-xhs",
            capability=_capability("xhs"), item_limit=1,
            on_event=events.append,
        )))

    row = _finish_done(store, report_id, len(hits))
    assert len(store.list_evidence(report_id)) == 1
    assert (row["status"], row["actual_count"], row["reason"]) == ("done", 2, None)
    assert [event["data"]["returned"] for event in events] == [1, 1]
    assert all(event["data"].get("reason") != "tool_unavailable" for event in events)


def test_抖音同章重复_permalink_只入库一次且按实采条数_done(tmp_path: Path) -> None:
    from app.adapters.source_mcp import SourceToolAdapter
    from app.sources import douyin

    report_id = "r-d015-douyin"
    store = _store(tmp_path, report_id)
    events: list[dict] = []
    video = {
        "aweme_info": {
            "aweme_id": "video-shared", "desc": "重复视频",
            "create_time": 1787799195,
            "author": {"uid": "uid-1", "nickname": "作者"},
            "statistics": {
                "digg_count": 8, "comment_count": 1,
                "share_count": 2, "collect_count": 3,
            },
        },
    }

    def http_request(method, url, headers, body, timeout):
        del method, headers, body, timeout
        if urlparse(url).path == douyin._SEARCH_PATH:
            return douyin.HttpResponse(200, {
                "code": 200,
                "data": {"items": [video], "pagination": {"has_more": 0}},
            })
        return douyin.HttpResponse(200, {
            "code": 200,
            "data": {
                "comments": [{"cid": "c-1", "text": "真实评论"}],
                "cursor": 0, "has_more": 0, "total": 1,
            },
        })

    def source(query, window, **kwargs):
        return douyin.search(
            query, window, comment_video_limit=1, token="runtime-secret",
            http_request=http_request, rate_gate=ImmediateGate(),
            now=lambda: datetime(2026, 8, 28, tzinfo=timezone.utc),
            **kwargs,
        )

    adapter = SourceToolAdapter({"source.douyin": source}, store=store)
    hits = []
    for query in ("第一组关键词", "第二组重叠关键词"):
        hits.extend(asyncio.run(adapter.call(
            "source.douyin", query, "30d", research_id=report_id,
            goal_id="goal-1", agent_id="data-collection-douyin",
            capability=_capability("douyin"), item_limit=1,
            on_event=events.append,
        )))

    row = _finish_done(store, report_id, len(hits))
    assert len(store.list_evidence(report_id)) == 1
    assert (row["status"], row["actual_count"], row["reason"]) == ("done", 2, None)
    assert [event["data"]["returned"] for event in events] == [1, 1]
    assert all(event["data"].get("reason") != "tool_unavailable" for event in events)
