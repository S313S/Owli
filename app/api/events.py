"""research 级事件序号、有限回放缓冲与 SSE 编码。"""

from __future__ import annotations

import asyncio
import copy
import json
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Deque, Mapping


Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class SequencedEvent:
    research_id: str
    sequence: int
    occurred_at: datetime
    payload: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.payload,
            "research_id": self.research_id,
            "sequence": self.sequence,
            "occurred_at": self.occurred_at.isoformat(),
        }

    def to_sse(self) -> str:
        body = json.dumps(self.as_dict(), ensure_ascii=False, separators=(",", ":"))
        event_type = str(self.payload.get("type", "message"))
        return f"id: {self.sequence}\nevent: {event_type}\ndata: {body}\n\n"


@dataclass(frozen=True)
class ReplayBatch:
    events: tuple[SequencedEvent, ...]
    truncated: bool


class ResearchEventBuffer:
    """每个 research 独立编号；条数与时间窗口取先到者。"""

    def __init__(
        self,
        *,
        max_events: int = 2000,
        max_age_seconds: int = 60 * 60,
        clock: Clock = _utc_now,
    ) -> None:
        self.max_events = max_events
        self.max_age = timedelta(seconds=max_age_seconds)
        self.clock = clock
        self._events: dict[str, Deque[SequencedEvent]] = defaultdict(deque)
        self._sequences: dict[str, int] = defaultdict(int)
        self._condition: asyncio.Condition | None = None

    def bind_to_running_loop(self) -> None:
        """在 ASGI lifespan 内调用，避免 Python 3.9 提前绑定默认循环。"""
        asyncio.get_running_loop()
        self._condition = asyncio.Condition()

    def _active_condition(self) -> asyncio.Condition:
        if self._condition is None:
            asyncio.get_running_loop()
            self._condition = asyncio.Condition()
        return self._condition

    def _prune(self, research_id: str, now: datetime) -> None:
        events = self._events[research_id]
        cutoff = now - self.max_age
        while events and events[0].occurred_at < cutoff:
            events.popleft()
        while len(events) > self.max_events:
            events.popleft()

    def _append_locked(self, research_id: str, payload: Mapping[str, Any]) -> SequencedEvent:
        now = self.clock()
        self._sequences[research_id] += 1
        event = SequencedEvent(
            research_id,
            self._sequences[research_id],
            now,
            copy.deepcopy(dict(payload)),
        )
        self._events[research_id].append(event)
        self._prune(research_id, now)
        return event

    async def publish(self, research_id: str, payload: Mapping[str, Any]) -> SequencedEvent:
        condition = self._active_condition()
        async with condition:
            event = self._append_locked(research_id, payload)
            condition.notify_all()
            return event

    async def replay_after(
        self,
        research_id: str,
        last_event_id: int | None,
    ) -> ReplayBatch:
        condition = self._active_condition()
        async with condition:
            self._prune(research_id, self.clock())
            events = self._events[research_id]
            if last_event_id is not None and self._history_is_truncated(events, last_event_id):
                event = self._append_locked(
                    research_id,
                    {
                        "type": "replay_truncated",
                        "data": {
                            "requested_after": last_event_id,
                            "message": "事件回放窗口已截断，请拉取全量快照对齐状态",
                        },
                    },
                )
                condition.notify_all()
                return ReplayBatch((event,), True)
            start = 0 if last_event_id is None else last_event_id
            return ReplayBatch(tuple(event for event in events if event.sequence > start), False)

    @staticmethod
    def _history_is_truncated(events: Deque[SequencedEvent], last_event_id: int) -> bool:
        if not events:
            return False
        return last_event_id < events[0].sequence - 1

    async def wait_after(
        self,
        research_id: str,
        sequence: int,
        *,
        timeout: float = 15.0,
    ) -> tuple[SequencedEvent, ...]:
        condition = self._active_condition()
        async with condition:
            def available() -> bool:
                self._prune(research_id, self.clock())
                return any(event.sequence > sequence for event in self._events[research_id])

            try:
                await asyncio.wait_for(condition.wait_for(available), timeout=timeout)
            except asyncio.TimeoutError:
                return ()
            return tuple(
                event for event in self._events[research_id] if event.sequence > sequence
            )
