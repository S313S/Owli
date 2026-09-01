"""§M6-d 货 2：公众号池契约与「临时链必附正文快照」闸门。

这道闸是本包最要紧的一条判据，理由值得写在用例文件抬头：搜狗等补充面给的是
带 `timestamp+signature` 的临时链，几小时到一天就过期。它在**验收当天全绿**，
一周后报告里的角标全部点开是「参数错误」页——「链接可追溯」这条核心判据会在
交付之后才失效。快照是这类链唯一的兜底证据，所以缺快照必须当场红。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


PERMANENT = "https://mp.weixin.qq.com/s/AbCdEf123456"
TEMPORARY = (
    "https://mp.weixin.qq.com/s?src=11&timestamp=1788228000"
    "&ver=5000&signature=abcdefg&new=1"
)


def _write_batch(
    root: Path, batch_id: str, rows: list[dict], *,
    snapshots: dict[str, str] | None = None, manifest: dict | None = None,
) -> Path:
    directory = root / "wechat_mp" / batch_id
    jsonl_dir = directory / "wechat_mp" / "jsonl"
    jsonl_dir.mkdir(parents=True, exist_ok=True)
    for relative, html in (snapshots or {}).items():
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(html, encoding="utf-8")
    payload = manifest if manifest is not None else {
        "platform": "wechat_mp", "batch_id": batch_id, "status": "ok",
        "keywords": ["茶叶"], "collected_at": "2026-09-02T02:00:00Z",
        "item_count": len(rows),
    }
    (directory / "manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    with (jsonl_dir / "search_contents_2026-09-02.jsonl").open(
        "w", encoding="utf-8"
    ) as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return directory


def _row(article_id: str, *, url: str = "", **overrides):
    row = {
        "article_id": article_id,
        "url": url or f"https://mp.weixin.qq.com/s/{article_id}",
        "title": "2026 年中国茶叶消费趋势观察",
        "content": "今年茶叶线上销售同比增长两成，" * 20,
        "account_name": "茶业复兴",
        "account_biz": "MzA5NjE4MDAwMA==",
        "publish_time": 1788228000,
        "source_keyword": "茶叶",
        "read_count": None,
        "like_count": None,
    }
    row.update(overrides)
    return row


def test_固定链行按契约映射且不要求快照(tmp_path: Path) -> None:
    from app.precollect import load_evidence

    _write_batch(tmp_path, "20260902-1000-茶叶", [_row("AbCdEf123456")])
    result = load_evidence("wechat_mp", root=tmp_path)

    assert len(result.items) == 1
    item = result.items[0]
    assert item["platform"] == "wechat_mp"
    assert item["source_type"] == "article"
    assert item["permalink"] == PERMANENT
    assert item["author_name"] == "茶业复兴"
    assert item["source_keyword"] == "茶叶"
    assert item["published_at"] and item["published_at"].endswith("Z")
    # 公众号是署名长文，不是网友短讯——内容类型与微博有意不同。
    assert item["extra"]["content_kind"] == "industry_view"
    assert item["extra"]["permalink_kind"] == "permanent"
    assert item["extra"]["content_snapshot"] is None
    assert item["extra"]["provider"] == "owli_precollect"
    assert item["fetch_method"] == "browser_agent"
    # 基线 1/1/0/1/1 = 4，问 platforms.py 不在源里手抄。
    assert item["score_authority"] == 1 and item["score_crossref"] == 0


def test_临时链缺快照当场拒收(tmp_path: Path) -> None:
    from app.precollect import PoolContractError, load_evidence

    _write_batch(tmp_path, "20260902-1001-茶叶", [_row("t1", url=TEMPORARY)])
    with pytest.raises(PoolContractError, match="必须附正文快照"):
        load_evidence("wechat_mp", root=tmp_path)


def test_临时链快照写了路径但文件不在批次目录里也拒收(tmp_path: Path) -> None:
    """路径不是证据。写了个不存在的路径与没写，后果一模一样。"""

    from app.precollect import PoolContractError, load_evidence

    _write_batch(
        tmp_path, "20260902-1002-茶叶",
        [_row("t2", url=TEMPORARY, snapshot_path="snapshots/t2.html")],
    )
    with pytest.raises(PoolContractError, match="不在批次目录里"):
        load_evidence("wechat_mp", root=tmp_path)


def test_临时链带落盘快照才收下且路径相对批次目录(tmp_path: Path) -> None:
    from app.precollect import load_evidence

    _write_batch(
        tmp_path, "20260902-1003-茶叶",
        [_row("t3", url=TEMPORARY, snapshot_path="snapshots/t3.html")],
        snapshots={"snapshots/t3.html": "<html>正文快照</html>"},
    )
    item = load_evidence("wechat_mp", root=tmp_path).items[0]

    assert item["permalink"] == TEMPORARY
    assert item["extra"]["permalink_kind"] == "temporary"
    # 相对路径：池会随定容清理搬走，绝对路径将来只会指向一个不存在的地方。
    assert item["extra"]["content_snapshot"] == "snapshots/t3.html"


def test_采集分层写错闭集当场报错而不是等sqlite拒收整批(tmp_path: Path) -> None:
    from app.precollect import PoolContractError, load_evidence

    _write_batch(
        tmp_path, "20260902-1004-茶叶",
        [_row("f1", fetch_method="wechat_client")],
    )
    with pytest.raises(PoolContractError, match="不在采集分层闭集"):
        load_evidence("wechat_mp", root=tmp_path)


def test_行内可覆盖采集分层因为公众号有两条发现路(tmp_path: Path) -> None:
    """搜一搜走客户端、搜狗补充面走 curl，两条路照实写，不替它猜一个。"""

    from app.precollect import load_evidence

    _write_batch(
        tmp_path, "20260902-1005-茶叶",
        [_row("f2", fetch_method="media_crawler")],
    )
    assert load_evidence("wechat_mp", root=tmp_path).items[0][
        "fetch_method"] == "media_crawler"


def test_公众号没有可比互动量整批走无指标归一化而不是编个零(tmp_path: Path) -> None:
    from app.precollect import load_evidence
    from app.reliability.scoring import normalize_evidence_metrics

    _write_batch(
        tmp_path, "20260902-1006-茶叶",
        [_row(f"n{i}") for i in range(3)],
    )
    items = load_evidence("wechat_mp", root=tmp_path).items
    normalized = normalize_evidence_metrics(
        items, computed_at="2026-09-02T02:00:00Z",
        report_id="r-1", goal_id="goal-1", queries=["茶叶"], filters="t",
    )

    assert {item["norm_method"] for item in normalized} == {"none"}
    assert {item["normalized_score"] for item in normalized} == {None}
    assert {item["norm_context"]["reason"] for item in normalized} == {
        "no_metric_available"}
    assert {item["norm_context"]["metric"] for item in normalized} == {None}


def test_有真标题就用真标题不拿正文前八十字充数(tmp_path: Path) -> None:
    """报告角标与 Excel 那一列印的就是这个字段；拿正文开头充数会全是半句话。"""

    from app.precollect import load_evidence

    _write_batch(tmp_path, "20260902-1007-茶叶", [_row("h1")])
    item = load_evidence("wechat_mp", root=tmp_path).items[0]

    assert item["title"] == "2026 年中国茶叶消费趋势观察"
    assert item["content_excerpt"].startswith("今年茶叶线上销售")


def test_没有标题字段的平台仍退回正文开头(tmp_path: Path) -> None:
    from app.precollect import load_evidence

    _write_batch(tmp_path, "20260902-1008-茶叶", [_row("h2", title="")])
    item = load_evidence("wechat_mp", root=tmp_path).items[0]

    assert item["title"].startswith("今年茶叶线上销售")


LONG_PERMANENT = (
    "https://mp.weixin.qq.com/s?__biz=MjM5NDEyNjQxMQ%3D%3D"
    "&mid=2650768050&idx=1&sn=6031caaffd1603b974cfa419dfcd7ca5"
)


def test_永久链两种形态都不要快照临时链才要() -> None:
    """公众号永久链有两种：短链 /s/xxx 与长链 /s?__biz=&mid=&sn=。

    长链是通用搜索引擎收录得最多的形态，它同样不会过期——早先只认短链，
    等于对一大批本来永久的链白要快照，还会把它们标成 temporary 误导下游。
    会过期的只有带 signature 的临时签名链。
    """

    import re

    from app.precollect import PLATFORM_PROFILES

    pattern = re.compile(PLATFORM_PROFILES["wechat_mp"].permanent_permalink_pattern)
    assert pattern.match(PERMANENT)
    assert pattern.match(PERMANENT + "?mpshare=1&scene=1")
    assert pattern.match(LONG_PERMANENT)
    assert not pattern.match(TEMPORARY)
    assert not pattern.match(LONG_PERMANENT + "&signature=xx")


def test_长永久链不要求快照(tmp_path: Path) -> None:
    from app.precollect import load_evidence

    _write_batch(tmp_path, "20260902-1009-茶叶", [_row("L1", url=LONG_PERMANENT)])
    item = load_evidence("wechat_mp", root=tmp_path).items[0]

    assert item["extra"]["permalink_kind"] == "permanent"
    assert item["extra"]["content_snapshot"] is None
