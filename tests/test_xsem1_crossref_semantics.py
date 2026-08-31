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


# --- 条 3：等级回退从「整套平台基线」换成「四维实值 + 基线交叉分」 -------------------

from app.reliability.crossref import _grade  # noqa: E402


def _rated(platform: str, authority: int, freshness: int, completeness: int,
           independence: int) -> dict:
    return {
        "id": "ev-x", "platform": platform,
        "score_authority": authority, "score_freshness": freshness,
        "score_completeness": completeness, "score_independence": independence,
    }


def test_四维实值齐全时走四维先验等级():
    """web_search 基线总分 5 = C；四维打实后可以到 B/A，天花板被拆掉。"""
    assert _grade(_rated("web_search", 2, 2, 2, 2)) == "A"   # 8 + 基线交叉 1 = 9
    assert _grade(_rated("web_search", 1, 2, 2, 1)) == "B"   # 6 + 1 = 7
    assert _grade(_rated("web_search", 1, 1, 1, 1)) == "C"   # 4 + 1 = 5，与基线同


def test_四维先验也会下探不只上探():
    """X 平台基线 6 = B；讨论串取不全 + 权威未达 P75 的真实读数是 5 = C。"""
    assert _grade({"id": "ev-x", "platform": "x"}) == "B"    # 四维缺 → 整套基线 6
    assert _grade(_rated("x", 0, 2, 1, 2)) == "C"            # 5 + 基线交叉 0 = 5


@pytest.mark.parametrize("missing", [
    "score_authority", "score_freshness", "score_completeness", "score_independence",
])
def test_四维任缺一维仍回落整套平台基线(missing):
    item = _rated("hacker_news", 1, 1, 2, 2)
    item.pop(missing)
    assert _grade(item) == "B"          # hacker_news 基线总分 7 = B
    item[missing] = None
    assert _grade(item) == "B"


def test_五维齐全时优先走总分不走先验():
    item = _rated("web_search", 2, 2, 2, 2)
    item["score_crossref"] = 0
    assert _grade(item) == "A"          # 8 分，走第②级
    assert _grade({**item, "grade": "D"}) == "D"   # 第①级仍最优先


# --- 条 3 的不动点：四维实值在本轮才写上，也必须一次调用内收敛 --------------------

import asyncio  # noqa: E402
from pathlib import Path  # noqa: E402

from app.reliability.backfill import backfill_report  # noqa: E402
from app.reliability.claims import register_claims  # noqa: E402
from tests.test_c1_claims import (  # noqa: E402
    NeverAdapter, add_evidence, make_store, raw_claim, ref,
)


def test_条3_连跑四遍逐字段零差异(tmp_path: Path) -> None:
    """D-013 的尺子照搬并加到四遍：第一遍才评上分的路径也不许漂。"""

    store = make_store(tmp_path)
    evidence = (
        ("ev-hn1", "hacker_news", "https://news.ycombinator.com/item?id=901",
         "甲", "2026-08-01T00:00:00+00:00"),
        ("ev-ws1", "web_search", "https://openai.com/index/one",
         "乙", "2026-08-10T00:00:00+00:00"),
        ("ev-ws2", "web_search", "https://anthropic.com/news/two",
         "丙", "2026-08-20T00:00:00+00:00"),
    )
    for evidence_id, platform, permalink, author, published_at in evidence:
        add_evidence(
            store, "r-c1", evidence_id, platform=platform, permalink=permalink,
            author=author, published_at=published_at,
        )
    register_claims(store, "r-c1", [raw_claim("c-01", [
        ref(permalink, firsthand=True) for _, _, permalink, _, _ in evidence
    ])], source="chapter")

    def snapshot() -> dict:
        rows = {row["id"]: row for row in store.list_evidence("r-c1")}
        return {
            "evidence": {
                evidence_id: {
                    key: row[key] for key in (
                        "score_authority", "score_freshness", "score_crossref",
                        "score_completeness", "score_independence",
                        "score_total", "grade", "rating_notes", "rated_by",
                    )
                } | {"extra": row["extra"]}
                for evidence_id, row in rows.items()
            },
            "claims": store.get_report("r-c1")["extra"]["claims"],
        }

    rounds = []
    for _ in range(4):
        asyncio.run(backfill_report(
            store, "r-c1", adapter=NeverAdapter(), runs_root=tmp_path / "runs",
        ))
        rounds.append(snapshot())
    assert rounds[1] == rounds[0]
    assert rounds[2] == rounds[0]
    assert rounds[3] == rounds[0]


# --- 条 2：反证行按 ¬c 的支撑面结算（B-1 对称簇） ---------------------------------

def _side(evidence_id: str, permalink: str, *, against: bool, **overrides):
    item = _ev(evidence_id, permalink, **overrides)
    item["firsthand_by_claim"] = {"c-01": True}
    if against:
        item["stance_by_claim"] = {"c-01": "contradicts"}
    return item


def _三对三夹具(**overrides) -> list[dict]:
    """支撑三条、反证三条，各自跨平台跨域名跨作者，两面都能成 ≥2 簇。"""

    rows = [
        _side("ev-s1", "https://news.ycombinator.com/item?id=701", against=False,
              platform="hacker_news", author_name="甲",
              title="自测内存占用记录", published_at="2026-06-01T00:00:00Z"),
        _side("ev-s2", "https://openai-notes.com/a", against=False,
              author_name="乙", title="第三方横向评测",
              published_at="2026-03-05T00:00:00Z"),
        _side("ev-s3", "https://ithome-mirror.net/b", against=False,
              author_name="丙", title="长期使用体感汇总",
              published_at="2026-01-09T00:00:00Z"),
        _side("ev-c1", "https://news.ycombinator.com/item?id=702", against=True,
              platform="hacker_news", author_name="丁",
              title="同版本复测未复现", published_at="2026-06-02T00:00:00Z"),
        _side("ev-c2", "https://labs-review.org/c", against=True,
              author_name="戊", title="实验室台架数据",
              published_at="2026-02-11T00:00:00Z"),
        _side("ev-c3", "https://user-forum.net/d", against=True,
              author_name="己", title="社区大样本投票",
              published_at="2026-04-17T00:00:00Z"),
    ]
    for row in rows:
        row.update(overrides.get(row["id"], {}))
    return rows


def test_条2_反证行首次拿到交叉结论与簇数():
    result = build_claim_clusters(_三对三夹具(), "c-01")
    for evidence_id in ("ev-c1", "ev-c2", "ev-c3"):
        patch = result["evidence_extra"][evidence_id]
        assert patch["crossref_verdict"] in {"PASS", "WEAK", "SINGLE", "CONFLICT"}
        assert patch["crossref_n_clusters"] >= 1
        assert "c-01" in patch["claim_ids"]
    # 反证面的簇数是它自己那面的，不是支撑面的。
    assert result["evidence_extra"]["ev-c1"]["crossref_n_clusters"] == 3
    assert result["k"] == 3


def test_条2_两面互指且未说明分歧时双方都判CONFLICT():
    result = build_claim_clusters(_三对三夹具(), "c-01")
    assert result["verdict"] == "CONFLICT"
    assert result["score_crossref"] == 0
    for evidence_id in ("ev-s1", "ev-c1"):
        patch = result["evidence_extra"][evidence_id]
        assert patch["crossref_verdict"] == "CONFLICT"
    # 各自的 conflicts 指向对面，不指向自己那面。
    assert set(result["evidence_extra"]["ev-s1"]["crossref_conflicts"]) == {
        "ev-c1", "ev-c2", "ev-c3",
    }
    assert set(result["evidence_extra"]["ev-c1"]["crossref_conflicts"]) == {
        "ev-s1", "ev-s2", "ev-s3",
    }


def test_条2_正文说明分歧后两面同时降为WEAK():
    result = build_claim_clusters(_三对三夹具(), "c-01", conflict_explained=True)
    assert result["verdict"] == "WEAK"
    for evidence_id in ("ev-s1", "ev-s2", "ev-c1", "ev-c2"):
        assert result["evidence_extra"][evidence_id]["crossref_verdict"] == "WEAK"


def test_条2_只有一条反证时反证面判孤证():
    rows = [row for row in _三对三夹具() if row["id"] not in {"ev-c2", "ev-c3"}]
    result = build_claim_clusters(rows, "c-01")
    assert result["evidence_extra"]["ev-c1"]["crossref_n_clusters"] == 1
    # 支撑面等级 ≥B（HN 基线 7 = B），故反证面先落 CONFLICT 分支而不是 SINGLE。
    assert result["evidence_extra"]["ev-c1"]["crossref_verdict"] == "CONFLICT"
    rows_explained = [dict(row) for row in rows]
    explained = build_claim_clusters(rows_explained, "c-01", conflict_explained=True)
    assert explained["evidence_extra"]["ev-c1"]["crossref_verdict"] == "WEAK"


def test_条2_无反证时支撑面读数与改前逐字段相同():
    """存量语料反证行为 0，本条必须对支撑面零漂移。"""

    rows = [row for row in _三对三夹具() if row["id"].startswith("ev-s")]
    result = build_claim_clusters(rows, "c-01")
    # 顶层取主簇（ev-s1，HN）的视角：它看到的其他簇是 {C, C} → WEAK。这是 C-1
    # 记档的既有设计（每条只看「除自己簇之外」的佐证等级），不是本包引入的。
    assert (result["k"], result["verdict"], result["score_crossref"]) == (3, "WEAK", 1)
    assert result["evidence_extra"]["ev-s2"]["crossref_verdict"] == "PASS"
    assert result["clusters"] == ["cl-01", "cl-02", "cl-03"]
    for evidence_id in ("ev-s1", "ev-s2", "ev-s3"):
        patch = result["evidence_extra"][evidence_id]
        assert patch["crossref_conflicts"] == []
        assert patch["crossref_n_clusters"] == 3


def test_条2_反证行经补评拿到五维与grade(tmp_path: Path) -> None:
    """C-1 那条「反证行按既有语义拿不到交叉分」的挂账，验到库行上。"""

    store = make_store(tmp_path)
    urls = {
        "s1": "https://news.ycombinator.com/item?id=801",
        "s2": "https://openai-notes.com/x",
        "c1": "https://labs-review.org/y",
        "c2": "https://user-forum.net/z",
    }
    for key, platform, author, published in (
        ("s1", "hacker_news", "甲", "2026-08-01T00:00:00+00:00"),
        ("s2", "web_search", "乙", "2026-08-05T00:00:00+00:00"),
        ("c1", "web_search", "丙", "2026-08-09T00:00:00+00:00"),
        ("c2", "web_search", "丁", "2026-08-13T00:00:00+00:00"),
    ):
        add_evidence(
            store, "r-c1", f"ev-{key}", platform=platform, permalink=urls[key],
            author=author, published_at=published,
        )
    register_claims(store, "r-c1", [raw_claim("c-01", [
        ref(urls["s1"], firsthand=True), ref(urls["s2"], firsthand=True),
        ref(urls["c1"], firsthand=True, stance="contradicts"),
        ref(urls["c2"], firsthand=True, stance="contradicts"),
    ])], source="chapter")

    asyncio.run(backfill_report(
        store, "r-c1", adapter=NeverAdapter(), runs_root=tmp_path / "runs",
    ))

    rows = {row["id"]: row for row in store.list_evidence("r-c1")}
    for evidence_id in ("ev-c1", "ev-c2"):
        row = rows[evidence_id]
        assert row["extra"]["crossref_verdict"] is not None
        assert row["score_crossref"] is not None
        assert row["score_total"] is not None
        assert row["grade"] is not None          # 改前这三列对反证行恒 NULL
        assert row["extra"]["crossref_n_clusters"] == 2
