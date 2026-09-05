#!/usr/bin/env python3
"""§RPT-1 货 6：三份成稿 × 三模板 = 九次整理，跑完逐份上尺子出读数表。

    python3 scripts/acceptance/rpt1/rpt1_matrix.py --db var/rpt1-8956.db --runs var/runs

串行跑（每次一个 Opus 调用，几分钟起步）。已经存在且过尺子的成稿默认跳过，
`--force` 才重跑——撞到缺陷时重跑单格用 `--only <id>:<模板>`。
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.report.polish.run import artifact_paths, polish  # noqa: E402
from app.report.polish.skills import load_templates  # noqa: E402

_spec = importlib.util.spec_from_file_location("check_polished", Path(__file__).with_name("check_polished.py"))
check_polished = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_polished)

REPORTS = ("r-b10812f664d2", "r-3e04f808dffd", "r-045acebc352b")


def _load_runner():
    spec = importlib.util.spec_from_file_location(
        "rpt1_polish", Path(__file__).with_name("rpt1_polish.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def one(runner, store, runs_root: Path, research_id: str, template: str,
              force: bool) -> dict:
    """跑一格并上尺子。返回这一格的读数行。"""
    md_path, tables_path = artifact_paths(runs_root, research_id, template)
    report = store.get_report(research_id)
    work_path = runner.work_draft_path(runs_root, research_id, report["report_path"])
    text = work_path.read_text(encoding="utf-8")
    started = time.time()
    if force or not md_path.is_file():
        outcome = await polish(store, research_id, runs_root, text, template=template)
    else:
        outcome = {"status": "ok", "attempts": 0, "offpool": [], "skipped": True}
    row = {"research_id": research_id, "template": template, "status": outcome["status"],
           "attempts": outcome.get("attempts"), "seconds": round(time.time() - started, 1),
           "bytes": md_path.stat().st_size if md_path.is_file() else 0}
    if outcome["status"] != "ok":
        row["ruler"] = {"—": ["未出稿：" + "；".join(outcome.get("errors") or ["未知"])]}
        return row
    findings = check_polished.run(md_path, tables_path, work_path)
    row["ruler"] = {name: problems for name, problems in findings.items() if problems}
    row["passed"] = not row["ruler"]
    return row


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--runs", required=True)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--only", default=None, help="<research_id>:<模板>，只跑这一格")
    args = parser.parse_args()
    runs_root = Path(args.runs).resolve()
    runner = _load_runner()
    store = runner.ReadOnlyStore(Path(args.db).resolve())
    templates = [t.name for t in load_templates()]
    cells = [(rid, tpl) for rid in REPORTS for tpl in templates]
    if args.only:
        want_id, _, want_tpl = args.only.partition(":")
        cells = [c for c in cells if c == (want_id, want_tpl)]
    rows = []
    for research_id, template in cells:
        row = await one(runner, store, runs_root, research_id, template, args.force)
        rows.append(row)
        mark = "PASS" if row.get("passed") else "FAIL"
        print(f"[{mark}] {research_id} × {template}  {row['bytes']} B  "
              f"{row['seconds']}s  attempts={row['attempts']}", flush=True)
        for name, problems in (row.get("ruler") or {}).items():
            print(f"        {name}: {len(problems)} 处 · {problems[0][:70]}", flush=True)
    print("\n" + json.dumps(rows, ensure_ascii=False, indent=1))
    green = sum(1 for r in rows if r.get("passed"))
    print(f"\n尺子全过 {green}/{len(rows)}")
    return 0 if green == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
