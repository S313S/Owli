"""§QUOTE-2：提示词里那两句「代表原声」也要挑真有人理的。

⛔ 这些用例锁的是**绿侧**。红侧（未改的代码上挑出的是零互动那两条）没法在这里
锁——它要的是旧代码。红侧读数落在 `docs/worklog/2026-09-09-quote2-representative-voice.md`。
"""

import re

from app.orchestrator.sectioning import _ugc_coding_digest


def _row(eid, likes, attitude, quote, platform="weibo"):
    """一行生产形状的证据：互动量在 `raw_metrics` 上，编码在 `extra.coding` 里。"""

    return {
        "id": eid, "platform": platform, "kind": "post",
        "raw_metrics": None if likes is None else {"likes": likes, "comments": 0},
        "extra": {"coding": {
            "coding_version": "v1", "attitude": attitude, "quote": quote,
            "topics": ["功能与能力"], "scenario": "日常", "audience": "开发者",
        }},
    }


def _digest_line(pool, attitude="负"):
    """跑生产函数取某一格那行；⛔ 不重实现挑法，重实现出来的尺子量的是它自己。"""

    rows = {eid: _row(eid, likes, att, quote) for eid, likes, att, quote in pool}
    items = [{"evidence_id": eid, "citation": f"[{eid}]"} for eid, *_ in pool]
    digest = _ugc_coding_digest(items, rows)
    assert digest is not None
    return next(
        line for line in digest.splitlines()
        if line.startswith(f"- {attitude}向代表原声")
    )


def test_有互动的顶掉零互动():
    """池里同时有零互动和有互动时，代表原声必须是有互动那两条。

    池序故意把零互动排在前面：旧代码「取前两条」会挑中 S01/S02。
    """

    line = _digest_line([
        ("S01", 0, "负", "零互动甲"),
        ("S02", 0, "负", "零互动乙"),
        ("S03", 120, "负", "有互动甲"),
        ("S04", 80, "负", "有互动乙"),
        ("S05", 5, "正", "凑够五条"),
    ])
    assert re.findall(r"\[S\d+\]", line) == ["[S03]", "[S04]"]
    # 有人理的那两条不该被扣标注——标注是给没人理的看的。
    assert "无人点赞或评论" not in line


def test_互动量高的排在前面():
    """同为有互动，档内按互动量降序，不按池序。"""

    line = _digest_line([
        ("S01", 3, "负", "少人理"),
        ("S02", 900, "负", "很多人理"),
        ("S03", 60, "负", "中等"),
        ("S04", 5, "正", "凑"),
        ("S05", 5, "正", "凑"),
    ])
    assert re.findall(r"\[S\d+\]", line) == ["[S02]", "[S03]"]


def test_全零互动不挖空但要带标注():
    """⛔ 零互动是降权不是排除。整格都是零互动时仍要给出原声。

    挖空这一格会被读成「没人这么说」，比一条冷门原声更误导（§QUOTE-1 拿真数据试过）。
    """

    line = _digest_line([
        ("Z01", 0, "负", "全零甲"),
        ("Z02", 0, "负", "全零乙"),
        ("Z03", 0, "负", "全零丙"),
        ("Z04", 0, "正", "凑"),
        ("Z05", 0, "正", "凑"),
    ])
    assert len(re.findall(r"\[Z\d+\]", line)) == 2
    assert line.count("无人点赞或评论") == 2


def test_取不到互动量与确实零分得开():
    """两者都排在有互动的后面，但「没这个数」和「确实没人理」标注上要分得开。"""

    line = _digest_line([
        ("N01", None, "负", "没数甲"),
        ("N02", 0, "负", "确实零"),
        ("N03", None, "负", "没数乙"),
        ("N04", 3, "正", "凑"),
        ("N05", 3, "正", "凑"),
    ])
    # 确实零的排在没数的前面（§QUOTE-1 的档位：有人理 > 零互动 > 没给互动数）。
    assert re.findall(r"\[N\d+\]", line) == ["[N02]", "[N01]"]
    assert "（无人点赞或评论）" in line
    assert "（该平台未提供互动数）" in line


def test_标注措辞与_quote1_逐字相同():
    """措辞是 import 来的不是抄的：抄一份会在它改词时悄悄分叉。"""

    from app.reliability import coding
    from app.orchestrator import sectioning

    assert sectioning.ENGAGEMENT_NOTES is coding.ENGAGEMENT_NOTES
    assert sectioning.engagement_tier is coding.engagement_tier
    assert sectioning._quote_sort_key is coding._quote_sort_key
