"""执行期连续 API retry 的纯会话停滞状态机。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

from app.adapters.events import ItemKind


class SessionStallState(str, Enum):
    ACTIVE = "ACTIVE"
    RETRYING = "RETRYING"
    TRIPPED = "TRIPPED"


@dataclass(frozen=True)
class SessionStallEvidence:
    elapsed_seconds: float
    api_retry_count: int


class SessionStallDetector:
    """只由归一化事件与注入时钟推进，不创建后台 timer。"""

    def __init__(
        self,
        *,
        timeout_seconds: float,
        clock: Callable[[], float],
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("session stall timeout 必须为正数")
        self._timeout_seconds = float(timeout_seconds)
        self._clock = clock
        self.state = SessionStallState.ACTIVE
        self._started_at: float | None = None
        self._api_retry_count = 0

    @staticmethod
    def _value(value: Any) -> str | None:
        raw = getattr(value, "value", value)
        return str(raw) if raw is not None else None

    def _reset(self) -> None:
        if self.state is SessionStallState.TRIPPED:
            return
        self.state = SessionStallState.ACTIVE
        self._started_at = None
        self._api_retry_count = 0

    def observe(self, event: Any) -> SessionStallEvidence | None:
        if self.state is SessionStallState.TRIPPED:
            return None

        kind = self._value(getattr(event, "item_kind", None))
        if kind in {ItemKind.TOOL_CALL.value, ItemKind.OUTPUT.value}:
            self._reset()
            return None

        cause = self._value(getattr(event, "cause", None))
        if cause is not None and cause.casefold() == "rate_limit":
            self._reset()
            return None

        outcome = self._value(getattr(event, "outcome", None))
        if outcome is None or outcome.casefold() != "api_retry":
            return None

        now = float(self._clock())
        if self.state is SessionStallState.ACTIVE:
            self.state = SessionStallState.RETRYING
            self._started_at = now
            self._api_retry_count = 1
            return None

        assert self._started_at is not None
        self._api_retry_count += 1
        elapsed = max(0.0, now - self._started_at)
        if elapsed < self._timeout_seconds:
            return None
        self.state = SessionStallState.TRIPPED
        return SessionStallEvidence(
            elapsed_seconds=elapsed,
            api_retry_count=self._api_retry_count,
        )


__all__ = [
    "SessionStallDetector",
    "SessionStallEvidence",
    "SessionStallState",
]
