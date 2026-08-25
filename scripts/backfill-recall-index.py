#!/usr/bin/env python3
"""把既有 completed 报告幂等回填到 recall_fts。"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.store.dao import Store  # noqa: E402


def _counts(database_path: Path) -> tuple[int, int]:
    with sqlite3.connect(database_path) as connection:
        completed = connection.execute(
            "SELECT count(*) FROM reports WHERE status = 'completed'"
        ).fetchone()[0]
        indexed = connection.execute(
            "SELECT count(*) FROM recall_fts"
        ).fetchone()[0]
    return int(completed), int(indexed)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="按 status='completed' 重建 Owli 召回索引"
    )
    parser.add_argument("database", type=Path, help="Owli SQLite 数据库路径")
    args = parser.parse_args()
    database_path = args.database.resolve()
    if not database_path.is_file():
        parser.error(f"数据库不存在：{database_path}")

    completed, before = _counts(database_path)
    written = Store(database_path).backfill_recall_index()
    completed_after, after = _counts(database_path)
    if completed_after != completed or written != completed or after != completed:
        raise RuntimeError(
            "回填计数不一致："
            f"completed={completed_after}, written={written}, after={after}"
        )
    print(json.dumps({
        "before": before,
        "completed": completed,
        "written": written,
        "after": after,
    }, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
