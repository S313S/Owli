"""单 research 的执行期传输断路状态机；不执行网络或计时。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.config import ResilienceConfig


class CircuitEvent(StrEnum):
    ENGINE_DOWN = "ENGINE_DOWN"
    PROBE_OK = "PROBE_OK"
    RESET = "RESET"


@dataclass(frozen=True)
class CircuitTransition:
    event: CircuitEvent
    research_id: str
    engine: str
    target: str | None = None


class ResearchCircuitBreaker:
    """只管理连续故障、断路与复位；候选探测由适配层执行。"""

    def __init__(self, research_id: str, config: ResilienceConfig) -> None:
        self.research_id = research_id
        self.config = config
        self._failures: dict[str, int] = {}
        self._pending: set[str] = set()
        self._down: dict[str, str] = {}
        self._route_override: str | None = None

    @property
    def route_override(self) -> str | None:
        return self._route_override

    def failure_count(self, engine: str) -> int:
        return self._failures.get(engine.casefold(), 0)

    def is_down(self, engine: str) -> bool:
        return engine.casefold() in self._down

    def record_transport_failure(
        self, engine: str, *, planning: bool
    ) -> CircuitTransition | None:
        key = engine.casefold()
        if planning or key in self._down:
            return None
        count = self._failures.get(key, 0) + 1
        self._failures[key] = count
        if count < self.config.transport_failure_threshold:
            return None
        self._pending.add(key)
        return CircuitTransition(
            CircuitEvent.ENGINE_DOWN, self.research_id, key
        )

    def record_non_transport(self, engine: str) -> None:
        key = engine.casefold()
        if key not in self._down:
            self._failures[key] = 0
            self._pending.discard(key)

    def record_success(self, engine: str) -> None:
        self.record_non_transport(engine)

    def reject_failover(self, engine: str) -> None:
        self._pending.discard(engine.casefold())

    def activate_failover(
        self, engine: str, target: str
    ) -> CircuitTransition:
        key = engine.casefold()
        selected = target.casefold()
        if key not in self._pending:
            raise RuntimeError(f"引擎 {key} 没有待激活的断路请求")
        self._pending.discard(key)
        self._down[key] = selected
        self._route_override = selected
        return CircuitTransition(
            CircuitEvent.ENGINE_DOWN,
            self.research_id,
            key,
            target=selected,
        )

    def record_probe(
        self, engine: str, *, healthy: bool
    ) -> tuple[CircuitTransition, ...]:
        key = engine.casefold()
        if not healthy or key not in self._down:
            return ()
        target = self._down.pop(key)
        self._failures[key] = 0
        self._pending.discard(key)
        if self._route_override == target:
            self._route_override = None
        return (
            CircuitTransition(
                CircuitEvent.PROBE_OK, self.research_id, key, target=target
            ),
            CircuitTransition(
                CircuitEvent.RESET, self.research_id, key, target=target
            ),
        )


__all__ = [
    "CircuitEvent",
    "CircuitTransition",
    "ResearchCircuitBreaker",
]
