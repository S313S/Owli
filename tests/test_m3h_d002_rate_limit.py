"""D-002/缺陷 C：限流判定必须结构化字段优先，措辞兜底不得命中否定句。"""

from __future__ import annotations

import json

import pytest

from app.adapters.ratelimit import (
    RateLimitVerdict,
    classify_rate_limit,
    structured_rate_limit_verdict,
)
from app.orchestrator.chapter_failure import chapter_failure_reason


# worklog §十 逐字原文：api_error_status=null，文案里显式写着「非限流」。
NON_RATELIMIT_ENGINE_ERROR = json.dumps({
    "is_error": True,
    "api_error_status": None,
    "subtype": "error_during_execution",
    "result": "非限流错误：error_during_execution",
    "errors": [
        "AxiosError: timeout of 5000ms exceeded",
        "Error: The socket connection was closed unexpectedly",
        "TelemetrySafeError: Output does not match required schema: "
        "/summary: must NOT have more than 200 characters",
    ],
}, ensure_ascii=False)


@pytest.mark.parametrize("text", [
    "非限流错误：error_during_execution",
    "本次失败并非限流",
    "未触发限流，是 schema 校验失败",
    "无额度问题",
    "engine reported: not a rate limit error",
    "quota 未超，失败原因是产物缺失",
    "rate limit not exceeded",
])
def test_D002_否定句不得被措辞兜底判成限流(text: str) -> None:
    assert classify_rate_limit(text) is not RateLimitVerdict.LIMITED


@pytest.mark.parametrize("text", [
    "HTTP 429 Too Many Requests",
    "Claude 限流：5 小时窗口已用尽",
    "usage limit reached, retry later",
    "额度耗尽，等待重置",
])
def test_D002_真限流文案仍走措辞兜底命中(text: str) -> None:
    assert classify_rate_limit(text) is RateLimitVerdict.LIMITED


def test_D002_结构化字段优先于措辞() -> None:
    # 429 结构化字段在，哪怕文案只字不提限流也归限流。
    assert structured_rate_limit_verdict(
        {"api_error_status": 429},
    ) is RateLimitVerdict.LIMITED
    assert classify_rate_limit(
        json.dumps({"api_error_status": 429, "result": "engine crashed"}),
    ) is RateLimitVerdict.LIMITED
    # 明确的非 429 状态码：结构化说了不是限流，措辞不得翻案。
    assert classify_rate_limit(
        {"api_error_status": 500}, text="rate limit exceeded",
    ) is RateLimitVerdict.NOT_LIMITED
    # 结构化错误类型闭集同样优先。
    assert structured_rate_limit_verdict(
        {"error": {"type": "rate_limit_error"}},
    ) is RateLimitVerdict.LIMITED
    assert structured_rate_limit_verdict(
        {"rate_limit_info": {"status": "rejected"}},
    ) is RateLimitVerdict.LIMITED
    # api_error_status 显式为 null = 没有状态信号，交给措辞兜底而不是当成限流。
    assert structured_rate_limit_verdict(
        {"api_error_status": None, "result": "非限流错误"},
    ) is RateLimitVerdict.UNKNOWN


def test_D002_共享入口对非限流原文不再归_quota_exhausted() -> None:
    reason = chapter_failure_reason({
        "engine_error": NON_RATELIMIT_ENGINE_ERROR,
        "conclusion_error": "owli-result.summary 必须是 200 字以内字符串",
    })
    assert reason != "quota_exhausted"
    assert reason == "timeout"  # 结构化 errors 里真实死因是传输超时


def test_D002_共享入口对真_429_仍归_quota_exhausted() -> None:
    assert chapter_failure_reason({
        "engine_error": json.dumps({
            "is_error": True, "api_error_status": 429,
            "result": "HTTP 429 rate limit",
        }, ensure_ascii=False),
    }) == "quota_exhausted"
    # Codex 侧只有纯文本、没有结构化状态码时，措辞兜底仍要接住。
    assert chapter_failure_reason({
        "engine_error": "Codex: you've hit your usage limit",
    }) == "quota_exhausted"


def test_D002_采集与报告两侧共用同一份判定() -> None:
    from app.orchestrator.scheduler import chapter_failure_reason as sched_reason
    from app.orchestrator.sectioning import section_failure_reason

    assert sched_reason is chapter_failure_reason
    assert section_failure_reason is chapter_failure_reason
    payload = {"engine_error": NON_RATELIMIT_ENGINE_ERROR, "conclusion_error": None}
    assert sched_reason(payload) == section_failure_reason(payload) != "quota_exhausted"
