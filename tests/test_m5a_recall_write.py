from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "app" / "store" / "schema.sql"


def _store(tmp_path):
    from app.store.dao import Store

    database_path = tmp_path / "owli.db"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    return Store(database_path), database_path


def _create_report(store, report_id: str, *, status: str = "running") -> None:
    store.create_report(
        id=report_id,
        title=f"{report_id} 的竞品分析",
        research_question="哪些产品能力值得复用？",
        created_at="2026-08-25T10:00:00+08:00",
        status=status,
        completed_at=(
            "2026-08-25T10:30:00+08:00" if status == "completed" else None
        ),
        summary_line=(f"{report_id} 已完成" if status == "completed" else None),
    )


def test_完成报告时事务内写agent标签并同步召回索引(tmp_path) -> None:
    store, database_path = _store(tmp_path)
    _create_report(store, "r-finish")
    tags = ["竞品对象", "用例类型", "领域", "结论指向"]

    store.finish_report(
        "r-finish",
        status="completed",
        completed_at="2026-08-25T11:00:00+08:00",
        summary_line="分析协作产品后，建议优先补齐自动化能力",
        agent_tags=tags,
    )

    with sqlite3.connect(database_path) as connection:
        tag_rows = connection.execute(
            "SELECT tag, source FROM report_tags WHERE report_id = ? ORDER BY tag",
            ("r-finish",),
        ).fetchall()
        recall_row = connection.execute(
            "SELECT report_id, title, tags, summary_line FROM recall_fts "
            "WHERE report_id = ?",
            ("r-finish",),
        ).fetchone()

    assert tag_rows == [(tag, "agent") for tag in sorted(tags)]
    assert recall_row == (
        "r-finish",
        "r-finish 的竞品分析",
        " ".join(sorted(tags)),
        "分析协作产品后，建议优先补齐自动化能力",
    )


def test_标签变更二次同步仍只有一条且内容取最新(tmp_path) -> None:
    store, database_path = _store(tmp_path)
    _create_report(store, "r-idempotent")
    store.finish_report(
        "r-idempotent",
        status="completed",
        completed_at="2026-08-25T11:00:00+08:00",
        agent_tags=["产品", "协作", "竞品"],
    )

    store.replace_report_tags(
        "r-idempotent",
        ["决策", "自动化", "协作"],
        source="agent",
        created_at="2026-08-25T11:10:00+08:00",
    )

    with sqlite3.connect(database_path) as connection:
        count = connection.execute(
            "SELECT count(*) FROM recall_fts WHERE report_id = ?",
            ("r-idempotent",),
        ).fetchone()[0]
        indexed_tags = connection.execute(
            "SELECT tags FROM recall_fts WHERE report_id = ?",
            ("r-idempotent",),
        ).fetchone()[0]

    assert count == 1
    assert indexed_tags == " ".join(sorted(["决策", "自动化", "协作"]))


def test_历史回填只收completed且重复执行行数不变(tmp_path) -> None:
    store, database_path = _store(tmp_path)
    _create_report(store, "r-completed-1", status="completed")
    _create_report(store, "r-completed-2", status="completed")
    _create_report(store, "r-running", status="running")
    _create_report(store, "r-failed", status="failed")
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO recall_fts(report_id, title, tags, summary_line) "
            "VALUES ('r-running', '旧运行态', '', NULL)"
        )
        connection.execute(
            "INSERT INTO recall_fts(report_id, title, tags, summary_line) "
            "VALUES ('r-completed-1', '旧重复一', '', NULL)"
        )
        connection.execute(
            "INSERT INTO recall_fts(report_id, title, tags, summary_line) "
            "VALUES ('r-completed-1', '旧重复二', '', NULL)"
        )

    first = store.backfill_recall_index()
    second = store.backfill_recall_index()

    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            "SELECT report_id FROM recall_fts ORDER BY report_id"
        ).fetchall()

    assert first == 2
    assert second == 2
    assert rows == [("r-completed-1",), ("r-completed-2",)]


def test_回填脚本输出可判定计数(tmp_path) -> None:
    store, database_path = _store(tmp_path)
    _create_report(store, "r-history", status="completed")

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "backfill-recall-index.py"),
            str(database_path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "before": 0,
        "completed": 1,
        "written": 1,
        "after": 1,
    }
