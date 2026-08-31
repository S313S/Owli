"""§XSEM-1 交叉验证维语义：空作者名、四维先验等级、反证行对称簇。"""

from __future__ import annotations

import pytest

from app.reliability.crossref import build_claim_clusters, independence_checks


def _ev(evidence_id: str, permalink: str, **overrides):
    item = {
        "id": evidence_id,
        "platform": "web_search",
        "permalink": permalink,
        "title": f"{evidence_id} 独立标题内容",
        "published_at": "2026-06-01T00:00:00Z",
    }
    item.update(overrides)
    return item


# --- 条 4：作者名缺失不再等价于「同一作者主体」 ---------------------------------

@pytest.mark.parametrize(
    "left_author, right_author, expected",
    [
        (None, None, True),          # 两边都不知道作者：不表态，按通过计
        ("柜体漫谈", None, True),      # 一边有名一边无名：同上
        ("Jon Bitner", "柜体漫谈", True),   # 两边具名且不等：独立
        ("柜体漫谈", "柜体漫谈", False),     # 两边具名且相等：同一主体
        ("柜体漫谈", " 柜体漫谈 ", False),   # 规范化后相等
    ],
)
def test_author_subject_treats_missing_name_as_unknown(left_author, right_author, expected):
    left = _ev("ev-1", "https://alpha-site.com/p1", author_name=left_author)
    right = _ev("ev-2", "https://beta-site.org/p2", author_name=right_author)
    checks = independence_checks(left, right, "c-01")
    assert checks["author_subject"] is expected


def test_missing_author_same_domain_still_one_cluster():
    """机构主体项仍然守着同域名：放开作者项不会让同一站点凑出多簇。"""
    items = [
        _ev("ev-1", "https://alpha-site.com/p1", author_name=None),
        _ev("ev-2", "https://alpha-site.com/p2", author_name=None),
    ]
    for item in items:
        item["firsthand_by_claim"] = {"c-01": True}
    result = build_claim_clusters(items, "c-01")
    assert result["k"] == 1
    assert result["verdict"] == "SINGLE"


def test_missing_author_cross_domain_forms_second_cluster():
    """跨注册域名、都无作者名 → §2.1 要求的「天然分属不同簇」。"""
    items = [
        _ev("ev-1", "https://alpha-site.com/p1", author_name=None,
            title="扫地机吸力实测记录"),
        _ev("ev-2", "https://beta-site.org/p2", author_name=None,
            title="洗衣机能耗横向对比"),
    ]
    for item in items:
        item["firsthand_by_claim"] = {"c-01": True}
    result = build_claim_clusters(items, "c-01")
    assert result["k"] == 2
