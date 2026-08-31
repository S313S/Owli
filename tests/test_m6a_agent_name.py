"""§M6-a 货 2：product_hunt / reddit / douyin 直落库也写下「是哪一章调的我」。

RATE-2 `5d012a2` 只补了 xhs；同族这三个源采完自己 `upsert_evidence_batch`，
载荷里没有 `agent_name`，这些行没有任何章认领——评级章物化时看不见它们，
「每源出货 ≥N 条」（货 3）也认不出是哪一章的产出。
"""

from __future__ import annotations

import asyncio
import functools
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse

from tests.test_d015_source_persistence import ImmediateGate, _capability, _store

AGENT_ID = "data-collection-7"


def _run(adapter, tool: str, source_id: str, report_id: str, query: str,
         **extra) -> None:
    asyncio.run(adapter.call(
        tool, query, "30d", research_id=report_id, goal_id="goal-1",
        agent_id=AGENT_ID, capability=_capability(source_id), item_limit=1,
        on_event=lambda event: None, **extra,
    ))


def _only_row(store, report_id: str) -> dict:
    rows = store.list_evidence(report_id)
    assert len(rows) == 1, rows
    return rows[0]


def test_product_hunt直落库带上章归属(tmp_path: Path) -> None:
    from app.adapters.source_mcp import SourceToolAdapter
    from app.sources import product_hunt as ph
    from tests.test_product_hunt import _node, _response

    report_id = "r-m6a-ph"
    store = _store(tmp_path, report_id)
    adapter = SourceToolAdapter({"source.product_hunt": ph.search}, store=store)

    with (
        patch.object(ph, "_load_token", return_value="私密-token"),
        patch.object(ph, "_post_graphql", return_value=_response([_node(1, 30)])),
        patch.object(ph, "_utc_now",
                     return_value=datetime(2026, 9, 1, tzinfo=timezone.utc)),
    ):
        _run(adapter, "source.product_hunt", "product_hunt", report_id,
             "产品", log_root=tmp_path / "logs")

    assert _only_row(store, report_id)["agent_name"] == AGENT_ID


def test_抖音直落库带上章归属(tmp_path: Path) -> None:
    from app.adapters.source_mcp import SourceToolAdapter
    from app.sources import douyin
    from tests.test_s1_sources import _douyin_video

    report_id = "r-m6a-douyin"
    store = _store(tmp_path, report_id)

    def http_request(method, url, headers, body, timeout):
        del method, headers, body, timeout
        if urlparse(url).path == douyin._SEARCH_PATH:
            return douyin.HttpResponse(200, {
                "code": 200,
                "data": {"items": [_douyin_video(1, 1)],
                         "pagination": {"has_more": 0}},
            })
        return douyin.HttpResponse(200, {
            "code": 200,
            "data": {"comments": [{"cid": "c-1", "text": "真实评论正文"}],
                     "cursor": 20, "has_more": 0, "total": 1},
        })

    entrypoint = functools.partial(
        douyin.search, token="runtime-secret", http_request=http_request,
        rate_gate=ImmediateGate(),
        now=lambda: datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    adapter = SourceToolAdapter({"source.douyin": entrypoint}, store=store)

    _run(adapter, "source.douyin", "douyin", report_id, "扫地机器人",
         comment_video_limit=1)

    assert _only_row(store, report_id)["agent_name"] == AGENT_ID


def _mcp(result: dict) -> bytes:
    return json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "result": {"content": [{"type": "text",
                                "text": json.dumps(result, ensure_ascii=False)}]},
    }).encode("utf-8")


def test_reddit直落库带上章归属(tmp_path: Path) -> None:
    from app.adapters.source_mcp import SourceToolAdapter
    from app.sources import reddit

    report_id = "r-m6a-reddit"
    store = _store(tmp_path, report_id)
    post = {
        "id": "t3_abc", "title": "扫地机器人半年真实体验",
        "body": "续航与噪音都还行", "author": "u/tester", "subreddit": "vacuum",
        "url": "https://www.reddit.com/r/vacuum/comments/abc/",
        "score": 42, "numComments": 7,
    }

    def http_request(method, url, headers, body, timeout):
        del method, url, headers, timeout
        payload = json.loads(body.decode("utf-8")) if body else {}
        if payload.get("method") == "initialize":
            return reddit.HttpResponse(200, {"Mcp-Session-Id": "session-1"}, _mcp({}))
        name = (payload.get("params") or {}).get("name")
        if name == "search_dataset":
            return reddit.HttpResponse(200, {}, _mcp({"items": [post]}))
        return reddit.HttpResponse(200, {}, _mcp({"record": post}))

    entrypoint = functools.partial(
        reddit.search, prowlo_token="runtime-secret", http_request=http_request,
        now=lambda: datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    adapter = SourceToolAdapter({"source.reddit": entrypoint}, store=store)

    _run(adapter, "source.reddit", "reddit", report_id, "扫地机器人")

    assert _only_row(store, report_id)["agent_name"] == AGENT_ID
