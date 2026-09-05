#!/usr/bin/env python3
"""§RPT-1 验收：对底料库里的一份成稿整理一次正式稿。

    python3 scripts/acceptance/rpt1/rpt1_polish.py --db var/rpt1-8956.db \
        --runs var/runs --id r-3e04f808dffd --template consulting

只读库与工作稿，只写 `runs/<id>/exports/`。零采集、零评级，只付一次撰写调用。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.report.polish.run import polish  # noqa: E402


class ReadOnlyStore:
    """够 `collect_inputs` 用的最小 store：只有 get_report 与 list_evidence 两个读方法。"""

    def __init__(self, database: Path) -> None:
        self.conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        self.conn.row_factory = sqlite3.Row

    def get_report(self, research_id: str):
        row = self.conn.execute("select * from reports where id=?", (research_id,)).fetchone()
        return dict(row) if row else None

    def list_evidence(self, research_id: str):
        return [dict(r) for r in
                self.conn.execute("select * from evidence where report_id=?", (research_id,))]


def work_draft_path(runs_root: Path, research_id: str, report_path: str) -> Path:
    """库里的 report_path 可能是指向别的 worktree 的绝对路径，按 runs_root 重新拼。"""
    raw = Path(report_path)
    return runs_root / research_id / "goals" / raw.parent.name / raw.name


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--runs", required=True)
    parser.add_argument("--id", required=True)
    parser.add_argument("--template", default="consulting")
    args = parser.parse_args()
    runs_root = Path(args.runs).resolve()
    store = ReadOnlyStore(Path(args.db).resolve())
    report = store.get_report(args.id)
    if report is None:
        raise SystemExit(f"× 库里没有 {args.id}")
    text = work_draft_path(runs_root, args.id, report["report_path"]).read_text("utf-8")
    started = time.time()
    outcome = await polish(store, args.id, runs_root, text, template=args.template)
    outcome["elapsed_seconds"] = round(time.time() - started, 1)
    print(json.dumps(outcome, ensure_ascii=False, indent=2))
    return 0 if outcome["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
