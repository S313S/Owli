"""五维可靠度、分级、评分理由与平台内归一化纯函数。"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence


from app.platforms import baselines as platform_baselines
from app.platforms import primary_metrics as platform_primary_metrics

SCORE_FIELDS = (
    "score_authority",
    "score_freshness",
    "score_crossref",
    "score_completeness",
    "score_independence",
)

#: 第七张手抄表已收编（§M6-b 货 6）：数值只在 `app/platforms.py` 写一次，
#: 这里保留原名与原形状，所有消费点（scoring:240 / audit:98 / validation:957 /
#: crossref:221）一行不用改。缺键仍回落 web_search，语义不变。
PLATFORM_BASELINES: dict[str, dict[str, int]] = platform_baselines()

AUTHORITY_SCORES = {
    "first_party_official": 2,
    "verified_principal": 2,
    "institutional_primary": 2,
    "named_secondary": 1,
    "community_high_signal": 1,
    "anonymous_or_unverifiable": 0,
    "content_farm": 0,
}
AUTHORITY_REASONS = {
    "first_party_official": "主体官方域名",
    "verified_principal": "认证议题当事方",
    "institutional_primary": "具名机构一手披露",
    "named_secondary": "具名二手来源",
    "community_high_signal": "社区高信号作者",
    "anonymous_or_unverifiable": "作者不可核验",
    "content_farm": "内容农场",
}

INTEREST_SCORES = {
    "arms_length": 2,
    "disclosed_interest": 1,
    "undisclosed_interest": 0,
}
INTEREST_REASONS = {
    "arms_length": "无可见利益关系",
    "disclosed_interest": "利益关系已披露",
    "undisclosed_interest": "利益关系未披露",
}

FRESHNESS_WINDOWS = {
    "product_launch": (90, 365),
    "market_data": (30, 180),
    "user_opinion": (180, 730),
    "industry_view": (365, 1095),
}

CROSSREF_SCORES = {"PASS": 2, "WEAK": 1, "SINGLE": 0, "CONFLICT": 0}
CROSSREF_REASONS = {
    "PASS": "独立强源佐证",
    "WEAK": "弱源或已说明分歧",
    "SINGLE": "该断言仅一簇",
    "CONFLICT": "存在未说明反证",
}

RATING_NOTES_PATTERN = re.compile(
    r"^权威([0-2?]):(.{1,14}) · 时效([0-2?]):(.{1,14}) · "
    r"交叉([0-2?]):(.{1,14}) · 完整([0-2?]):(.{1,14}) · "
    r"无关([0-2?]):(.{1,14})( ⚠️.{1,30})?$"
)
RATING_NOTES_FORBIDDEN = ("可能", "大概", "视情况", "疑似", "应该是")

NORM_METHODS = {
    "percentile_in_batch",
    "percentile_in_window",
    "log_zscore_in_window",
    "none",
}
#: 第八张手抄表已收编（§M6-b 货 6）。注意 `.get()` 取不到键与「登记了但没有
#: 指标」同样返回 None——收编后前者不再可能发生（平台表就是全集）。
#: 微博的主指标待拍：`app/store/dao.py:28` 有一份禁区里的镜像，两边不一致
#: 会让整批证据入库时被拒收（§M6-b 呈拍二），拍板前微博一律不归一化。
PRIMARY_METRICS = platform_primary_metrics()


def _parse_time(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def grade_for_total(total: int) -> str:
    """按 §1.6 的硬边界把 0–10 映射为 A/B/C/D。"""
    if not isinstance(total, int) or isinstance(total, bool) or not 0 <= total <= 10:
        raise ValueError("可靠度总分必须是 0–10 整数")
    if total >= 8:
        return "A"
    if total >= 6:
        return "B"
    if total >= 4:
        return "C"
    return "D"


def claim_support_is_valid(
    grades: Iterable[str], *, downgraded_to_lead: bool = False
) -> bool:
    """D 级可作线索，但不得作为普通断言的唯一最高等级证据。"""
    values = list(grades)
    if not values:
        return False
    if downgraded_to_lead:
        return True
    return any(value in {"A", "B", "C"} for value in values)


def _freshness(evidence: Mapping[str, Any], content_kind: str | None) -> tuple[int, str, str | None]:
    if evidence.get("continuous_page"):
        cutoff = evidence.get("statistics_cutoff_at")
        if cutoff:
            evidence = {**evidence, "published_at": cutoff}
        else:
            return 2, "持续页按采集日", None
        content_kind = "market_data"
    if content_kind == "reference":
        status = evidence.get("reference_status")
        if status == "current":
            return 2, "现行版本", None
        if status == "previous":
            return 1, "上一大版本", None
        if status in {"deprecated", "archived"}:
            return 0, "版本已废弃", None
        content_kind = "industry_view"

    extra = evidence.get("extra")
    degraded_source = (
        extra.get("freshness_degraded_source")
        if isinstance(extra, Mapping)
        else None
    )
    if degraded_source == "fetched_at":
        if not evidence.get("fetched_at"):
            return 0, "缺采集时间", None
        return 1, "抓取时刻兜底", None

    published_at = evidence.get("published_at")
    fetched_at = evidence.get("fetched_at")
    if not published_at:
        inferred_year = evidence.get("published_year")
        if not inferred_year:
            return 0, "缺发布时间", None
        if not fetched_at:
            return 0, "缺采集时间", None
        fetched = _parse_time(str(fetched_at))
        age_days = max(0, (fetched - datetime(int(inferred_year), 1, 1, tzinfo=timezone.utc)).days)
        candidate_windows = (
            [FRESHNESS_WINDOWS[content_kind]]
            if content_kind in FRESHNESS_WINDOWS
            else list(FRESHNESS_WINDOWS.values())
        )
        score = min(
            2 if age_days <= recent else 1 if age_days <= stale else 0
            for recent, stale in candidate_windows
        )
        return min(score, 1), "仅推断发布年份", None
    if not fetched_at:
        return 0, "缺采集时间", None

    published = _parse_time(str(published_at))
    fetched = _parse_time(str(fetched_at))
    age_days = (fetched - published).days
    if age_days < 0:
        return 0, "发布时间晚于采集", "时间戳异常"
    candidate_windows = (
        [FRESHNESS_WINDOWS[content_kind]]
        if content_kind in FRESHNESS_WINDOWS
        else list(FRESHNESS_WINDOWS.values())
    )
    score = min(
        2 if age_days <= recent else 1 if age_days <= stale else 0
        for recent, stale in candidate_windows
    )
    reason = f"距采集{age_days}天" if content_kind in FRESHNESS_WINDOWS else f"类型缺失保守{age_days}天"
    return score, reason, None


def _completeness(evidence: Mapping[str, Any], baseline: int) -> tuple[int, str]:
    if evidence.get("source_type") == "search_snippet":
        return 0, "仅搜索片段"
    if evidence.get("permalink_reachable") is False:
        return 0, "原文不可达"
    if evidence.get("content_truncated") and not evidence.get("permalink_reachable"):
        return 0, "正文截断不可溯"

    has_body = evidence.get("has_body")
    if has_body is None:
        return baseline, "沿用平台基线"
    if has_body:
        discussion = evidence.get("source_type") in {"post", "article", "video"}
        total = evidence.get("comments_total")
        fetched = evidence.get("comments_fetched")
        if discussion and isinstance(total, int) and total > 0:
            ratio = (fetched or 0) / total
            if ratio < 0.8:
                return 1, f"评论覆盖{ratio:.0%}"
        if evidence.get("published_at") and evidence.get("author_name"):
            if evidence.get("platform") == "x" and not evidence.get("conversation_complete"):
                return 1, "仅取单帖正文"
            return 2, "正文作者时间齐全"
        return 1, "正文要素不全"
    if evidence.get("summary_sufficient") and evidence.get("permalink_reachable"):
        return 1, "摘要充分可回溯"
    return 0, "无完整正文"


def _baseline(platform: str, supplied: Mapping[str, int] | None) -> dict[str, int]:
    if supplied is not None:
        result = dict(supplied)
    else:
        result = dict(PLATFORM_BASELINES.get(platform, PLATFORM_BASELINES["web_search"]))
    if set(result) != set(SCORE_FIELDS) or any(
        not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 2
        for value in result.values()
    ):
        raise ValueError("平台基线必须完整提供五个 0–2 整数分")
    return result


def is_comment_row(evidence: Mapping[str, Any]) -> bool:
    """这一行是不是评论（§CMT-1 货 3 的 kind 列；旧行回落 source_type）。"""

    kind = evidence.get("kind")
    if kind is not None:
        return str(kind) == "comment"
    return str(evidence.get("source_type") or "") == "comment"


def _rating_notes(
    scores: Mapping[str, int | None], reasons: Sequence[str], warning: str | None
) -> str:
    labels = ("权威", "时效", "交叉", "完整", "无关")
    main = " · ".join(
        f"{label}{'?' if scores[field] is None else scores[field]}:{reason[:14]}"
        for label, field, reason in zip(labels, SCORE_FIELDS, reasons)
    )
    return main + (f" ⚠️{warning[:30]}" if warning else "")


def rating_notes_problem(
    notes: Any, scores: Mapping[str, Any] | None = None
) -> str | None:
    """返回格式问题；None 表示 §4.2 正则、长度、禁用词与分数均通过。"""
    if not isinstance(notes, str):
        return "rating_notes 必须是字符串"
    if "\n" in notes or "\r" in notes:
        return "rating_notes 必须是单行"
    if len(notes) > 160:
        return "rating_notes 含尾注总长超过 160 字"
    main = notes.split(" ⚠️", 1)[0]
    if len(main) > 120:
        return "rating_notes 主串总长超过 120 字"
    forbidden = next((word for word in RATING_NOTES_FORBIDDEN if word in notes), None)
    if forbidden:
        return f"rating_notes 含禁用词：{forbidden}"
    matched = RATING_NOTES_PATTERN.fullmatch(notes)
    if matched is None:
        return "rating_notes 不匹配五段式正则"
    if scores is not None:
        actual = tuple(
            None if matched.group(index) == "?" else int(matched.group(index))
            for index in (1, 3, 5, 7, 9)
        )
        expected_values = tuple(scores.get(field) for field in SCORE_FIELDS)
        if actual != expected_values:
            return f"rating_notes 分数 {actual} 与五维列 {expected_values} 不一致"
    return None


def score_evidence(
    evidence: Mapping[str, Any], *, baseline: Mapping[str, int] | None = None,
    cluster_stats: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """输入证据、平台基线与簇统计，纯计算五维分、等级和评分理由。"""
    platform = str(evidence.get("platform", "web_search"))
    prior = _baseline(platform, baseline)
    extra = evidence.get("extra") if isinstance(evidence.get("extra"), Mapping) else {}

    authority_kind = extra.get("authority_kind")
    authority = AUTHORITY_SCORES.get(authority_kind, prior["score_authority"])
    authority_reason = AUTHORITY_REASONS.get(authority_kind, "沿用平台基线")
    # §CMT-1 货 4：评论行是读者反应、不是帖子作者的说法，评分理由第一段就要说清
    # 「这是评论」——报告角标悬停、Excel 证据表和人工复核都只看这一行。
    # 父帖链接不进这一行（五段式正则每段上限 14 字，塞不下 URL），
    # 它在 extra.comment_of 与 evidence.parent_permalink 两处都留了。
    if is_comment_row(evidence):
        authority_reason = f"评论·{authority_reason}"[:14]
    if (
        authority_kind == "community_high_signal"
        and not evidence.get("author_history_verified")
        and (
            not isinstance(evidence.get("normalized_score"), (int, float))
            or isinstance(evidence.get("normalized_score"), bool)
            or evidence["normalized_score"] < 0.75
        )
    ):
        authority = 0
        authority_reason = "未达P75且无历史"

    content_kind = extra.get("content_kind")
    freshness, freshness_reason, freshness_warning = _freshness(evidence, content_kind)
    stats = dict(cluster_stats or {})
    verdict = stats.get("verdict", extra.get("crossref_verdict"))
    crossref = CROSSREF_SCORES.get(verdict, prior["score_crossref"])
    crossref_reason = CROSSREF_REASONS.get(verdict, "沿用平台基线")

    completeness, completeness_reason = _completeness(
        evidence, prior["score_completeness"]
    )

    relation = extra.get("interest_relation")
    independence = INTEREST_SCORES.get(relation, prior["score_independence"])
    independence_reason = INTEREST_REASONS.get(relation, "沿用平台基线")
    if authority_kind in {"first_party_official", "verified_principal"} and independence > 1:
        independence = 1
        independence_reason = "官方自述"

    scores = dict(
        zip(SCORE_FIELDS, (authority, freshness, crossref, completeness, independence))
    )
    total = sum(scores.values())
    warnings = [value for value in (freshness_warning, "存在反证" if verdict == "CONFLICT" else None) if value]
    warning = "；".join(warnings) or None
    notes = _rating_notes(
        scores,
        (
            authority_reason, freshness_reason, crossref_reason,
            completeness_reason, independence_reason,
        ),
        warning,
    )
    problem = rating_notes_problem(notes, scores)
    if problem is not None:
        raise AssertionError(problem)
    return {**scores, "score_total": total, "grade": grade_for_total(total), "rating_notes": notes}


def score_evidence_partial(
    evidence: Mapping[str, Any], *,
    missing_dimensions: Mapping[str, str] | None = None,
    baseline: Mapping[str, int] | None = None,
    cluster_stats: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """按既有口径打分；信息不足的维度保留 NULL，并把原因写进五段式理由。"""

    result = score_evidence(
        evidence, baseline=baseline, cluster_stats=cluster_stats
    )
    missing = dict(missing_dimensions or {})
    unknown = sorted(set(missing) - set(SCORE_FIELDS))
    if unknown:
        raise ValueError(f"诚实缺失维度不在闭集：{unknown}")
    matched = RATING_NOTES_PATTERN.fullmatch(result["rating_notes"])
    if matched is None:
        raise AssertionError("既有评分理由无法解析")
    reasons = [matched.group(index) for index in (2, 4, 6, 8, 10)]
    scores: dict[str, int | None] = {
        field: int(result[field]) for field in SCORE_FIELDS
    }
    for field, reason in missing.items():
        normalized = str(reason).strip()
        if not normalized:
            raise ValueError(f"{field} 的诚实缺失原因不得为空")
        scores[field] = None
        reasons[SCORE_FIELDS.index(field)] = normalized
    # 诚实缺失会把权威那段整段换掉，「评论」标记不能跟着丢（§CMT-1 货 4）。
    if is_comment_row(evidence) and not any("评论" in reason for reason in reasons):
        reasons[0] = f"评论·{reasons[0]}"[:14]
    notes = _rating_notes(scores, reasons, None)
    problem = rating_notes_problem(notes, scores)
    if problem is not None:
        raise AssertionError(problem)
    complete = all(value is not None for value in scores.values())
    total = sum(value for value in scores.values() if value is not None)
    return {
        **scores,
        "score_total": total if complete else None,
        "grade": grade_for_total(total) if complete else None,
        "rating_notes": notes,
    }


def _stats(values: Sequence[float]) -> dict[str, float | None]:
    if not values:
        return {"min": None, "p25": None, "median": None, "p75": None, "max": None}
    ordered = sorted(values)

    def percentile(fraction: float) -> float:
        position = (len(ordered) - 1) * fraction
        lower = math.floor(position)
        upper = math.ceil(position)
        if lower == upper:
            return ordered[lower]
        weight = position - lower
        return ordered[lower] * (1 - weight) + ordered[upper] * weight

    return {
        "min": ordered[0], "p25": percentile(0.25), "median": percentile(0.5),
        "p75": percentile(0.75), "max": ordered[-1],
    }


def _none_context(
    *, platform: str, metric: str | None, computed_at: str, reason: str,
    report_id: str, goal_id: str,
) -> dict[str, Any]:
    context = {
        "scope": "batch", "platform": platform, "metric": metric,
        "n": 0, "formula": "none", "stats": {}, "computed_at": computed_at,
        "report_id": report_id, "goal_id": goal_id, "reason": reason,
    }
    if platform == "x":
        context["sampling"] = "post_filtered_local"
    return context


def normalize_evidence_metrics(
    evidence_items: Iterable[Mapping[str, Any]], *, computed_at: str,
    report_id: str, goal_id: str, queries: Sequence[str] = (), filters: str = "",
    window_values: Mapping[str, Sequence[float]] | None = None,
    method: str | None = None,
) -> list[dict[str, Any]]:
    """只在同平台参照池内归一化，返回 raw_metrics 与 R4 三个派生字段。"""
    if method is not None and method not in NORM_METHODS:
        raise ValueError(f"norm_method 不在闭集：{method}")
    items = [deepcopy(dict(item)) for item in evidence_items]
    grouped: dict[str, list[int]] = defaultdict(list)
    for index, item in enumerate(items):
        if item.get("report_id") not in {None, report_id}:
            raise ValueError("归一化条目的 report_id 与参照批不一致")
        if item.get("goal_id") not in {None, goal_id}:
            raise ValueError("归一化条目的 goal_id 与参照批不一致")
        grouped[str(item.get("platform", ""))].append(index)
    windows = window_values or {}

    for platform, indices in grouped.items():
        metric = PRIMARY_METRICS.get(platform)
        if metric is None:
            for index in indices:
                items[index].update(
                    normalized_score=None,
                    norm_method="none",
                    norm_context=_none_context(
                        platform=platform, metric=None, computed_at=computed_at,
                        reason="no_metric_available", report_id=report_id, goal_id=goal_id,
                    ),
                )
            continue

        available = [
            float(items[index].get("raw_metrics", {}).get(metric))
            for index in indices
            if isinstance(items[index].get("raw_metrics", {}).get(metric), (int, float))
            and not isinstance(items[index].get("raw_metrics", {}).get(metric), bool)
        ]
        window = [float(value) for value in windows.get(platform, ())]
        if method == "none":
            chosen = "none"
            pool = []
            scope = "batch"
        elif method == "percentile_in_batch":
            if len(available) < 20:
                raise ValueError("percentile_in_batch 要求批内池 n>=20")
            chosen = method
            pool = available
            scope = "batch"
        elif method == "percentile_in_window":
            if len(window) < 50:
                raise ValueError("percentile_in_window 要求窗口池 n>=50")
            chosen = method
            pool = window
            scope = "window"
        elif method == "log_zscore_in_window":
            if len(window) < 50:
                raise ValueError("log_zscore_in_window 要求窗口池 n>=50")
            chosen = method
            pool = window
            scope = "window"
        elif len(available) >= 20:
            chosen = "percentile_in_batch"
            pool = available
            scope = "batch"
        elif len(window) >= 50:
            chosen = "percentile_in_window"
            pool = window
            scope = "window"
        else:
            chosen = "none"
            pool = []
            scope = "batch"

        for index in indices:
            raw_value = items[index].get("raw_metrics", {}).get(metric)
            if not isinstance(raw_value, (int, float)) or isinstance(raw_value, bool):
                items[index].update(
                    normalized_score=None, norm_method="none",
                    norm_context=_none_context(
                        platform=platform, metric=metric, computed_at=computed_at,
                        reason="metric_not_collected", report_id=report_id, goal_id=goal_id,
                    ),
                )
                continue
            if chosen == "none":
                items[index].update(
                    normalized_score=None, norm_method="none",
                    norm_context=_none_context(
                        platform=platform, metric=metric, computed_at=computed_at,
                        reason="insufficient_sample", report_id=report_id, goal_id=goal_id,
                    ),
                )
                continue

            value = float(raw_value)
            effective_pool = list(pool)
            if scope == "window" and value not in effective_pool:
                effective_pool.append(value)
            if chosen == "log_zscore_in_window":
                logs = [math.log1p(candidate) for candidate in effective_pool]
                mean = sum(logs) / len(logs)
                variance = sum((candidate - mean) ** 2 for candidate in logs) / len(logs)
                sigma = math.sqrt(variance)
                z_score = 0.0 if sigma == 0 else (math.log1p(value) - mean) / sigma
                normalized = 0.5 * (1 + math.erf(z_score / math.sqrt(2)))
                formula = "Phi((log1p(v)-mu)/sigma)"
            else:
                normalized = sum(candidate < value for candidate in effective_pool) / (len(effective_pool) - 1)
                formula = "count(x<v)/(n-1)"
            context: dict[str, Any] = {
                "scope": scope, "platform": platform, "metric": metric,
                "n": len(effective_pool), "formula": formula, "stats": _stats(effective_pool),
                "queries": list(queries), "filters": filters,
                "computed_at": computed_at, "report_id": report_id, "goal_id": goal_id,
            }
            if scope == "window":
                context.update(window_days=90, fallback_from="batch")
            if platform == "x":
                context["sampling"] = "post_filtered_local"
            items[index].update(
                normalized_score=normalized, norm_method=chosen, norm_context=context
            )
    return items
