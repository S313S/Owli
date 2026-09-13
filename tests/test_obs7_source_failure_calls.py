"""§OBS-7 货 4：付费源失败 / 搜空的轮次也要报调用次数。

判据 2 的单元层形态：夹具自己数真实发出的 HTTP 请求，源报的对账次数必须逐字相等。
只加发射点——采集、重试、限流一概不动（这里只断言事件，不断言产出变化）。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from tests.test_s1_sources import _mcp_tool_result, _reddit_item, _sse_result


class ImmediateGate:
    def wait(self) -> None:
        return None


_NOW = lambda: datetime(2026, 9, 13, tzinfo=timezone.utc)  # noqa: E731


def _of(events, kind):
    return [event["data"] for event in events if event["type"] == kind]


def test_抖音两版搜索都挂_对账次数等于真实请求数且紧跟不可用事件() -> None:
    from app.sources import douyin

    sent: list[str] = []

    def http_request(method, url, headers, body, timeout):
        sent.append(url)
        return douyin.HttpResponse(400, {"code": 400, "message": "boom"})

    events: list[dict] = []
    assert douyin.search("豆包", limit=5, comment_video_limit=1, token="t",
                         http_request=http_request, rate_gate=ImmediateGate(),
                         on_event=events.append, now=_NOW) == []

    kinds = [event["type"] for event in events]
    assert kinds[-2:] == ["source_unavailable", "source_usage_reconciled"]
    (reconciled,) = _of(events, "source_usage_reconciled")
    (unavailable,) = _of(events, "source_unavailable")
    assert reconciled["outcome"] == "unavailable" and reconciled["returned"] == 0
    assert sum(reconciled["calls"].values()) == len(sent) > 0
    assert unavailable["calls"] == reconciled["calls"]


def test_小红书搜索被限流_拿到响应的次数照报_传输层断连不计() -> None:
    from app.sources import xhs

    sent: list[str] = []

    def throttled(url, headers, timeout):
        sent.append(url)
        return xhs.HttpResponse(429, {"code": 429, "message": "too many requests"})

    events: list[dict] = []
    assert xhs.search("豆包", "30d", limit=2, token="t", http_get=throttled,
                      rate_gate=ImmediateGate(), on_event=events.append, now=_NOW) == []
    assert events[0]["type"] == "source_unavailable"          # SRC-1：首条仍是分诊
    (reconciled,) = _of(events, "source_usage_reconciled")
    assert reconciled["calls"] == {"search_notes": len(sent), "get_image_note_detail": 0}
    assert len(sent) > 0

    def broken(url, headers, timeout):
        raise xhs.TikHubError("transport", endpoint=xhs._SEARCH_PATH, detail="URLError")

    events = []
    xhs.search("豆包", "30d", limit=2, token="t", http_get=broken,
               rate_gate=ImmediateGate(), on_event=events.append, now=_NOW)
    (reconciled,) = _of(events, "source_usage_reconciled")
    assert reconciled["calls"]["search_notes"] == 0


def test_Reddit_Prowlo半路挂_已打出的工具调用照报() -> None:
    from app.sources import reddit

    tool_calls: list[str] = []

    def http_request(method, url, headers, body, timeout):
        if "api.prowlo.com" not in url:
            return reddit.HttpResponse(503, {}, b"{}")         # Apify 兜底也挂，别碰真环境
        payload = json.loads(body) if body else {}
        if payload.get("method") == "initialize":
            return reddit.HttpResponse(200, {"Mcp-Session-Id": "s-1"},
                                       _sse_result({"protocolVersion": "2025-03-26"}))
        if payload.get("method") == "notifications/initialized":
            return reddit.HttpResponse(202, {}, b"")
        name = payload["params"]["name"]
        tool_calls.append(name)
        if name == "search_dataset":
            return reddit.HttpResponse(200, {}, _mcp_tool_result(
                {"items": [{**_reddit_item(1), "id": "rec-1"}]}))
        return reddit.HttpResponse(503, {}, b"{}")             # get_record 与 live_read 都挂

    events: list[dict] = []
    assert reddit.search("AI meeting", "30d", limit=2, prowlo_token="p", apify_token="apify-test",
                         http_request=http_request, on_event=events.append, now=_NOW) == []

    (reconciled,) = _of(events, "source_usage_reconciled")
    assert reconciled["provider"] == "prowlo" and reconciled["outcome"] == "unavailable"
    assert reconciled["calls"] == {"dataset_search": 1, "dataset_get_record": 1, "live_read": 1}
    assert sum(reconciled["calls"].values()) == len(tool_calls)
    (unavailable,) = _of(events, "source_unavailable")
    assert unavailable["calls"] == reconciled["calls"]


def test_Reddit_搜空也报次数() -> None:
    from app.sources import reddit

    tool_calls: list[str] = []

    def http_request(method, url, headers, body, timeout):
        if "api.prowlo.com" not in url:
            return reddit.HttpResponse(503, {}, b"{}")         # Apify 兜底也挂，别碰真环境
        payload = json.loads(body) if body else {}
        if payload.get("method") == "initialize":
            return reddit.HttpResponse(200, {"Mcp-Session-Id": "s-1"},
                                       _sse_result({"protocolVersion": "2025-03-26"}))
        if payload.get("method") == "notifications/initialized":
            return reddit.HttpResponse(202, {}, b"")
        tool_calls.append(payload["params"]["name"])
        return reddit.HttpResponse(200, {}, _mcp_tool_result({"items": []}))

    events: list[dict] = []
    assert reddit.search("AI meeting", "30d", limit=2, prowlo_token="p", apify_token="apify-test",
                         http_request=http_request, on_event=events.append, now=_NOW) == []
    kinds = [event["type"] for event in events]
    assert kinds[-2:] == ["source_empty", "source_usage_reconciled"]
    (reconciled,) = _of(events, "source_usage_reconciled")
    assert reconciled["outcome"] == "empty"
    assert sum(reconciled["calls"].values()) == len(tool_calls) == 2
