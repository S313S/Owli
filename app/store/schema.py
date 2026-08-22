"""schema.sql 的初始化与结构快照。"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


_LATEST_SCHEMA_VERSION = 5


def initialize_database_if_empty(
    database_path: str | Path, schema_path: str | Path
) -> None:
    """仅在数据库全空时执行权威 schema，不修补部分结构。"""
    database = Path(database_path)
    database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as connection:
        table_count = connection.execute(
            """
            SELECT count(*) FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            """
        ).fetchone()[0]
        user_version = connection.execute("PRAGMA user_version").fetchone()[0]
        if table_count == 0 and user_version == 0:
            connection.executescript(Path(schema_path).read_text(encoding="utf-8"))
        elif user_version > 0:
            _apply_migrations(connection, Path(schema_path), user_version)


def _apply_migrations(
    connection: sqlite3.Connection, schema_path: Path, current_version: int
) -> None:
    """按版本号只向前执行 store 自有迁移。"""

    if current_version > _LATEST_SCHEMA_VERSION:
        return
    migrations_dir = schema_path.parent / "migrations"
    for version in range(current_version + 1, _LATEST_SCHEMA_VERSION + 1):
        matches = sorted(migrations_dir.glob(f"v{version}_*.sql"))
        if len(matches) != 1:
            raise RuntimeError(f"schema v{version} 迁移文件数量必须为 1，实际 {len(matches)}")
        chapter_columns = {
            row[1] for row in connection.execute(
                "PRAGMA table_xinfo(chapter_progress)"
            ).fetchall()
        }
        if version == 4 and {"engine_error", "conclusion_error"} <= chapter_columns:
            connection.execute("PRAGMA user_version = 4")
        else:
            connection.executescript(matches[0].read_text(encoding="utf-8"))
        migrated_version = connection.execute("PRAGMA user_version").fetchone()[0]
        if migrated_version != version:
            raise RuntimeError(
                f"schema v{version} 迁移未更新 user_version，实际 {migrated_version}"
            )


def read_database_snapshot(database_path: str | Path) -> dict[str, Any]:
    with sqlite3.connect(database_path) as connection:
        return _snapshot(connection)


def read_expected_snapshot(schema_path: str | Path) -> dict[str, Any]:
    with sqlite3.connect(":memory:") as connection:
        connection.executescript(Path(schema_path).read_text(encoding="utf-8"))
        return _snapshot(connection)


def _snapshot(connection: sqlite3.Connection) -> dict[str, Any]:
    definitions = connection.execute(
        """
        SELECT name, sql FROM sqlite_master
        WHERE type = 'table' AND sql IS NOT NULL AND name NOT LIKE 'sqlite_%'
        """
    ).fetchall()
    virtual_tables = {
        name
        for name, sql in definitions
        if sql.lstrip().upper().startswith("CREATE VIRTUAL TABLE")
    }
    shadow_tables = {
        name
        for name, _ in definitions
        if any(name.startswith(f"{virtual_name}_") for virtual_name in virtual_tables)
    }
    application_tables = {
        name for name, _ in definitions if name not in shadow_tables
    }
    columns = {
        table: _column_names(connection, table) for table in application_tables
    }
    strict_flags = _strict_flags(connection, application_tables)
    return {
        "tables": application_tables,
        "virtual_tables": virtual_tables,
        "columns": columns,
        "strict": strict_flags,
        "schema_version": connection.execute("PRAGMA user_version").fetchone()[0],
        "journal_mode": connection.execute("PRAGMA journal_mode").fetchone()[0],
    }


def _column_names(connection: sqlite3.Connection, table: str) -> set[str]:
    quoted_table = table.replace('"', '""')
    return {
        row[1]
        for row in connection.execute(f'PRAGMA table_xinfo("{quoted_table}")').fetchall()
    }


def _strict_flags(
    connection: sqlite3.Connection, application_tables: set[str]
) -> dict[str, bool]:
    rows = connection.execute("PRAGMA table_list").fetchall()
    return {
        row[1]: bool(row[5])
        for row in rows
        if row[1] in application_tables and row[2] == "table"
    }
