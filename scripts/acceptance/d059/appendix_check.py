#!/usr/bin/env python3
"""§D-059 货 3 量尺：原声表挪到附录之后，判据 ①②③④ 离线自证。

    python3 scripts/acceptance/d059/appendix_check.py --db <库> --runs <runs 根> --id <研究 id>

⛔ **不重出**（一轮 38 分钟、要付撰写调用）。做法是拿 09-10 那份真成稿当写手产物，
把程序块按生产那条路重新接一遍——`quotes_reference_table` 是生产函数本体，
不是尺子里重写的一份。这样量出来的附录，与重出后那份**逐字相同**。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for extra in (ROOT, ROOT / "scripts" / "acceptance" / "rpt1"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from rpt1_polish import ReadOnlyStore, work_draft_path  # noqa: E402

from app.report.polish.run import (  # noqa: E402
    PROGRAM_APPENDIX_HEADINGS, QUOTES_HEADING, quotes_reference_table)
from app.report.polish.tables import collect_inputs  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "check_polished", ROOT / "scripts" / "acceptance" / "rpt1" / "check_polished.py")
check_polished = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_polished)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--runs", required=True)
    ap.add_argument("--id", required=True)
    ap.add_argument("--template", default="consulting")
    args = ap.parse_args()

    runs_root = Path(args.runs).resolve()
    store = ReadOnlyStore(Path(args.db).resolve())
    report = store.get_report(args.id)
    draft = work_draft_path(runs_root, args.id, report["report_path"]).read_text("utf-8")
    built = collect_inputs(store, args.id, draft)

    block = quotes_reference_table(built["tables"])
    rows = (built["tables"].get("quotes") or {}).get("rows") or []
    table_rows = [ln for ln in block.splitlines()
                  if ln.startswith("|") and not set(ln) <= set("|- ")][1:]

    # 现成的正式稿：写手那部分原样，程序块按生产顺序重接（原声块插在信息源清单之前）。
    md = (runs_root / args.id / "exports" / f"{args.id}.polished.{args.template}.md")
    current = md.read_text("utf-8")
    marker = "\n## 信息源清单"
    cut = current.rfind(marker)
    rebuilt = (current[:cut + 1] + block + "\n" + current[cut + 1:]
               if cut >= 0 else current.rstrip() + "\n\n" + block)

    body = check_polished.writer_body(rebuilt)
    out = {
        "① 附录里真出现那张表": QUOTES_HEADING in rebuilt,
        "① 行数与 tables.json 的 quotes/rows 一致": (len(table_rows), len(rows),
                                              len(table_rows) == len(rows)),
        "② 正文里没有同一张表": check_polished.check_quotes_table_not_in_body(rebuilt) == [],
        "② 这张表落在程序块内（已被 writer_body 剜掉）": QUOTES_HEADING not in body,
        "③ 互动量是真数": [r.get("互动量") for r in rows],
        "④ 夸竞品那句不在表里": all("Kimi" not in str(r.get("原声")) for r in rows),
        "程序块清单": list(PROGRAM_APPENDIX_HEADINGS),
        "写手正文字符数（重接前/后应相同）": (
            len(check_polished.writer_body(current)), len(body)),
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    print("\n--- 附录块前 8 行 ---")
    print("\n".join(block.splitlines()[:8]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
