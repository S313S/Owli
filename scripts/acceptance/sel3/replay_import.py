"""§SEL-3 真机复现：在**库副本**上重放一次 `import_research`，再用只读尺量命中率。

- 库副本一律走 sqlite `.backup`（⛔ 不用 `cp`：会漏 WAL，读到旧状态像「功能没生效」）；
- 底料（`../Owli-src5`）全程只读：只 `mode=ro` 连库、只 `copytree` 读产物目录；
- 零引擎：只跑 `import_research` 与只读读数，不起任何 adapter。

    python scripts/acceptance/sel3/replay_import.py \
        --source-database var/sel3-base.db --source-research-id r-3b3482ca7f8b \
        --source-runs ../Owli-src5/var/runs \
        --target-database var/sel3-after.db --runs-root var/runs-after \
        --from-goal goal-6 --only-chapters ch-4
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from app.replay.import_research import import_research  # noqa: E402
from app.store.dao import Store  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from measure_closure import measure  # noqa: E402


def _backup(source: Path, target: Path) -> None:
    """sqlite 在线备份：`cp` 会漏 WAL，这里拿的是一致快照。"""

    if target.exists():
        raise SystemExit(f"目标库已存在，换个路径或先删：{target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    origin = sqlite3.connect(f"file:{source.resolve()}?mode=ro", uri=True)
    try:
        sink = sqlite3.connect(str(target))
        try:
            origin.backup(sink)
        finally:
            sink.close()
    finally:
        origin.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-database", required=True, type=Path)
    parser.add_argument("--source-research-id", required=True)
    parser.add_argument("--source-runs", required=True, type=Path)
    parser.add_argument("--target-database", required=True, type=Path)
    parser.add_argument("--runs-root", required=True, type=Path)
    parser.add_argument("--from-goal", default=None)
    parser.add_argument("--only-chapters", nargs="*", default=None)
    parser.add_argument("--chapter", default="goal-6/ch-4")
    args = parser.parse_args()

    _backup(args.source_database, args.target_database)
    args.runs_root.mkdir(parents=True, exist_ok=True)
    imported = import_research(
        store=Store(args.target_database),
        source_database=args.source_database,
        source_runs=args.source_runs,
        source_research_id=args.source_research_id,
        runs_root=args.runs_root,
        now_iso="2026-09-18T00:00:00+00:00",
        from_goal=args.from_goal,
        only_chapters=list(args.only_chapters) if args.only_chapters else None,
    )
    result = measure(
        args.target_database, imported.research_id, args.runs_root, args.chapter,
    )
    result["imported"] = {
        "research_id": imported.research_id,
        "source_research_id": imported.source_research_id,
        "from_goal": imported.from_goal,
        "evidence_copied": imported.evidence_copied,
        "chapters_copied": imported.chapters_copied,
        "chapters_reset": list(imported.chapters_reset),
        "runs_dir": str(imported.runs_dir),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
