"""schema.sql 的初始化与结构快照。"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


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
