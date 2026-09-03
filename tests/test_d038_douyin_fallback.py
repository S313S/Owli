"""§D-038：抖音搜索 v5 主路 / v4 兜底，两套字段映射归一。

录制返回体照 2026-09-03 真机样本（v5 `request_id 25de759c…`、v4 `9e968418…`）的
形状裁剪：v5 是 `data.items[].aweme_info.*` snake_case、秒级 `create_time`；
v4 是 `data.data.itemList[].AwemeInfo.*` PascalCase、**毫秒** `CreateTime`，
且 `Statistics` 没有 `CollectCount`、`Author` 没有 `Uid`。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest


class ImmediateGate:
    def wait(self) -> None:
        return None


_NOW = lambda: datetime(2026, 9, 3, tzinfo=timezone.utc)  # noqa: E731


def _v5_page(ids: list[str], *, has_more: int = 0) -> dict:
    return {
        "code": 200,
        "data": {
            "items": [
                {
                    "type": 1,
                    "aweme_info": {
                        "aweme_id": item_id,
                        "desc": f"豆包实测 {item_id}",
                        "create_time": 1775107082,
                        "author": {
                            "uid": "2499659671545672", "sec_uid": "MS4wLj",
                            "nickname": "灵GO", "is_verified": True,
                        },
                        "statistics": {
                            "digg_count": 10, "comment_count": 0,
                            "share_count": 1, "collect_count": 7,
                        },
                    },
                }
                for item_id in ids
            ],
            "pagination": {
                "search_id": "2026090319", "offset": 10, "cursor": 10,
                "backtrace": "cDjndU==", "has_more": has_more, "next_page": 2,
            },
        },
    }


def _v4_page(ids: list[str], *, has_more: bool = False, cursor: int = 12) -> dict:
    return {
        "code": 200,
        "data": {
            "code": 0,
            "msg": "success",
            "data": {
                "hasMore": has_more,
                "cursor": cursor,
                "itemList": [
                    {
                        "AwemeInfo": {
                            "AwemeId": item_id,
                            "Desc": f"抖音怎么找豆包 {item_id}",
                            "CreateTime": "1782789498000",  # 真机是字符串毫秒
                            "Author": {
                                "Nickname": "科技老王", "ShortId": "47588417600",
                                "SecUid": "MS4wLjA", "FollowerCount": 145238,
                            },
                            "Statistics": {
                                "AwemeId": "", "CommentCount": 195,
                                "DiggCount": 132, "ShareCount": 24,
                            },
                        },
                        "ItemSource": 0, "IsAd": False,
                    }
                    for item_id in ids
                ],
            },
        },
    }


def _router(routes: dict[str, list[dict]], *, log: list[str] | None = None):
    """按端点回放录制返回体；同一端点按调用次序逐页给。"""

    from app.sources import douyin

    counters: dict[str, int] = {}

    def http_request(method, url, headers, body, timeout):
        if douyin._COMMENTS_PATH in url:
            return douyin.HttpResponse(200, {"code": 200, "data": {
                "comments": [], "has_more": 0, "cursor": 0,
            }})
        key = "v4" if douyin._SEARCH_PATH_V4 in url else "v5"
        if log is not None:
            log.append(key)
        index = counters.get(key, 0)
        counters[key] = index + 1
        pages = routes.get(key)
        if pages is None:
            raise AssertionError(f"未预期的端点调用：{url}")
        page = pages[min(index, len(pages) - 1)]
        if isinstance(page, int):  # 用状态码表示这一路直接失败
            return douyin.HttpResponse(page, {"code": page, "message": "boom"})
        return douyin.HttpResponse(200, page)

    return http_request


def _search(http_request, events: list[dict], **kwargs):
    from app.sources import douyin

    return douyin.search(
        "豆包", limit=kwargs.pop("limit", 10), comment_video_limit=1,
        token="t", http_request=http_request, rate_gate=ImmediateGate(),
        on_event=events.append, now=_NOW, **kwargs,
    )


def test_v5_正常时不碰兜底路() -> None:
    """主路够用就别打 v4——兜底是保险不是并联，多打一次是白花钱。"""

    log: list[str] = []
    events: list[dict] = []
    result = _search(_router({"v5": [_v5_page(["1", "2"])]} , log=log), events)

    assert [item["platform_item_id"] for item in result] == ["1", "2"]
    assert log == ["v5"]
    assert not [e for e in events if e["type"] == "source_search_fallback"]
    (reconciled,) = [e for e in events if e["type"] == "source_usage_reconciled"]
    assert reconciled["data"]["search_version"] == "v5"
    assert reconciled["data"]["calls"]["video_search_v4"] == 0


def test_v5_恒400时自动回退v4并留痕() -> None:
    """回归哨：2026-09-03 傍晚 v5 对任何参数都 400（含官方演示词）。

    那一次抖音整轮 0 产出。现在同样的 400 必须换来 v4 的数据，且**留下事件**——
    兜底路平时不走，不留痕就等于没人知道它走过、也没人知道主路病了。
    """

    log: list[str] = []
    events: list[dict] = []
    result = _search(
        _router({"v5": [400], "v4": [_v4_page(["a1", "a2"])]}, log=log), events,
    )

    assert [item["platform_item_id"] for item in result] == ["a1", "a2"]
    assert log == ["v5", "v4"]
    (fallback,) = [e for e in events if e["type"] == "source_search_fallback"]
    assert fallback["data"]["from_version"] == "v5"
    assert fallback["data"]["to_version"] == "v4"
    assert fallback["data"]["closed_reason"] == "tikhub_http_400"
    assert fallback["data"]["endpoint"].endswith("fetch_video_search_v5")
    (reconciled,) = [e for e in events if e["type"] == "source_usage_reconciled"]
    assert reconciled["data"]["search_version"] == "v4"
    assert reconciled["data"]["calls"] == {
        "video_search_v5": 1, "video_search_v4": 1, "video_comments": 1,
    }


def test_v4字段映射归一到v5形态() -> None:
    """PascalCase 嵌套要落成与 v5 同一张证据，下游不必知道走的哪版。"""

    events: list[dict] = []
    (item,) = _search(_router({"v5": [400], "v4": [_v4_page(["7657022587453783921"])]}), events)

    assert item["platform"] == "douyin"
    assert item["platform_item_id"] == "7657022587453783921"
    assert item["permalink"] == "https://www.douyin.com/video/7657022587453783921"
    assert item["author_name"] == "科技老王"
    assert item["author_meta"]["sec_uid"] == "MS4wLjA"
    # v4 没有 Uid，退用 ShortId；没有认证位，按 False 不编造。
    assert item["author_meta"]["uid"] == "47588417600"
    assert item["author_meta"]["verified"] is False
    assert item["raw_metrics"]["digg_count"] == 132
    assert item["raw_metrics"]["comments_count"] == 195
    assert item["raw_metrics"]["share_count"] == 24
    # v4 不给收藏数，落 0 而不是猜一个。
    assert item["raw_metrics"]["collect_count"] == 0
    assert "抖音怎么找豆包" in item["title"]
    assert item["published_at"].startswith("2026-")


def test_v4的毫秒时间戳不会算到公元五十八万年() -> None:
    """v5 给秒、v4 给毫秒，同一个 `_published_at` 直接吃会差三个数量级。"""

    from app.sources import douyin

    assert douyin._published_at(1775107082).startswith("2026-")
    assert douyin._published_at(1782789498000).startswith("2026-")
    # 真机 v4 给的是字符串；只认数字的话整列 published_at 会静默变 None。
    assert douyin._published_at("1782789498000").startswith("2026-")
    assert douyin._published_at("not-a-time") is None
    assert douyin._published_at(0) is None
    assert douyin._published_at(None) is None


def test_v4按cursor翻页且两页不重复() -> None:
    """v4 翻页靠 `hasMore` + `cursor`，没有 v5 的 search_id/backtrace。"""

    log: list[str] = []
    events: list[dict] = []
    result = _search(
        _router({
            "v5": [400],
            "v4": [
                _v4_page(["b1", "b2"], has_more=True, cursor=12),
                _v4_page(["b3", "b4"], has_more=False),
            ],
        }, log=log),
        events, limit=4,
    )

    assert [item["platform_item_id"] for item in result] == ["b1", "b2", "b3", "b4"]
    assert log == ["v5", "v4", "v4"]


def test_v4游标不前进就停别原地打转() -> None:
    """上游把同一个 cursor 回给我们时，继续翻只会拿到同一页并烧钱。"""

    log: list[str] = []
    events: list[dict] = []
    result = _search(
        _router({
            "v5": [400],
            "v4": [_v4_page(["c1"], has_more=True, cursor=0)],
        }, log=log),
        events, limit=10,
    )

    assert [item["platform_item_id"] for item in result] == ["c1"]
    assert log == ["v5", "v4"]


def test_v4空页不再翻也不报错() -> None:
    """搜不到东西是「0 条」，不是「源坏了」——别把空结果算成不可用。"""

    events: list[dict] = []
    assert _search(_router({"v5": [400], "v4": [_v4_page([], has_more=True)]}), events) == []
    assert not [e for e in events if e["type"] == "source_unavailable"]


def test_两路都断才算源不可用且主路死因是头条() -> None:
    """SRC-1 的契约不能被兜底吃掉：报出来的仍是主路的分诊，v4 的挂在旁边。"""

    from app.sources import douyin

    events: list[dict] = []
    assert _search(_router({"v5": [503], "v4": [429]}), events) == []
    (event,) = [e for e in events if e["type"] == "source_unavailable"]
    assert event["data"]["closed_reason"] == "tikhub_http_5xx"
    assert event["data"]["endpoint"] == douyin._SEARCH_PATH_V5
    assert event["data"]["fallback_closed_reason"] == "tikhub_http_429"
    assert event["data"]["fallback_endpoint"] == douyin._SEARCH_PATH_V4


def test_v4返回体缺itemList按坏响应处理() -> None:
    events: list[dict] = []
    http_request = _router({"v5": [400], "v4": [{"code": 200, "data": {"data": {}}}]})
    assert _search(http_request, events) == []
    (event,) = [e for e in events if e["type"] == "source_unavailable"]
    assert event["data"]["fallback_closed_reason"] == "tikhub_bad_response"
