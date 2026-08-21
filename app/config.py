"""部署级运行配置；产品默认值不得按单机环境分支。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping


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


__all__ = ["ResilienceConfig", "load_resilience_config"]
