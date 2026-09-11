#!/usr/bin/env python3
"""§D-060 量尺：附录两表口径离线自证（零引擎、⛔ 不重出正式稿）。

    python3 scripts/acceptance/d060/appendix_check.py --db <库> --runs <runs 根> --id <研究 id> \
        [--template consulting] [--out <目录>]

做法沿用 §D-059 `appendix_check.py`：拿现成正式稿当写手产物，用生产函数本体
（`collect_inputs` → `tables.json`、`missing_table`）把程序块重新接一遍，再上尺子。
- 货 1：用**重算的** tables.json 跑 `check_polished` ⑭（现成那份没有 `title_independent`，
  尺子按老行为读，对照组就是它）。
- 货 2：打印 `missing_table` 旧口径（只给 objectives）与新口径（带 chapters）两份，供逐行对照。
库只读；`--out` 默认写到系统临时目录，⛔ 不碰 runs 根。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for extra in (ROOT, ROOT / "scripts" / "acceptance" / "rpt1"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from rpt1_polish import ReadOnlyStore, work_draft_path  # noqa: E402

from app.report.polish.run import missing_table  # noqa: E402
from app.report.polish.tables import collect_inputs, is_speech_quote  # noqa: E402
from app.report.render import parse_report  # noqa: E402

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
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    runs_root = Path(args.runs).resolve()
    out_dir = Path(args.out or tempfile.mkdtemp(prefix="d060-")).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    store = ReadOnlyStore(Path(args.db).resolve())
    report = store.get_report(args.id)
    work_path = work_draft_path(runs_root, args.id, report["report_path"])
    draft = work_path.read_text("utf-8")
    built = collect_inputs(store, args.id, draft)

    exports = runs_root / args.id / "exports"
    md_path = exports / f"{args.id}.polished.{args.template}.md"
    old_tables = exports / f"{args.id}.polished.{args.template}.tables.json"
    new_tables = out_dir / old_tables.name
    new_tables.write_text(json.dumps(built, ensure_ascii=False, indent=2), encoding="utf-8")

    # —— 货 1：⑭ 对照（老 tables.json = 全部 title 进名单；新 = 只收独立标题）——
    key = "⑭ 原声是人说的话"
    before = check_polished.run(md_path, old_tables, work_path)[key] if old_tables.is_file() else None
    after = check_polished.run(md_path, new_tables, work_path)[key]
    sources = built["sources"]
    dependent = [s["mark"] for s in sources if not s.get("title_independent", True)]
    # 尺子还能响：拿一条**有独立标题**的源的标题当原声，必须被否决。
    titles = [str(s.get("title") or "") for s in sources if s.get("title_independent", True)]
    probe = next((t for t in titles if len(t) >= 8), None)
    still_rings = (not is_speech_quote(probe, titles)) if probe else None

    # —— 货 2：缺失表旧/新口径 ——
    missing = parse_report(draft).get("missing") or []
    old_md = missing_table(missing, built.get("objectives") or [])
    try:
        new_md = missing_table(missing, built.get("objectives") or [],
                               chapters=built.get("chapters") or [])
    except TypeError:       # 货 2 未落地时也能跑货 1 的对照
        new_md = "（missing_table 尚无 chapters 参数——货 2 未落地）\n"

    print(json.dumps({
        "tables.json（重算）": str(new_tables),
        "货1 ⑭ 改前（现成 tables.json）": before,
        "货1 ⑭ 改后（重算 tables.json）": after,
        "货1 无独立标题的源": dependent,
        "货1 尺子仍能响（真标题当原声被否决）": still_rings,
        "货1 探针标题": probe,
    }, ensure_ascii=False, indent=2))
    print("\n--- 货2 缺失表·旧口径 ---\n" + old_md)
    print("--- 货2 缺失表·新口径 ---\n" + new_md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
