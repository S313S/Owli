"""Owli 启动时的 schema 自检。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.store.schema import (
    initialize_database_if_empty,
    read_database_snapshot,
    read_expected_snapshot,
)


class SchemaCheckError(RuntimeError):
    """SQLite 实际结构与权威 schema 不一致。"""


def initialize_and_check(
    database_path: str | Path, schema_path: str | Path
) -> dict[str, Any]:
    initialize_database_if_empty(database_path, schema_path)
    actual = read_database_snapshot(database_path)
    expected = read_expected_snapshot(schema_path)
    differences = _compare_snapshots(actual, expected)
    if differences:
        detail = "\n".join(f"- {difference}" for difference in differences)
        raise SchemaCheckError(f"Schema 自检失败：\n{detail}")

    business_tables = sorted(actual["tables"] - actual["virtual_tables"])
    return {
        "ok": True,
        "schema_version": actual["schema_version"],
        "journal_mode": actual["journal_mode"],
        "tables": business_tables,
        "virtual_tables": sorted(actual["virtual_tables"]),
    }


def _compare_snapshots(
    actual: dict[str, Any], expected: dict[str, Any]
) -> list[str]:
    differences: list[str] = []
    missing_tables = expected["tables"] - actual["tables"]
    unexpected_tables = actual["tables"] - expected["tables"]
    differences.extend(f"缺少表 {name}" for name in sorted(missing_tables))
    differences.extend(f"多出表 {name}" for name in sorted(unexpected_tables))

    for table in sorted(actual["tables"] & expected["tables"]):
        missing_columns = expected["columns"][table] - actual["columns"][table]
        unexpected_columns = actual["columns"][table] - expected["columns"][table]
        differences.extend(
            f"表 {table} 缺少列 {name}" for name in sorted(missing_columns)
        )
        differences.extend(
            f"表 {table} 多出列 {name}" for name in sorted(unexpected_columns)
        )
        if actual["strict"].get(table) != expected["strict"].get(table):
            differences.append(
                f"表 {table} STRICT 标记不一致："
                f"预期 {expected['strict'].get(table)}，"
                f"实际 {actual['strict'].get(table)}"
            )

    if actual["schema_version"] != expected["schema_version"]:
        differences.append(
            f"user_version 不一致：预期 {expected['schema_version']}，"
            f"实际 {actual['schema_version']}"
        )
    if actual["journal_mode"].lower() != "wal":
        differences.append(
            f"journal_mode 不一致：预期 wal，实际 {actual['journal_mode']}"
        )
    return differences
