"""部署级运行配置；产品默认值不得按单机环境分支。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping


_DEFAULTS = {
    "OWLI_TRANSPORT_FAILURE_THRESHOLD": 3,
    "OWLI_PLAN_SEGMENT_RETRIES": 3,
    "OWLI_BACKOFF_INITIAL_SECONDS": 60,
    "OWLI_BACKOFF_MAX_SECONDS": 900,
    "OWLI_ENGINE_PROBE_INTERVAL_SECONDS": 300,
    "OWLI_SESSION_STALL_TIMEOUT_SECONDS": 600,
}


@dataclass(frozen=True)
class ResilienceConfig:
    transport_failure_threshold: int
    plan_segment_retries: int
    backoff_initial_seconds: int
    backoff_max_seconds: int
    engine_probe_interval_seconds: int
    session_stall_timeout_seconds: int = 600

    def backoff_seconds(self, failure_count: int) -> int:
        """返回第 failure_count 次退避时长，按指数增长并封顶。"""

        exponent = max(0, int(failure_count))
        return min(
            self.backoff_initial_seconds * (2 ** exponent),
            self.backoff_max_seconds,
        )


@dataclass(frozen=True)
class ResearchScaleProfile:
    """单个调研规模档位；数值是产品配置，不读取部署环境。"""

    max_goals: int
    max_sources_per_goal: int | None
    source_item_limits: Mapping[str, int]

    def __post_init__(self) -> None:
        if self.max_goals < 3:
            raise ValueError("max_goals 不得小于 3")
        if self.max_sources_per_goal is not None and self.max_sources_per_goal < 1:
            raise ValueError("max_sources_per_goal 必须是正整数或 None")
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


_SCALE_DEFAULTS: dict[str, dict[str, Any]] = {
    "standard": {
        "max_goals": 7,
        "max_sources_per_goal": None,
        "source_item_limits": {
            "hacker_news": 1000,
            "product_hunt": 20,
            "web_search": 10,
            "x": 10,
        },
    },
    "fast": {
        "max_goals": 3,
        "max_sources_per_goal": 2,
        "source_item_limits": {
            "hacker_news": 100,
            "product_hunt": 10,
            "web_search": 5,
            "x": 10,
        },
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
            "max_goals", "max_sources_per_goal", "source_item_limits"
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
    """从部署环境读取 M3-f 韧性数值；传入空映射可读取纯产品默认值。"""

    values = os.environ if environ is None else environ
    config = ResilienceConfig(
        transport_failure_threshold=_positive_int(
            values, "OWLI_TRANSPORT_FAILURE_THRESHOLD"
        ),
        plan_segment_retries=_positive_int(values, "OWLI_PLAN_SEGMENT_RETRIES"),
        backoff_initial_seconds=_positive_int(
            values, "OWLI_BACKOFF_INITIAL_SECONDS"
        ),
        backoff_max_seconds=_positive_int(values, "OWLI_BACKOFF_MAX_SECONDS"),
        engine_probe_interval_seconds=_positive_int(
            values, "OWLI_ENGINE_PROBE_INTERVAL_SECONDS"
        ),
        session_stall_timeout_seconds=_positive_int(
            values, "OWLI_SESSION_STALL_TIMEOUT_SECONDS"
        ),
    )
    if config.backoff_initial_seconds > config.backoff_max_seconds:
        raise ValueError(
            "OWLI_BACKOFF_INITIAL_SECONDS 不得大于 OWLI_BACKOFF_MAX_SECONDS"
        )
    return config


__all__ = [
    "ResearchScaleConfig",
    "ResearchScaleProfile",
    "ResilienceConfig",
    "load_research_scale_config",
    "load_resilience_config",
]
