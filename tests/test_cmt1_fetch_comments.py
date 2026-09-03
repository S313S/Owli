"""§CMT-1 货 1：三源 fetch_comments 的统一形状与最小清洗。

返回体用真机录制的样本（`tests/fixtures/cmt1/`，2026-09-03 用真凭证打的），
不联网；调度已在提货单「已验事实」节记下两个端点真机通了。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

import pytest

from app.sources import comments as comment_shape
from app.sources import douyin, reddit, xhs


FIXTURES = Path(__file__).parent / "fixtures" / "cmt1"
XHS_NOTE = "69d0e65b000000001f003cdc"
XHS_PARENT = f"https://www.xiaohongshu.com/explore/{XHS_NOTE}"
UNIFIED_FIELDS = {
    "parent_permalink", "permalink", "author", "text", "likes",
    "published_at", "platform", "comment_id",
}


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_低价值评论按长度与纯表情丢弃() -> None:
    assert comment_shape.is_low_value("顶")
    assert comment_shape.is_low_value("[失望R]")
    assert comment_shape.is_low_value("。。。。。。")
    assert not comment_shape.is_low_value("[失望R]文案AI出的，看着有点难受")
    assert comment_shape.is_low_value("好用啊")  # 3 字，不足 4 字门槛
    assert not comment_shape.is_low_value("真的很好用")


def test_clean_丢弃计数与去重() -> None:
    def build(text: str, comment_id: str) -> comment_shape.Comment:
        return comment_shape.Comment(
            parent_permalink="https://example.com/p/1", permalink="",
            author="甲", text=text, likes=0, published_at=None,
            platform="xhs", comment_id=comment_id,
        )

    kept, dropped = comment_shape.clean(
        [build("顶", "c1"), build("真的好用", "c2"), build("真的好用", "c2")],
        limit=10,
    )
    assert [item.text for item in kept] == ["真的好用"]
    assert dropped == 1


def test_小红书评论按录制返回体给出统一形状() -> None:
    payload = _fixture("xhs_note_comments.json")
    seen: list[str] = []

    def http_get(url: str, headers: Mapping[str, str], timeout: float) -> Any:
        seen.append(url)
        return xhs.HttpResponse(status=200, payload=payload)

    batch = xhs.fetch_comments(
        XHS_NOTE, parent_permalink=XHS_PARENT, limit=20, token="tk",
        http_get=http_get, rate_gate=xhs.RateGate(0.0), max_pages=2,
    )
    assert batch.calls == 2 and seen[0].endswith(f"note_id={XHS_NOTE}")
    assert "cursor=" in seen[1]
    assert len(batch.comments) >= 1
    first = batch.comments[0]
    assert set(vars(first)) == UNIFIED_FIELDS
    assert first.platform == "xhs"
    assert first.parent_permalink == XHS_PARENT
    assert first.permalink == ""  # 小红书没有单条评论链接
    assert first.comment_id and first.author and first.text
    assert first.published_at is not None and first.published_at.endswith("+00:00")
    assert all(not comment_shape.is_low_value(item.text) for item in batch.comments)


def test_reddit_评论用_t1_id_拼出单条链接() -> None:
    payload = _fixture("prowlo_social_get_post.json")
    calls: list[tuple[str, Mapping[str, Any]]] = []

    class Client:
        def call(self, name: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
            calls.append((name, arguments))
            return payload

    parent = str(payload["post"]["permalink"])
    batch = reddit.fetch_comments(
        "t3_1vtabus", parent_permalink=parent, limit=20, client=Client(),
    )
    assert calls == [("social_get_post", {
        "platform": "reddit", "id": "t3_1vtabus",
        "includeComments": True, "commentLimit": 20,
    })]
    assert batch.calls == 1 and len(batch.comments) >= 1
    assert {frozenset(vars(item)) for item in batch.comments} == {frozenset(UNIFIED_FIELDS)}
    links = {item.permalink for item in batch.comments}
    assert len(links) == len(batch.comments)  # 每条评论一个独立链接
    for item in batch.comments:
        assert item.platform == "reddit"
        assert item.parent_permalink == parent
        assert item.comment_id.startswith("t1_")
        # 样本里父帖链接是 reddit.com、评论链接是 www.reddit.com——真机就这么漂，
        # 归一化交给 dao.normalize_permalink，这里只认「评论链接以自己的 t1 id 结尾」。
        assert item.permalink.rstrip("/").endswith(
            item.comment_id.removeprefix("t1_")
        )
        assert item.permalink != parent


def test_reddit_评论链接缺失时按父帖加评论_id_合成() -> None:
    composed = reddit._comment_permalink(
        {"id": "t1_abc123"}, "https://www.reddit.com/r/x/comments/1/y"
    )
    assert composed == "https://www.reddit.com/r/x/comments/1/y/abc123"


def test_抖音评论包成同一形状且沿用既有分页() -> None:
    pages = [
        {"comments": [
            {"cid": "c1", "text": "转录准确率确实高", "digg_count": 12,
             "create_time": 1777135519, "user": {"nickname": "甲"}},
            {"cid": "c2", "text": "顶", "digg_count": 0,
             "create_time": 1777135520, "user": {"nickname": "乙"}},
        ], "has_more": 1, "cursor": 20, "total": 3},
        {"comments": [
            {"cid": "c3", "text": "会议纪要还是飞书好用", "digg_count": 3,
             "create_time": 1777135530, "user": {"nickname": "丙"}},
        ], "has_more": 0, "cursor": 40, "total": 3},
    ]
    seen: list[str] = []

    def http_request(
        method: str, url: str, headers: Mapping[str, str],
        body: bytes | None, timeout: float,
    ) -> Any:
        seen.append(url)
        return douyin.HttpResponse(
            status=200, payload={"code": 200, "data": pages[len(seen) - 1]}
        )

    batch = douyin.fetch_comments(
        "7300000000000000000",
        parent_permalink="https://www.douyin.com/video/7300000000000000000",
        limit=20, token="tk", http_request=http_request,
        rate_gate=douyin.RateGate(0.0), max_pages=3,
    )
    assert batch.calls == 2 and batch.dropped_short == 1
    assert [item.text for item in batch.comments] == [
        "转录准确率确实高", "会议纪要还是飞书好用",
    ]
    assert {frozenset(vars(item)) for item in batch.comments} == {
        frozenset(UNIFIED_FIELDS)
    }
    assert batch.comments[0].platform == "douyin"
    assert batch.comments[0].likes == 12
    assert batch.comments[0].comment_id == "c1"
    assert batch.comments[0].permalink == ""


@pytest.mark.parametrize("bad", [0, -1, 101, True, "20"])
def test_limit_闭集拒绝非法值(bad: Any) -> None:
    with pytest.raises(ValueError):
        xhs.fetch_comments(XHS_NOTE, parent_permalink=XHS_PARENT, limit=bad, token="t")
