from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "app" / "store" / "schema.sql"


def _initialized_database(tmp_path: Path) -> Path:
    from app.store.schema import initialize_database_if_empty

    database = tmp_path / "owli.db"
    initialize_database_if_empty(database, SCHEMA_PATH)
    return database


def test_v1数据库通过既有版本机制迁移且不改_evidence_冻结列(tmp_path) -> None:
    from app.store.schema import initialize_database_if_empty, read_expected_snapshot

    database = tmp_path / "owli-v1.db"
    expected = read_expected_snapshot(SCHEMA_PATH)
    evidence_columns = expected["columns"]["evidence"]
    with sqlite3.connect(database) as connection:
        connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        connection.execute("DROP TABLE source_usage_billed_resource")
        connection.execute("DROP TABLE source_usage")
        connection.execute("PRAGMA user_version = 1")

    initialize_database_if_empty(database, SCHEMA_PATH)

    with sqlite3.connect(database) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        actual_evidence_columns = {
            row[1] for row in connection.execute("PRAGMA table_xinfo(evidence)")
        }
    assert version == 6
    assert {"source_usage", "source_usage_billed_resource"} <= tables
    assert actual_evidence_columns == evidence_columns


def test_同一_UTC_日资源去重后才累加读取但每次请求都对账(tmp_path) -> None:
    from app.store.usage import SourceUsageStore

    database = _initialized_database(tmp_path)
    store = SourceUsageStore(database)
    occurred_at = datetime(2026, 8, 20, 9, tzinfo=timezone.utc)

    first = store.record_response(
        "x_api", occurred_at=occurred_at, resource_ids=["post-1", "post-2", "post-2"]
    )
    second = store.record_response(
        "x_api", occurred_at=occurred_at, resource_ids=["post-1", "post-3"]
    )

    assert first.returned == 3
    assert first.newly_billed == 2
    assert second.returned == 2
    assert second.newly_billed == 1
    assert store.reads_since("x_api", datetime(2026, 8, 20, tzinfo=timezone.utc)) == 3
    assert store.requests_since("x_api", datetime(2026, 8, 20, tzinfo=timezone.utc)) == 2


def test_去重窗口跨_UTC_日后重新计费(tmp_path) -> None:
    from app.store.usage import SourceUsageStore

    database = _initialized_database(tmp_path)
    store = SourceUsageStore(database)
    store.record_response(
        "x_api",
        occurred_at=datetime(2026, 8, 20, 23, 59, tzinfo=timezone.utc),
        resource_ids=["post-1"],
    )
    next_day = store.record_response(
        "x_api",
        occurred_at=datetime(2026, 8, 21, 0, 1, tzinfo=timezone.utc),
        resource_ids=["post-1"],
    )

    assert next_day.newly_billed == 1


def test_周窗口从周一零点且_31_号锚点按月末对齐() -> None:
    from app.store.usage import billing_cycle_start_utc, week_start_utc

    thursday = datetime(2026, 8, 20, 12, tzinfo=timezone.utc)
    assert week_start_utc(thursday) == datetime(2026, 8, 17, tzinfo=timezone.utc)
    assert billing_cycle_start_utc(thursday, 31) == datetime(
        2026, 7, 31, tzinfo=timezone.utc
    )
    assert billing_cycle_start_utc(
        datetime(2028, 3, 1, tzinfo=timezone.utc), 31
    ) == datetime(2028, 2, 29, tzinfo=timezone.utc)
