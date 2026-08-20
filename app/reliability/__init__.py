"""Owli 五维可靠度与断言级交叉验证纯函数。"""

from .crossref import build_claim_clusters, independence_checks
from .scoring import (
    PLATFORM_BASELINES,
    claim_support_is_valid,
    grade_for_total,
    normalize_evidence_metrics,
    rating_notes_problem,
    score_evidence,
)

__all__ = [
    "PLATFORM_BASELINES",
    "build_claim_clusters",
    "claim_support_is_valid",
    "grade_for_total",
    "independence_checks",
    "normalize_evidence_metrics",
    "rating_notes_problem",
    "score_evidence",
]
