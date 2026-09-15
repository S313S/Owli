#!/usr/bin/env python3
"""§RATE-5 尺子：重落库之后，库行与评级章产物文件按 permalink 逐格比对。

⛔ 这把尺子**不调被测的写回函数**（`RuntimeCoordinator._rating_payloads` /
`_persist_rating_chapter`）：它直接读 sqlite 与 JSON 文件比较。可复用的生产件只有
`normalize_permalink`（库行键口径）与正式稿闸门 `citation_preflight`（判据本身就是
「闸门对这份库 0 条问题」）。

读数：
- 各评级章产物：产物条目中能在库里找到的行里，五维 + rating_notes 与产物逐格一致的行数，
  以及库行 rated_by 分布
- 全研究 post 行：rated_by 前缀分布、score_crossref 非空数、grade 非空数
- 与 `--before` 库比：citation_no 变动行数；before 里已是 agent:* 的行评分块变动行数
- `--marks`：指定 S 号在库里有没有等级
- `--preflight`：正式稿闸门对这份库的问题清单

    ../Owli/.venv/bin/python scripts/acceptance/rate5/check_repersist.py \
        --database var/rate5-copy.db --before var/rate5-pristine.db \
        --runs ../Owli-wx1/var/runs --research r-20271e8a5028 \
        --marks S18-S23,S40-S45,S85-S91 --preflight
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from app.store.dao import normalize_permalink  # noqa: E402

BLOCK = (
    "score_authority", "score_freshness", "score_crossref",
    "score_completeness", "score_independence", "rating_notes",
)


def _rows(path: Path, research: str) -> dict[str, dict[str, Any]]:
    connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        return {
            str(row["permalink"]): dict(row)
            for row in connection.execute(
                "SELECT * FROM evidence WHERE report_id = ?", (research,),
            )
        }
    finally:
        connection.close()


def _prefix(rated_by: Any) -> str:
    text = str(rated_by or "")
    return text.split("@", 1)[0] if text else "(空)"


def _marks(spec: str) -> list[int]:
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip().upper().replace("S", "")
        if "-" in part:
            low, high = part.split("-")
            out.extend(range(int(low), int(high) + 1))
        elif part:
            out.append(int(part))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--before", type=Path)
    parser.add_argument("--runs", type=Path, required=True)
    parser.add_argument("--research", required=True)
    parser.add_argument("--chapters", help="逗号分隔的评级章 agent_id；默认全部顶层产物")
    parser.add_argument("--marks")
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()

    rows = _rows(args.database, args.research)
    report: dict[str, Any] = {"database": str(args.database), "research": args.research}

    wanted = set(args.chapters.split(",")) if args.chapters else None
    chapters = []
    goals = args.runs.resolve() / args.research / "goals"
    for path in sorted(goals.glob("*/reliability-audit*.json")):
        if ".part." in path.name:
            continue
        agent_id = path.stem
        if wanted and agent_id not in wanted:
            continue
        items = json.loads(path.read_text(encoding="utf-8"))
        found = equal = 0
        rated_by: Counter[str] = Counter()
        kinds: Counter[str] = Counter()
        for item in items if isinstance(items, list) else []:
            row = rows.get(normalize_permalink(str(item.get("permalink") or "")))
            if row is None:
                continue
            found += 1
            kinds[str(row["kind"])] += 1
            rated_by[str(row["rated_by"])] += 1
            if all(row[c] == item.get(c) for c in BLOCK):
                equal += 1
        chapters.append({
            "file": str(path.relative_to(goals)), "items": len(items),
            "found_in_db": found, "block_equal": equal,
            "kinds": dict(kinds), "db_rated_by": dict(rated_by),
        })
    report["chapters"] = chapters

    posts = [r for r in rows.values() if r["kind"] == "post"]
    report["posts"] = {
        "total": len(posts),
        "rated_by": dict(Counter(_prefix(r["rated_by"]) for r in posts)),
        "agent_rated": sum(str(r["rated_by"] or "").startswith("agent:") for r in posts),
        "score_crossref_not_null": sum(r["score_crossref"] is not None for r in posts),
        "grade_not_null": sum(r["grade"] is not None for r in posts),
        "cited": sum(r["citation_no"] is not None for r in posts),
        "cited_graded": sum(
            r["citation_no"] is not None and r["grade"] is not None for r in posts
        ),
    }
    comments = [r for r in rows.values() if r["kind"] == "comment"]
    report["comments"] = {
        "total": len(comments),
        "rated_by": dict(Counter(_prefix(r["rated_by"]) for r in comments)),
    }

    if args.before:
        before = _rows(args.before, args.research)
        report["vs_before"] = {
            "rows_before": len(before), "rows_after": len(rows),
            "citation_no_changed": sum(
                1 for key, row in before.items()
                if key not in rows or rows[key]["citation_no"] != row["citation_no"]
            ),
            "agent_rows_before": sum(
                str(r["rated_by"] or "").startswith("agent:") for r in before.values()
            ),
            "agent_rows_block_changed": sum(
                1 for key, row in before.items()
                if str(row["rated_by"] or "").startswith("agent:")
                and any(rows[key][c] != row[c] for c in (*BLOCK, "rated_by"))
            ),
        }

    if args.marks:
        by_no = {int(r["citation_no"]): r for r in rows.values() if r["citation_no"] is not None}
        marks = []
        for number in _marks(args.marks):
            row = by_no.get(number)
            marks.append({
                "mark": f"S{number:02d}",
                "platform": row["platform"] if row else None,
                "grade": row["grade"] if row else None,
                "rated_by": row["rated_by"] if row else None,
            })
        report["marks"] = marks
        report["marks_ungraded"] = [m["mark"] for m in marks if not m["grade"]]

    if args.preflight:
        from app.report.polish.run import citation_preflight
        from app.report.polish.tables import collect_inputs
        from app.store.dao import Store

        connection = sqlite3.connect(f"file:{args.database.resolve()}?mode=ro", uri=True)
        report_path = connection.execute(
            "SELECT report_path FROM reports WHERE id = ?", (args.research,),
        ).fetchone()[0]
        connection.close()
        text_path = Path(report_path)
        if not text_path.is_absolute():
            text_path = args.runs.resolve().parent.parent / text_path
        store = Store(args.database.resolve())
        data = collect_inputs(store, args.research, text_path.read_text(encoding="utf-8"))
        report["preflight_text"] = str(text_path)
        report["preflight_sources"] = len(data.get("sources") or [])
        report["preflight"] = citation_preflight(store, args.research, data)

    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
