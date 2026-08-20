"""信息源运行态用量的固定 SQLite 通道。"""

from __future__ import annotations

import calendar
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class UsageRecord:
    returned: int
    newly_billed: int


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("时间必须带时区")
    return value.astimezone(timezone.utc)


def week_start_utc(value: datetime) -> datetime:
    """返回包含给定时刻的周一 00:00 UTC。"""

    current = _utc(value)
    monday = current - timedelta(days=current.weekday())
    return monday.replace(hour=0, minute=0, second=0, microsecond=0)


def _anchor(year: int, month: int, anchor_day: int) -> datetime:
    if not 1 <= anchor_day <= 31:
        raise ValueError("账期锚点必须为 1–31")
    day = min(anchor_day, calendar.monthrange(year, month)[1])
    return datetime(year, month, day, tzinfo=timezone.utc)


def billing_cycle_start_utc(value: datetime, anchor_day: int) -> datetime:
    """按月末对齐规则返回当前平台账期起点。"""

    current = _utc(value)
    candidate = _anchor(current.year, current.month, anchor_day)
    if current >= candidate:
        return candidate
    if current.month == 1:
        return _anchor(current.year - 1, 12, anchor_day)
    return _anchor(current.year, current.month - 1, anchor_day)


class SourceUsageStore:
    """原子维护 UTC 日请求数与平台计费去重集合。"""

    def __init__(self, database_path: str | Path) -> None:
        self._database_path = Path(database_path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def record_response(
        self,
        source: str,
        *,
        occurred_at: datetime,
        resource_ids: Iterable[str],
    ) -> UsageRecord:
        if not source.strip():
            raise ValueError("source 不能为空")
        identifiers = [str(item) for item in resource_ids]
        if any(not item for item in identifiers):
            raise ValueError("resource_id 不能为空")
        utc_date = _utc(occurred_at).date().isoformat()
        newly_billed = 0
        with self._connect() as connection:
            for resource_id in identifiers:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO source_usage_billed_resource (
                      source, utc_date, resource_id
                    ) VALUES (?, ?, ?)
                    """,
                    (source, utc_date, resource_id),
                )
                newly_billed += max(cursor.rowcount, 0)
            connection.execute(
                """
                INSERT INTO source_usage (source, utc_date, reads, requests)
                VALUES (?, ?, ?, 1)
                ON CONFLICT(source, utc_date) DO UPDATE SET
                  reads = source_usage.reads + excluded.reads,
                  requests = source_usage.requests + 1
                """,
                (source, utc_date, newly_billed),
            )
        return UsageRecord(returned=len(identifiers), newly_billed=newly_billed)

    def reads_since(
        self, source: str, start: datetime, end: datetime | None = None
    ) -> int:
        return self._sum_since("reads", source, start, end)

    def requests_since(
        self, source: str, start: datetime, end: datetime | None = None
    ) -> int:
        return self._sum_since("requests", source, start, end)

    def _sum_since(
        self, column: str, source: str, start: datetime, end: datetime | None
    ) -> int:
        if column not in {"reads", "requests"}:
            raise ValueError("未知用量列")
        start_date = _utc(start).date().isoformat()
        params: list[str] = [source, start_date]
        end_clause = ""
        if end is not None:
            end_clause = " AND utc_date < ?"
            params.append(_utc(end).date().isoformat())
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT COALESCE(SUM({column}), 0) FROM source_usage "
                f"WHERE source = ? AND utc_date >= ?{end_clause}",
                params,
            ).fetchone()
        return int(row[0])
