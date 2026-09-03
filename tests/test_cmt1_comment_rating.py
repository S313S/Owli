"""§CMT-1 货 4：评论行的评级语义（firsthand 起评、rating_notes 标「评论」、
同父帖的 contradict 边）。"""

from __future__ import annotations

from typing import Any

from app.reliability.backfill import _claim_crossref_item
from app.reliability.crossref import build_claim_clusters
from app.reliability.scoring import (
    is_comment_row, score_evidence, score_evidence_partial,
)


PARENT = "https://www.xiaohongshu.com/explore/note-1?xsec_token=t"


def _comment(index: int = 1, **overrides: Any) -> dict[str, Any]:
    row = {
        "id": f"ev-c{index}",
        "platform": "xhs",
        "kind": "comment",
        "source_type": "comment",
        "parent_permalink": PARENT,
        "permalink": f"{PARENT}&owli_comment=c{index}",
        "author_name": f"读者{index}",
        "content_excerpt": "用下来转录准确率一般，会议里人一多就串行",
        "published_at": "2026-09-01T00:00:00+00:00",
        "extra": {
            "content_kind": "user_opinion",
            "authority_kind": "anonymous_or_unverifiable",
            "interest_relation": "arms_length",
            "comment_of": PARENT,
            "thread_key": PARENT,
        },
    }
    row.update(overrides)
    return row


def test_是不是评论行_按_kind_判_旧行回落_source_type() -> None:
    assert is_comment_row({"kind": "comment"})
    assert not is_comment_row({"kind": "post", "source_type": "comment"})
    assert is_comment_row({"source_type": "comment"})
    assert not is_comment_row({"platform": "xhs"})


def test_评论行的评分理由带评论字样() -> None:
    notes = score_evidence(_comment())["rating_notes"]
    assert "评论" in notes
    assert notes.startswith("权威0:评论·")


def test_诚实缺失换掉权威那段也保住评论标记() -> None:
    notes = score_evidence_partial(
        _comment(), missing_dimensions={"score_authority": "作者信息缺失"},
    )["rating_notes"]
    assert "评论" in notes


def test_帖子行不会被误标成评论() -> None:
    post = _comment(kind="post", source_type="post", parent_permalink=None)
    assert "评论" not in score_evidence(post)["rating_notes"]


def test_写手没登记_firsthand_时评论按用户直述起评() -> None:
    claim = {"id": "c-0101"}
    item = _claim_crossref_item(_comment(), claim)
    assert item["firsthand_by_claim"] == {"c-0101": True}

    post = _comment(kind="post", source_type="post", parent_permalink=None)
    assert _claim_crossref_item(post, claim)["firsthand_by_claim"] == {"c-0101": False}


def test_写手显式登记了_firsthand_就以写手为准() -> None:
    claim = {"id": "c-0101", "firsthand": ["ev-other"]}
    item = _claim_crossref_item(_comment(), claim)
    assert item["firsthand_by_claim"] == {"c-0101": False}


def _post_row() -> dict[str, Any]:
    return {
        "id": "ev-p1", "platform": "xhs", "kind": "post",
        "permalink": PARENT, "author_name": "博主",
        "content_excerpt": "这款会议助手转录准确率很高，强烈推荐",
        "published_at": "2026-08-30T00:00:00+00:00",
        "extra": {"content_kind": "user_opinion", "thread_key": PARENT},
    }


def test_评论与父帖观点相反时成为反证簇() -> None:
    claim = {
        "id": "c-0101",
        "stance": {"ev-c1": "contradicts"},
        "firsthand": ["ev-p1", "ev-c1"],
    }
    items = [
        _claim_crossref_item(_post_row(), claim),
        _claim_crossref_item(_comment(), claim),
    ]
    result = build_claim_clusters(items, "c-0101")
    extra = result["evidence_extra"]
    # contradict 边落在评论行的 crossref_conflicts 上，指回它的父帖
    assert extra["ev-c1"]["crossref_conflicts"] == ["ev-p1"]
    assert extra["ev-p1"]["crossref_conflicts"] == []


def test_评论与父帖不算互相独立的两个信源() -> None:
    """评论的 parent_permalink 指向父帖 → crossref 的血缘检查认得出，
    父帖 + 它自己的评论并成一簇，不会被当成两条独立佐证。"""

    claim = {"id": "c-0101", "firsthand": ["ev-p1", "ev-c1"]}
    items = [
        _claim_crossref_item(_post_row(), claim),
        _claim_crossref_item(_comment(), claim),
    ]
    result = build_claim_clusters(items, "c-0101")
    assert result["k"] == 1


def test_同一帖下多条评论受每线程选簇上限约束() -> None:
    claim = {"id": "c-0101", "firsthand": [f"ev-c{i}" for i in range(1, 6)]}
    items = [_claim_crossref_item(_comment(i), claim) for i in range(1, 6)]
    result = build_claim_clusters(items, "c-0101")
    assert result["k"] <= 2  # crossref 的 thread_counts 上限
