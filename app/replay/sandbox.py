"""重放沙盒：把底料复制出一份再跑，原件全程只读。

隔离做在**文件层**，不做在主键层：库和产物目录各复制一份到工作区，
研究 id 原样不动，`reports.id` / `evidence.report_id` 一个字都不用改写。
"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Fingerprint:
    """底料指纹：库文件与产物目录各一个 sha256，用来证明原件零改动。"""

    database: str
    runs: str

    def as_dict(self) -> dict[str, str]:
        return {"database": self.database, "runs": self.runs}


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _tree_sha256(root: Path) -> str:
    """目录指纹 = 逐文件「相对路径 + 内容 sha256」排序后再 sha256。

    只按路径与内容算，不含 mtime/权限：复制回来做对比时才不会被元数据搅乱。
    """

    if not root.exists():
        return "absent"
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(_file_sha256(path).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def fingerprint(database: Path, runs: Path) -> Fingerprint:
    return Fingerprint(database=_file_sha256(database), runs=_tree_sha256(runs))


class SandboxMigrationError(RuntimeError):
    """沙盒副本迁不到当前代码要求的 schema 版本。"""


@dataclass(frozen=True)
class ReplaySandbox:
    workspace: Path
    database: Path
    runs_root: Path
    source_database: Path
    source_runs: Path
    source_fingerprint: Fingerprint
    schema_version: int = 0

    def verify_source_untouched(self) -> Fingerprint:
        """重放跑完再量一次原件；对不上就是污染了底料，调用方必须当红处理。"""

        return fingerprint(self.source_database, self.source_runs)


def open_sandbox(
    *,
    source_database: Path,
    source_runs: Path,
    research_id: str,
    workspace: Path,
    schema_path: Path | None = None,
) -> ReplaySandbox:
    """把底料复制进 workspace：库走 sqlite `.backup`（不是 `cp`），产物走整目录复制。

    `cp` 一个开着 WAL 的库会拷到半截事务；`.backup` 是 sqlite 自己的在线备份，
    拿到的是一致快照（本项目此前踩过，写进交接坑清单）。
    """

    source_database = source_database.resolve()
    source_runs = source_runs.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    database = workspace / "replay.db"
    runs_root = workspace / "runs"
    before = fingerprint(source_database, source_runs / research_id)

    if database.exists():
        raise FileExistsError(f"工作区已有库，换一个空目录：{database}")
    origin = sqlite3.connect(f"file:{source_database}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(database)
        try:
            origin.backup(target)
        finally:
            target.close()
    finally:
        origin.close()

    schema_version = _migrate_sandbox_database(database, schema_path)

    runs_root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_runs / research_id, runs_root / research_id)
    _rebase_ledger_paths(database, research_id, runs_root)
    return ReplaySandbox(
        workspace=workspace,
        database=database,
        runs_root=runs_root,
        source_database=source_database,
        source_runs=source_runs / research_id,
        source_fingerprint=before,
        schema_version=schema_version,
    )


def _default_schema_path() -> Path:
    return Path(__file__).resolve().parents[1] / "store" / "schema.sql"


def _read_user_version(database: Path) -> int:
    connection = sqlite3.connect(database)
    try:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])
    finally:
        connection.close()


def _migrate_sandbox_database(database: Path, schema_path: Path | None) -> int:
    """把沙盒副本迁到当前代码要求的 schema，底料原件不参与。

    `.backup` 拿到的是底料的 `user_version` 原样；底料比代码旧（v9 库遇上 v10 代码）
    时一写证据就 `no such column: kind`。这里走 store 自己那条迁移链
    （`initialize_and_check` → `initialize_database_if_empty`），**不另写一份迁移**，
    顺带做一次结构自检；迁不过去就抬头报清版本落差，不静默让重放带病往下跑。
    """

    from app.adapters.selfcheck import initialize_and_check  # 延迟导入：避免 replay 包一被 import 就拉起引擎适配器

    resolved = Path(schema_path) if schema_path is not None else _default_schema_path()
    before = _read_user_version(database)
    try:
        check = initialize_and_check(database, resolved)
    except Exception as error:
        after = _read_user_version(database)
        raise SandboxMigrationError(
            f"重放沙盒库迁移失败：底料 schema v{before}"
            f"（迁到 v{after} 后停下）→ 代码要求 v{_expected_schema_version(resolved)}"
            f"，迁不过去：{error}"
        ) from error
    return int(check.get("schema_version") or _read_user_version(database))


def _expected_schema_version(schema_path: Path) -> int:
    from app.store.schema import read_expected_snapshot

    try:
        return int(read_expected_snapshot(schema_path)["schema_version"])
    except Exception:  # 连权威 schema 都读不出版本时不要掩盖原始错误
        return -1


def _rebase_ledger_paths(database: Path, research_id: str, runs_root: Path) -> int:
    """把账本里指向底料根的绝对产物路径改写到沙盒根。

    §RD-1 抓到的保真缺口：`chapter_progress.actual_output_path` 落库时是绝对路径
    （指向底料那台 worktree 的 runs），而计划里 `opening.inputs` 是相对路径，跑时按
    沙盒根拼绝对路径；两边对不上，`_declared_done_goal_closure` 就永远只剩本 goal，
    跨 goal 证据在沙盒里全部不可见——已 done 的节被判角标失效重写，重放读数全污染。
    只改这一列：闭包只读它，别的绝对路径（事件 payload 等）不参与判定。
    """

    marker = f"/{research_id}/"
    connection = sqlite3.connect(database)
    try:
        rows = connection.execute(
            "SELECT rowid, actual_output_path FROM chapter_progress"
            " WHERE research_id = ? AND actual_output_path LIKE '/%'",
            (research_id,),
        ).fetchall()
        changed = 0
        for rowid, raw in rows:
            text = str(raw or "")
            index = text.find(marker)
            if index < 0:
                continue
            rebased = str(runs_root.resolve() / research_id / text[index + len(marker):])
            if rebased != text:
                connection.execute(
                    "UPDATE chapter_progress SET actual_output_path = ? WHERE rowid = ?",
                    (rebased, rowid),
                )
                changed += 1
        connection.commit()
    finally:
        connection.close()
    return changed
