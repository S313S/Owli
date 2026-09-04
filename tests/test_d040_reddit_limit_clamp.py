"""§D-040：fast 档名额（reddit=25）撞源校验 1–20，Reddit 每次调用被打回。

夜跑 r-b10812f664d2「海外证据 0」的真因。修法是源层封顶而不是报错，
本文件把三条口径（超上限封顶 / 合法值原样 / 非法值仍报错）钉在源上。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from app.sources import reddit


def _sse_result(result: dict) -> bytes:
    payload = {"jsonrpc": "2.0", "id": 1, "result": result}
    return f"event: message\ndata: {json.dumps(payload)}\n\n".encode()


def _tool_result(value: dict) -> bytes:
    return _sse_result({"content": [{"type": "text", "text": json.dumps(value)}]})


def _item(index: int) -> dict:
    return {
        "redditId": f"t3_post{index}",
        "subreddit": "productivity",
        "title": f"Reddit 标题 {index}",
        "body": f"正文 {index}",
        "permalink": f"/r/productivity/comments/post{index}/title/",
        "score": index,
        "numComments": index + 1,
        "upvote_ratio": 0.9,
        "redditCreatedAt": "2026-08-20T00:00:00.000Z",
        "author": f"author{index}",
        "status": "normal",
        "removedByCategory": None,
    }


def _recording_http(sink: list[dict]):
    """录下每次工具调用的入参；一跳给满 20 条，逼出 dataset→live 两步。"""

    def http_request(method, url, headers, body, timeout):
        payload = json.loads(body) if body else {}
        if payload.get("method") == "initialize":
            return reddit.HttpResponse(
                200, {"Mcp-Session-Id": "session-1"},
                _sse_result({"protocolVersion": "2025-03-26"}),
            )
        if payload.get("method") == "notifications/initialized":
            return reddit.HttpResponse(202, {}, b"")
        params = payload["params"]
        sink.append({"name": params["name"], "arguments": params["arguments"]})
        return reddit.HttpResponse(
            200, {}, _tool_result({"items": [_item(1), _item(2)]})
        )

    return http_request


def _search(limit: int, sink: list[dict]):
    return reddit.search(
        "Doubao",
        "90d",
        limit=limit,
        prowlo_token="prowlo-runtime",
        http_request=_recording_http(sink),
        now=lambda: datetime(2026, 9, 4, tzinfo=timezone.utc),
    )


def test_fast档名额25被封顶到20而不是报错() -> None:
    sink: list[dict] = []

    rows = _search(25, sink)

    assert [call["name"] for call in sink] == ["search_dataset", "social_search"]
    assert sink[0]["arguments"]["limit"] == 20
    assert sink[1]["arguments"]["limit"] == 20
    assert len(rows) == 2


def test_上限内的名额原样下发() -> None:
    sink: list[dict] = []

    _search(20, sink)

    assert sink[0]["arguments"]["limit"] == 20


def test_名额小于1仍然报错() -> None:
    with pytest.raises(ValueError, match="limit 必须为不小于 1 的整数"):
        _search(0, [])
    with pytest.raises(ValueError, match="limit 必须为不小于 1 的整数"):
        _search(True, [])  # type: ignore[arg-type]


def test_评论二跳的per_post超上限同样封顶() -> None:
    """采集卡可以把 with_comments.per_post 写成任意正整数（source_mcp.py:329
    只校验非负），Reddit 评论入口原先同样是 1–20 报错——同一族缺陷。"""

    captured: dict = {}

    class _Client:
        def call(self, name, arguments):
            captured["arguments"] = arguments
            return {"post": {"permalink": "/r/p/1/"}, "comments": []}

    reddit.fetch_comments(
        "post1", parent_permalink="/r/p/1/", limit=50, client=_Client(),
    )

    assert captured["arguments"]["commentLimit"] == 20


def test_两档真实名额都能过reddit源自己的校验() -> None:
    """把「档位名额」和「源上限」钉在一起：D-040 就是这两个数字各改各的漂出来的。

    夹具里写死 25 只锁住今天的值；这条读真配置，将来谁把 fast 档 reddit
    名额调成 40（或 standard 调成 0），这里当场红。
    """

    from app.config import load_research_scale_config

    scales = load_research_scale_config()
    for scale in ("fast", "standard"):
        quota = scales.profile(scale).source_item_limits["reddit"]
        sink: list[dict] = []
        _search(quota, sink)
        assert sink[0]["arguments"]["limit"] == min(quota, 20)
