"""§ALLOC-1 货 2：抖音搜索页级重试——一页失败不作废整词，已取到的页保留。

2026-09-11 探针实录：v5 恒 400、v4 首页 13 次里 6 次 400/URLError，复试即成；
源层此前没有重试，整词 0 条。用户拍甲：只解禁抖音这一处，词级重试。
"""

from __future__ import annotations

from tests.test_d038_douyin_fallback import _router, _search, _v4_page, _v5_page


def _events_of(events: list[dict], kind: str) -> list[dict]:
    return [e["data"] for e in events if e["type"] == kind]


def test_v4首页400抖一次_复试即成_整词不再0条且留重试事件() -> None:
    log: list[str] = []
    events: list[dict] = []
    result = _search(_router({"v5": [400], "v4": [400, _v4_page(["a1", "a2"])]}, log=log), events)
    assert [item["platform_item_id"] for item in result] == ["a1", "a2"]
    assert log == ["v5", "v4", "v4"], "v5 的 400 不重试（恒定），v4 的 400 重试一次即成"
    (retry,) = _events_of(events, "source_search_retry")
    assert retry["search_version"] == "v4" and retry["page"] == 1 and retry["attempt"] == 1
    assert retry["closed_reason"] == "tikhub_http_400"
    assert not _events_of(events, "source_unavailable")


def test_v4首页连挂三次才算兜底也断_主路死因仍是头条() -> None:
    log: list[str] = []
    events: list[dict] = []
    assert _search(_router({"v5": [400], "v4": [400]}, log=log), events) == []
    assert log == ["v5", "v4", "v4", "v4"]
    assert len(_events_of(events, "source_search_retry")) == 2
    (event,) = _events_of(events, "source_unavailable")
    assert event["closed_reason"] == "tikhub_http_400"
    assert event["fallback_closed_reason"] == "tikhub_http_400"


def test_v4后页挂死_保留已取到的页并留partial事件_不作废整词() -> None:
    log: list[str] = []
    events: list[dict] = []
    first = _v4_page(["a1", "a2"], has_more=True, cursor=12)
    result = _search(_router({"v5": [400], "v4": [first, 400, 400, 400]}, log=log), events, limit=10)
    assert [item["platform_item_id"] for item in result] == ["a1", "a2"], "第一页保留"
    assert log == ["v5", "v4", "v4", "v4", "v4"], "第二页试满三次"
    (partial,) = _events_of(events, "source_search_partial")
    assert partial["search_version"] == "v4" and partial["pages_kept"] == 1 and partial["items_kept"] == 2
    assert partial["closed_reason"] == "tikhub_http_400"
    assert not _events_of(events, "source_unavailable")
    (reconciled,) = _events_of(events, "source_usage_reconciled")
    assert reconciled["returned"] == 2


def test_v5的400不重试_5xx与传输错两路都重试() -> None:
    from app.sources import douyin

    log: list[str] = []
    events: list[dict] = []
    result = _search(_router({"v5": [503, _v5_page(["1"])]}, log=log), events)
    assert [item["platform_item_id"] for item in result] == ["1"]
    assert log == ["v5", "v5"] and not _events_of(events, "source_search_fallback")

    calls: list[str] = []

    def flaky_transport(method, url, headers, body, timeout):
        if douyin._COMMENTS_PATH in url:
            return douyin.HttpResponse(200, {"code": 200, "data": {"comments": [], "has_more": 0, "cursor": 0}})
        calls.append("v5")
        if len(calls) == 1:
            raise douyin.TikHubError("transport", endpoint=url, detail="URLError")
        return douyin.HttpResponse(200, _v5_page(["t1"]))

    events = []
    result = _search(flaky_transport, events)
    assert [item["platform_item_id"] for item in result] == ["t1"] and calls == ["v5", "v5"]
    (retry,) = _events_of(events, "source_search_retry")
    assert retry["closed_reason"] == "tikhub_transport"


def test_凭证与限流不重试() -> None:
    log: list[str] = []
    events: list[dict] = []
    assert _search(_router({"v5": [401], "v4": [429]}, log=log), events) == []
    assert log == ["v5", "v4"], "401/429 再打一次也不会好，还会拿限流的时钟当节拍"
    assert not _events_of(events, "source_search_retry")
