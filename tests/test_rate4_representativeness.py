"""§RATE-4 货 1：UGC 行改用「代表性」维，非 UGC 行分值逐条不变。"""

from __future__ import annotations

import pytest

from app.reliability.scoring import (
    REPRESENTATIVENESS_MIN_POOL,
    engagement_percentiles,
    engagement_value,
    rating_notes_problem,
    score_evidence,
    score_evidence_partial,
)


def _row(index: int, engagement: int, **overrides):
    extra = {
        "content_kind": "user_opinion",
        "authority_kind": "anonymous_or_unverifiable",
        "interest_relation": "arms_length",
    }
    extra.update(overrides.pop("extra", {}))
    row = {
        "id": f"e-{index:03d}",
        "platform": "xhs",
        "raw_metrics": {
            "liked_count": engagement, "comments_count": 0, "collected_count": 0,
        },
        "published_at": "2026-08-01T00:00:00Z",
        "fetched_at": "2026-09-01T00:00:00Z",
        "content_excerpt": "用起来还行" * 6,
        "has_body": True,
        "author_name": "某用户",
        "permalink_reachable": True,
        "extra": extra,
    }
    row.update(overrides)
    return row


def _batch(size: int = 100):
    """互动量 0..size-1，分位由 count(x<v)/(n-1) 定，分布正好 10/30/60。"""

    return [_row(index, index) for index in range(size)]


def test_percentile_bands_split_batch_10_30_60():
    rows = _batch()
    percentiles = engagement_percentiles(rows)
    assert len(percentiles) == 100
    scored = [
        score_evidence({**row, "engagement_percentile": percentiles[row["id"]]})
        for row in rows
    ]
    buckets = {2: 0, 1: 0, 0: 0}
    for result in scored:
        buckets[result["score_authority"]] += 1
    assert buckets == {2: 10, 1: 30, 0: 60}
    assert scored[-1]["rating_notes"].startswith("代表性2:P100")
    assert rating_notes_problem(scored[-1]["rating_notes"], scored[-1]) is None


def test_small_pool_falls_back_to_authority_closed_set():
    rows = _batch(REPRESENTATIVENESS_MIN_POOL - 1)
    assert engagement_percentiles(rows) == {}
    result = score_evidence(rows[-1])
    assert result["score_authority"] == 0
    assert result["rating_notes"].startswith("权威0:作者不可核验")


def test_missing_raw_metrics_falls_back_per_row():
    rows = _batch()
    rows[0]["raw_metrics"] = {}
    percentiles = engagement_percentiles(rows)
    assert rows[0]["id"] not in percentiles
    assert engagement_value(rows[0]) is None
    assert score_evidence(rows[0])["rating_notes"].startswith("权威0:")


def test_non_ugc_rows_score_identically_to_before():
    """非 user_opinion 行：喂不喂分位，五维与理由都逐条相等。"""

    kinds = ("product_launch", "market_data", "industry_view", "reference", None)
    authorities = ("first_party_official", "named_secondary", "content_farm")
    for content_kind in kinds:
        for authority_kind in authorities:
            extra = {"authority_kind": authority_kind}
            extra["content_kind"] = content_kind
            row = _row(1, 999, extra=extra)
            if content_kind is None:
                row["extra"].pop("content_kind")
            baseline = score_evidence(row)
            with_pct = score_evidence({**row, "engagement_percentile": 0.99})
            assert with_pct == baseline, (content_kind, authority_kind)


def test_content_farm_stays_zero_even_at_top_percentile():
    row = _row(1, 999, extra={"authority_kind": "content_farm"})
    result = score_evidence({**row, "engagement_percentile": 1.0})
    assert result["score_authority"] == 0
    assert result["rating_notes"].startswith("权威0:内容农场")


def test_undisclosed_interest_still_costs_the_independence_point():
    row = _row(1, 999, extra={"interest_relation": "undisclosed_interest"})
    result = score_evidence({**row, "engagement_percentile": 1.0})
    assert result["score_independence"] == 0
    assert result["score_authority"] == 2


def test_comments_pool_apart_from_posts():
    posts = _batch()
    comments = [
        {
            **_row(500 + index, 0),
            "id": f"c-{index:03d}",
            "kind": "comment",
            "raw_metrics": {"likes": index},
        }
        for index in range(REPRESENTATIVENESS_MIN_POOL)
    ]
    percentiles = engagement_percentiles(posts + comments)
    assert percentiles["c-019"] == 1.0
    assert percentiles["e-099"] == 1.0
    top = score_evidence({**comments[-1], "engagement_percentile": 1.0})
    assert top["rating_notes"].startswith("代表性2:评论·P100")


def test_partial_keeps_the_representativeness_label():
    rows = _batch()
    percentiles = engagement_percentiles(rows)
    result = score_evidence_partial(
        {**rows[-1], "engagement_percentile": percentiles[rows[-1]["id"]]},
        missing_dimensions={"score_crossref": "缺断言血缘簇"},
    )
    assert result["rating_notes"].startswith("代表性2:P100")
    assert result["score_crossref"] is None
    assert rating_notes_problem(result["rating_notes"], result) is None


def test_percentile_out_of_range_is_rejected():
    with pytest.raises(ValueError):
        score_evidence({**_row(1, 5), "engagement_percentile": 1.5})
