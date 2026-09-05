#!/usr/bin/env python3
"""§RATE-4 货 3 沙盒备料：把库里现成的五维**原样抄**进评级章产物。

为什么要这一步：重放起跑时 runtime 会把 `runs/**/reliability-audit*.json` 重新
贴回库（`_persist_goal_evidence`），那份 JSON 还是补评之前的分——补评算出来的
结果一投影就被还原（事件里 `d_gate_filtered` 仍是改前的数）。同一个机制在真跑
里也成立，已另立缺陷卡；这里只把**沙盒**的库与产物对齐，好让重放看到新尺子。

本脚本**一分都不算**：只按 permalink 把库里的 `score_*` / `rating_notes` /
`extra` / `rated_by` 抄过去，抄不上的行原样不动并计数报出来。验收尺子自己也要
验——所以它不重实现评分，也不容忍静默的部分匹配。

    python3 scripts/rate4_sync_rating_artifacts.py \
        --database var/rate4-sandbox.db --runs var/runs --research r-3e04f808dffd
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

FIELDS = (
    "score_authority", "score_freshness", "score_crossref",
    "score_completeness", "score_independence", "rating_notes", "rated_by",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--runs", type=Path, required=True)
    parser.add_argument("--research", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    connection = sqlite3.connect(f"file:{args.database.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = {
            str(row["permalink"]): dict(row)
            for row in connection.execute(
                "SELECT permalink, extra, " + ", ".join(FIELDS)
                + " FROM evidence WHERE report_id = ?", (args.research,),
            )
        }
    finally:
        connection.close()

    root = args.runs.resolve() / args.research / "goals"
    touched = matched = missed = 0
    for path in sorted(root.glob("*/reliability-audit*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, list):
            continue
        changed = False
        for item in payload:
            if not isinstance(item, dict):
                continue
            row = rows.get(str(item.get("permalink")))
            if row is None:
                missed += 1
                continue
            matched += 1
            for field in FIELDS:
                if item.get(field) != row[field]:
                    item[field] = row[field]
                    changed = True
            if item.get("extra") is not None and row["extra"] is not None:
                stored = json.loads(row["extra"])
                if item["extra"] != stored:
                    item["extra"] = stored
                    changed = True
        if changed and not args.dry_run:
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
            )
            touched += 1
    print(json.dumps({
        "artifacts_written": touched, "rows_matched": matched,
        "rows_unmatched": missed, "dry_run": args.dry_run,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
