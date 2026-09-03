"""research 级事件序号、有限回放缓冲与 SSE 编码。"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Deque, Mapping

from app.adapters.transcript import TRANSCRIPT_SUFFIX, read_transcript

logger = logging.getLogger(__name__)


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
        store: Any | None = None,
    ) -> None:
        self.max_events = max_events
        self.max_age = timedelta(seconds=max_age_seconds)
        self.clock = clock
        self._store = store
        self._events: dict[str, Deque[SequencedEvent]] = defaultdict(deque)
        self._sequences: dict[str, int] = defaultdict(int)
        self._condition: asyncio.Condition | None = None

    def bind_store(self, store: Any) -> None:
        """由 API 装配固定 Store；测试仍可不绑定而使用纯内存模式。"""

        self._store = store

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
        frozen_payload = copy.deepcopy(dict(payload))
        if self._store is None:
            self._sequences[research_id] += 1
            event = SequencedEvent(
                research_id,
                self._sequences[research_id],
                now,
                frozen_payload,
            )
        else:
            row = self._store.append_event(
                research_id,
                event_type=str(frozen_payload.get("type", "message")),
                payload=frozen_payload,
                created_at=now.isoformat(),
            )
            event = SequencedEvent(
                str(row["research_id"]),
                int(row["sequence"]),
                self._parse_time(str(row["created_at"])),
                copy.deepcopy(dict(row["payload"])),
            )
            self._sequences[research_id] = max(
                self._sequences[research_id], event.sequence
            )
        self._events[research_id].append(event)
        self._prune(research_id, now)
        return event

    @staticmethod
    def _parse_time(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    def _load_window_locked(self, research_id: str) -> Deque[SequencedEvent]:
        if self._store is None:
            self._prune(research_id, self.clock())
            return self._events[research_id]
        cutoff = self.clock() - self.max_age
        rows = self._store.list_events_window(
            research_id,
            created_since=cutoff.isoformat(),
            limit=self.max_events,
        )
        events = deque(
            SequencedEvent(
                str(row["research_id"]),
                int(row["sequence"]),
                self._parse_time(str(row["created_at"])),
                copy.deepcopy(dict(row["payload"])),
            )
            for row in rows
        )
        self._events[research_id] = events
        if events:
            self._sequences[research_id] = max(
                self._sequences[research_id], events[-1].sequence
            )
        return events

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
            events = self._load_window_locked(research_id)
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


def _step_hint(record: Mapping[str, Any]) -> str:
    """从一条原始事件里挑出「此刻在干什么」：工具名 / 阶段名，挑不出就留空。"""

    event = record.get("event")
    if isinstance(event, str):
        return event[:80]
    if not isinstance(event, Mapping):
        return ""
    item = event.get("item")
    if isinstance(item, Mapping):
        for key in ("tool_name", "name", "type"):
            value = item.get(key)
            if isinstance(value, str) and value:
                return value[:80]
    for key in ("subtype", "type"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return value[:80]
    return ""


@dataclass
class _SectionBeat:
    """一节的心跳状态：起跑时刻、上次发的时刻与那时的 seq。"""

    started_at: float
    last_emit_at: float = 0.0
    last_seq: int = 0
    emitted: bool = False


class SectionHeartbeatPublisher:
    """§OBS-2 货 3：每节每 15 s 或每 20 条原始事件发一条 `section_heartbeat`。

    读的是货 1 落下的 `<chapter>.transcript.jsonl`，**不进适配器**——适配器只管
    把原始流写出来，谁在看、多久看一次是观测侧的事。节跑完就不再发（文件不再长）。
    """

    def __init__(
        self,
        buffer: ResearchEventBuffer,
        runs_root: Any,
        active_researches: Callable[[], Any],
        *,
        interval_seconds: float = 3.0,
        min_gap_seconds: float = 15.0,
        event_step: int = 20,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._buffer = buffer
        self._runs_root = Path(runs_root)
        self._active = active_researches
        self.interval_seconds = interval_seconds
        self.min_gap_seconds = min_gap_seconds
        self.event_step = event_step
        self._clock = clock
        self._beats: dict[tuple[str, str, str], _SectionBeat] = {}

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:  # 观测不许把服务拖死
                logger.warning("section_heartbeat 一轮失败，跳过", exc_info=True)
            await asyncio.sleep(self.interval_seconds)

    async def tick(self) -> list[dict[str, Any]]:
        """扫一遍所有在跑研究的 transcript，该发的发掉；返回本轮发出的心跳。"""

        published: list[dict[str, Any]] = []
        for research_id in list(self._active() or []):
            root = self._runs_root / str(research_id) / "goals"
            if not root.is_dir():
                continue
            for path in sorted(root.glob(f"*/*{TRANSCRIPT_SUFFIX}")):
                payload = self._beat_for(str(research_id), path)
                if payload is None:
                    continue
                await self._buffer.publish(str(research_id), payload)
                published.append(payload)
        return published

    def _beat_for(self, research_id: str, path: Path) -> dict[str, Any] | None:
        goal_id = path.parent.name
        chapter = path.name[: -len(TRANSCRIPT_SUFFIX)]
        key = (research_id, goal_id, chapter)
        now = self._clock()
        beat = self._beats.get(key)
        if beat is None:
            beat = self._beats.setdefault(key, _SectionBeat(started_at=now))
        tail = read_transcript(path, tail=1)
        last_seq = int(tail.get("last_seq") or 0)
        if last_seq <= 0:
            return None
        due = (
            not beat.emitted
            or last_seq - beat.last_seq >= self.event_step
            or now - beat.last_emit_at >= self.min_gap_seconds
        )
        if not due or (beat.emitted and last_seq == beat.last_seq):
            return None
        lines = tail.get("lines") or []
        record = lines[-1] if lines else {}
        beat.emitted = True
        beat.last_emit_at = now
        beat.last_seq = last_seq
        return {
            "type": "section_heartbeat",
            "data": {
                "goal": goal_id,
                "chapter": chapter,
                "agent": str(record.get("agent") or ""),
                "engine": str(record.get("engine") or ""),
                "step_hint": _step_hint(record),
                "elapsed_s": round(max(0.0, now - beat.started_at), 1),
                "last_seq": last_seq,
            },
        }
