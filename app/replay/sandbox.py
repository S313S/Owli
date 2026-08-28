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


@dataclass(frozen=True)
class ReplaySandbox:
    workspace: Path
    database: Path
    runs_root: Path
    source_database: Path
    source_runs: Path
    source_fingerprint: Fingerprint

    def verify_source_untouched(self) -> Fingerprint:
        """重放跑完再量一次原件；对不上就是污染了底料，调用方必须当红处理。"""

        return fingerprint(self.source_database, self.source_runs)


def open_sandbox(
    *,
    source_database: Path,
    source_runs: Path,
    research_id: str,
    workspace: Path,
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

    runs_root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_runs / research_id, runs_root / research_id)
    return ReplaySandbox(
        workspace=workspace,
        database=database,
        runs_root=runs_root,
        source_database=source_database,
        source_runs=source_runs / research_id,
        source_fingerprint=before,
    )
