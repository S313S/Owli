"""部署级运行配置；产品默认值不得按单机环境分支。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Mapping


_DEFAULTS = {
    "OWLI_PLAN_SEGMENT_RETRIES": 3,
    "OWLI_PLAN_CHAPTER_LINT_RETRIES": 2,
    "OWLI_PLAN_TRANSPORT_RETRIES": 3,
    "OWLI_BACKOFF_INITIAL_SECONDS": 60,
    "OWLI_BACKOFF_MAX_SECONDS": 900,
    "OWLI_SOURCE_PAYLOAD_BYTE_LIMIT": 262_144,
}
_MIN_SOURCE_PAYLOAD_BYTE_LIMIT = 1024


@dataclass(frozen=True)
class ResilienceConfig:
    plan_segment_retries: int
    backoff_initial_seconds: int
    backoff_max_seconds: int
    plan_transport_retries: int = 3
    plan_chapter_lint_retries: int = 2

    def backoff_seconds(self, failure_count: int) -> int:
        """返回第 failure_count 次退避时长，按指数增长并封顶。"""

        exponent = max(0, int(failure_count))
        return min(
            self.backoff_initial_seconds * (2 ** exponent),
            self.backoff_max_seconds,
        )


_CHAPTER_ENGINE_DEFAULTS = {
    "collection": "codex",
    "transport": "codex",
    "data_cleaning": "codex",
    "code_execution": "codex",
    "excel_generation": "codex",
    "comparison": "claude",
    "cross_validation": "claude",
    "audit": "claude",
    "report": "claude",
    "summary": "claude",
    "tagging": "claude",
}


@dataclass(frozen=True)
class ChapterEngineConfig:
    """章类型到默认引擎的产品配置；编排层只透传计划声明。"""

    overrides: Mapping[str, str] = field(default_factory=dict)

    def engine_for(self, chapter_type: str) -> str:
        name = str(chapter_type)
        engine = self.overrides.get(name, _CHAPTER_ENGINE_DEFAULTS.get(name))
        if engine not in {"claude", "codex"}:
            raise ValueError(f"章类型 {name!r} 缺少合法引擎配置")
        return engine


@dataclass(frozen=True)
class ResearchScaleProfile:
    """单个调研规模档位；数值是产品配置，不读取部署环境。"""

    max_goals: int
    max_sources_per_goal: int | None
    source_item_limits: Mapping[str, int]
    max_chapters_per_goal: int | None = None
    # 节化章按「每节」使用，章总墙钟由编排层乘节数；其余章仍按「每章」使用。
    chapter_wall_clock_seconds: int | None = None

    def __post_init__(self) -> None:
        if self.max_goals < 3:
            raise ValueError("max_goals 不得小于 3")
        if self.max_sources_per_goal is not None and self.max_sources_per_goal < 1:
            raise ValueError("max_sources_per_goal 必须是正整数或 None")
        if self.max_chapters_per_goal is not None and self.max_chapters_per_goal < 1:
            raise ValueError("max_chapters_per_goal 必须是正整数或 None")
        if (
            self.chapter_wall_clock_seconds is not None
            and self.chapter_wall_clock_seconds < 1
        ):
            raise ValueError("chapter_wall_clock_seconds 必须是正整数或 None")
        limits = {str(key): int(value) for key, value in self.source_item_limits.items()}
        if any(value < 1 for value in limits.values()):
            raise ValueError("source_item_limits 的值必须是正整数")
        object.__setattr__(self, "source_item_limits", limits)


@dataclass(frozen=True)
class ResearchScaleConfig:
    standard: ResearchScaleProfile
    fast: ResearchScaleProfile

    def profile(self, scale: str) -> ResearchScaleProfile:
        if scale not in {"fast", "standard"}:
            raise ValueError(f"scale 只能取 fast 或 standard，实际为 {scale!r}")
        return getattr(self, scale)


@dataclass(frozen=True)
class SourceResponseConfig:
    """信息源工具回灌给 LLM 的响应护栏。"""

    payload_byte_limit: int


_SCALE_DEFAULTS: dict[str, dict[str, Any]] = {
    "standard": {
        "max_goals": 7,
        "max_sources_per_goal": None,
        "max_chapters_per_goal": None,
        "source_item_limits": {
            "hacker_news": 1000,
            "product_hunt": 20,
            "web_search": 10,
            "x": 10,
            "xhs": 20,
            "douyin": 10,
            "reddit": 20,
        },
        "chapter_wall_clock_seconds": 1800,
    },
    "fast": {
        "max_goals": 3,
        "max_sources_per_goal": 2,
        "max_chapters_per_goal": 4,
        "source_item_limits": {
            "hacker_news": 100,
            "product_hunt": 10,
            # §SRC-1 货 8：原值 5 是 xhs/douyin(25) 的 1/5，「网页搜索少」是
            # 这个数字写死的天花板，不是源取不到——实测同一适配器
            # max_results=20 是 3.02 秒 20 条（解禁依据：decision-log 19:0x）。
            "web_search": 20,
            "x": 10,
            "xhs": 25,
            "douyin": 25,
            "reddit": 25,
        },
        "chapter_wall_clock_seconds": 330,
    },
}


def load_research_scale_config(
    overrides: Mapping[str, Mapping[str, Any]] | None = None,
) -> ResearchScaleConfig:
    """读取产品级规模配置；不从环境变量分叉默认行为。"""

    values: dict[str, dict[str, Any]] = {
        name: {
            **profile,
            "source_item_limits": dict(profile["source_item_limits"]),
        }
        for name, profile in _SCALE_DEFAULTS.items()
    }
    for name, override in (overrides or {}).items():
        if name not in values:
            raise ValueError(f"未知调研规模档位：{name}")
        unknown = set(override) - {
            "max_goals", "max_sources_per_goal", "source_item_limits",
            "max_chapters_per_goal", "chapter_wall_clock_seconds",
        }
        if unknown:
            raise ValueError(f"{name} 含未知规模配置：{sorted(unknown)}")
        limits = dict(values[name]["source_item_limits"])
        limits.update(dict(override.get("source_item_limits", {})))
        values[name].update(dict(override))
        values[name]["source_item_limits"] = limits
    return ResearchScaleConfig(
        standard=ResearchScaleProfile(**values["standard"]),
        fast=ResearchScaleProfile(**values["fast"]),
    )


def _positive_int(values: Mapping[str, str], name: str) -> int:
    raw = values.get(name, str(_DEFAULTS[name]))
    try:
        parsed = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是正整数，实际为 {raw!r}") from exc
    if parsed <= 0:
        raise ValueError(f"{name} 必须是正整数，实际为 {raw!r}")
    return parsed


def load_resilience_config(
    environ: Mapping[str, str] | None = None,
) -> ResilienceConfig:
    """读取规划分段重试与限流退避配置。"""

    values = os.environ if environ is None else environ
    config = ResilienceConfig(
        plan_segment_retries=_positive_int(values, "OWLI_PLAN_SEGMENT_RETRIES"),
        backoff_initial_seconds=_positive_int(
            values, "OWLI_BACKOFF_INITIAL_SECONDS"
        ),
        backoff_max_seconds=_positive_int(values, "OWLI_BACKOFF_MAX_SECONDS"),
        plan_transport_retries=_positive_int(
            values, "OWLI_PLAN_TRANSPORT_RETRIES"
        ),
        plan_chapter_lint_retries=_positive_int(
            values, "OWLI_PLAN_CHAPTER_LINT_RETRIES"
        ),
    )
    if config.backoff_initial_seconds > config.backoff_max_seconds:
        raise ValueError(
            "OWLI_BACKOFF_INITIAL_SECONDS 不得大于 OWLI_BACKOFF_MAX_SECONDS"
        )
    return config


def load_source_response_config(
    environ: Mapping[str, str] | None = None,
) -> SourceResponseConfig:
    """读取 source.* 回灌 payload 的 UTF-8 字节上限。"""

    values = os.environ if environ is None else environ
    payload_byte_limit = _positive_int(
        values, "OWLI_SOURCE_PAYLOAD_BYTE_LIMIT"
    )
    if payload_byte_limit < _MIN_SOURCE_PAYLOAD_BYTE_LIMIT:
        raise ValueError(
            "OWLI_SOURCE_PAYLOAD_BYTE_LIMIT 不得小于 "
            f"{_MIN_SOURCE_PAYLOAD_BYTE_LIMIT}"
        )
    return SourceResponseConfig(payload_byte_limit=payload_byte_limit)


__all__ = [
    "ChapterEngineConfig",
    "ResearchScaleConfig",
    "ResearchScaleProfile",
    "ResilienceConfig",
    "SourceResponseConfig",
    "load_research_scale_config",
    "load_resilience_config",
    "load_source_response_config",
]
