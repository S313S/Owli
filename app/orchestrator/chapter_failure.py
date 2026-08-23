"""章节失败原因的共享闭集归类。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping

from app.adapters import validation
from app.adapters.ratelimit import (
    RateLimitVerdict,
    classify_rate_limit,
    classify_transport_error,
    search_without_negation,
)


# 措辞正则一律只做兜底，且必须走 search_without_negation 排除否定句（D-002/缺陷 C）。
_TIMEOUT_WORDING_PATTERN = re.compile(r"timeout|timed? ?out|超时", re.IGNORECASE)


CHAPTER_FAILURE_REASONS = frozenset({
    "empty_result",
    "tool_unavailable",
    "quota_exhausted",
    "retry_exhausted",
    "conclusion_invalid",
    "timeout",
})


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def chapter_failure_reason(
    result: Any,
    output_path: Path | None = None,
    *,
    fallback: str | None = None,
) -> str:
    """把适配器或 Scheduler 结果归一为章账本闭集。"""

    conclusion = _field(result, "conclusion")
    result_reason = str(_field(result, "reason") or "").casefold()
    if result_reason in CHAPTER_FAILURE_REASONS:
        return result_reason
    declared = str(_field(conclusion, "reason") or "").casefold()
    denials = [
        *list(_field(result, "permission_denials", []) or []),
        *list(_field(conclusion, "capability_denials", []) or []),
    ]
    if declared == "tool_unavailable" or denials:
        return "tool_unavailable"
    if declared in CHAPTER_FAILURE_REASONS:
        return declared

    engine_error = str(_field(result, "engine_error", "") or "")
    conclusion_error = str(_field(result, "conclusion_error", "") or "")
    errors = " ".join(filter(None, (engine_error, conclusion_error)))
    # 限流优先读结构化字段（api_error_status / 错误类型 / rate_limit_info）；
    # 只有结构化一个信号都没有时才退到措辞兜底，且兜底不命中「非限流」这类否定句。
    if classify_rate_limit(
        result, engine_error, conclusion_error, text=errors,
    ) is RateLimitVerdict.LIMITED:
        return "quota_exhausted"
    if search_without_negation(_TIMEOUT_WORDING_PATTERN, errors) is not None:
        return "timeout"
    if errors and classify_transport_error(errors):
        return "retry_exhausted"

    if output_path is not None:
        try:
            if (
                not output_path.is_file()
                or not output_path.read_text(encoding="utf-8").strip()
            ):
                return "empty_result"
        except (OSError, UnicodeError):
            return "empty_result"

    report = _field(result, "validation")
    if (
        conclusion is None
        and conclusion_error
        and _field(report, "verdict") is validation.Verdict.PASS
    ):
        return "conclusion_invalid"
    if fallback is not None:
        if fallback not in CHAPTER_FAILURE_REASONS:
            raise ValueError(f"章失败 fallback 不在闭集：{fallback!r}")
        return fallback
    conclusion_output = str(_field(conclusion, "output_path", "") or "").strip()
    if conclusion is None or not conclusion_output:
        return "empty_result"
    return "retry_exhausted"


__all__ = ["CHAPTER_FAILURE_REASONS", "chapter_failure_reason"]
