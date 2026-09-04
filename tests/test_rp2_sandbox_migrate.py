"""§RP-2 货 1/2：重放沙盒自己把副本迁到当前 schema。

`open_sandbox` 走的是 sqlite `.backup`，拿到的库**原样带着底料的 `user_version`**。
§CMT-1 把 schema 升到 v10（evidence 加 `kind`/`parent_permalink`）之后，v9 的底料
在 v10 代码下重放一写证据就 `no such column: kind`。此前只在需求仓的驱动脚本里
绕过（跑前手动 `initialize_and_check`）；产品侧要让沙盒自己迁，重放才是
「任何底料都能用」的功能。

底料原件不参与迁移：迁的是副本，原件的指纹必须一个字节都不动。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "app" / "store" / "schema.sql"
SOURCE_ID = "r-01JXOWLI0000000000RP2TEST"


def _seed_v9(database: Path, runs_root: Path) -> None:
    """造一份 v9 底料：建当前库再把 v10 加的两列拆掉，`user_version` 退回 9。

    直接按 v9 时刻的 schema.sql 建库会把「历史 schema 文件」也钉进用例；
    这里只回退 v10 那一步的结构差异，验的正是 v9→v10 这条迁移。
    """

    from app.adapters.selfcheck import initialize_and_check

    initialize_and_check(database, SCHEMA_PATH)
    connection = sqlite3.connect(database)
    try:
        connection.execute("DROP INDEX IF EXISTS idx_evidence_parent")
        connection.execute("ALTER TABLE evidence DROP COLUMN kind")
        connection.execute("ALTER TABLE evidence DROP COLUMN parent_permalink")
        connection.execute("PRAGMA user_version = 9")
        connection.commit()
    finally:
        connection.close()

    directory = runs_root / SOURCE_ID / "goals" / "goal-1"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "agent-1.md").write_text("上一轮产物", encoding="utf-8")


def _user_version(database: Path) -> int:
    connection = sqlite3.connect(database)
    try:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])
    finally:
        connection.close()


def test_v9底料开沙盒会被迁到当前版本而原件不动(tmp_path: Path) -> None:
    from app.replay.sandbox import fingerprint, open_sandbox

    database = tmp_path / "source.db"
    runs = tmp_path / "runs"
    _seed_v9(database, runs)
    before = fingerprint(database, runs / SOURCE_ID)
    assert _user_version(database) == 9

    sandbox = open_sandbox(
        source_database=database,
        source_runs=runs,
        research_id=SOURCE_ID,
        workspace=tmp_path / "ws",
    )

    expected = _user_version(Path(str(sandbox.database)))
    assert expected == 10, "沙盒副本必须迁到当前 schema 版本"
    assert sandbox.schema_version == 10
    # v10 那两列在沙盒里真的能写（当初就是这里炸 no such column: kind）
    connection = sqlite3.connect(sandbox.database)
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_xinfo(evidence)")}
    finally:
        connection.close()
    assert {"kind", "parent_permalink"} <= columns

    # 原件仍是 v9，且库文件与产物目录指纹一个字节没变
    assert _user_version(database) == 9
    assert fingerprint(database, runs / SOURCE_ID) == before
    assert sandbox.verify_source_untouched() == sandbox.source_fingerprint


def test_迁不过去要报清版本落差而不是静默(tmp_path: Path) -> None:
    """把 v10 迁移文件指到一份坏 schema 目录，迁移必须抬头报错。"""

    from app.replay.sandbox import SandboxMigrationError, open_sandbox

    database = tmp_path / "source.db"
    runs = tmp_path / "runs"
    _seed_v9(database, runs)

    broken_root = tmp_path / "broken_store"
    (broken_root / "migrations").mkdir(parents=True)
    (broken_root / "schema.sql").write_text(
        SCHEMA_PATH.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (broken_root / "migrations" / "v10_broken.sql").write_text(
        "ALTER TABLE evidence ADD COLUMN kind TEXT;\n"
        "SELECT 该语句是坏的;\n",
        encoding="utf-8",
    )

    with pytest.raises(SandboxMigrationError) as caught:
        open_sandbox(
            source_database=database,
            source_runs=runs,
            research_id=SOURCE_ID,
            workspace=tmp_path / "ws",
            schema_path=broken_root / "schema.sql",
        )

    message = str(caught.value)
    assert "v9" in message and "v10" in message, message
    assert "迁不过去" in message, message
