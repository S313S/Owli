"""§SEL-2 货 2 判据 3：修前修后两版 `coding_entity_roles` 同时加载，喂全库 820 行逐条对照。

⛔ 只读：库一律 `mode=ro`，不写任何表。

**两版怎么来的**：旧版从 git 里把 base 那份 `tables.py` 原样取出来当独立模块加载
（`git show <base>:app/report/polish/tables.py`），新版直接 import 工作树的。
`tables.py` 全是绝对 import，两份模块里除了本文件自身的差异，依赖的都是同一批代码。
⛔ 不在本脚本里手抄一份旧逻辑当尺子（`verification-ruler-needs-verifying`）。

用法：
  ../Owli/.venv/bin/python scripts/acceptance/sel2/roles_diff.py [--base c5f7b7c] [--db PATH]
"""

from __future__ import annotations

import argparse
import collections
import importlib.util
import json
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

BASE = Path("/Users/xiaoci/Downloads/Workspace/VibeCoding/InformationCollection")
DEFAULT_DB = str(BASE / "Owli-src5/var/src5-backfill.db")
DEFAULT_REPORT = "r-3b3482ca7f8b"


def load_base_tables(base_rev: str):
    """把 base 那份 `tables.py` 取出来，作为独立模块加载。"""
    raw = subprocess.run(
        ["git", "-C", str(ROOT), "show", f"{base_rev}:app/report/polish/tables.py"],
        capture_output=True, check=True,
    ).stdout
    tmp = Path(tempfile.mkdtemp(prefix="sel2-base-")) / "tables_base.py"
    tmp.write_bytes(raw)
    spec = importlib.util.spec_from_file_location("sel2_tables_base", tmp)
    module = importlib.util.module_from_spec(spec)
    sys.modules["sel2_tables_base"] = module
    spec.loader.exec_module(module)
    return module


def load_rows(db: str, report_id: str) -> list[dict]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = []
    for row in conn.execute("SELECT * FROM evidence WHERE report_id = ? ORDER BY id",
                            (report_id,)).fetchall():
        item = dict(row)
        for field in ("author_meta", "raw_metrics", "norm_context", "extra"):
            if item.get(field) is not None:
                try:
                    item[field] = json.loads(item[field])
                except (ValueError, TypeError):
                    item[field] = {}
        rows.append(item)
    plan = json.loads(conn.execute("SELECT plan_snapshot FROM reports WHERE id = ?",
                                   (report_id,)).fetchone()[0])
    return rows, plan


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="c5f7b7c")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--report", default=DEFAULT_REPORT)
    args = ap.parse_args()

    rows, plan = load_rows(args.db, args.report)
    by_id = {str(r["id"]): r for r in rows}

    old_mod = load_base_tables(args.base)
    from app.report.polish import tables as new_mod          # 工作树这一版
    from app.reliability.parentage import parent_index

    old = old_mod.coding_entity_roles(plan, rows)
    new = new_mod.coding_entity_roles(plan, rows)
    parents = parent_index(rows)

    print(f"# §SEL-2 货 2 行为不变面 · {args.report} · base={args.base}")
    print(f"喂进去 {len(rows)} 行；旧版给出 {len(old)} 条、新版给出 {len(new)} 条")
    assert set(old) == set(new), "两版覆盖的 id 集合不同，先查这个"

    changed = [rid for rid in old if old[rid] != new[rid]]
    print(f"\n## 行为不变 {len(old)-len(changed)} / 有变 {len(changed)}")

    matrix = collections.Counter(
        (f"{old[r]['entity']}:{old[r]['entity_name']}",
         f"{new[r]['entity']}:{new[r]['entity_name']}") for r in changed)
    print("\n## 改判矩阵（旧 → 新 : 条数）")
    for (o, n), cnt in matrix.most_common():
        print(f"  {o:<18} → {n:<18} {cnt}")

    print(f"\n## 逐条有变的（{len(changed)} 条），每条给父帖点名原文")
    for rid in sorted(changed, key=lambda i: str(by_id[i].get("citation_no") or "zz")):
        row = by_id[rid]
        parent = parents.get(rid)
        ptext = new_mod._own_text(parent) if parent is not None else ""
        pnames = []
        if parent is not None:
            from app.plan.entities import mentions
            aliases = new_mod._entity_aliases(plan)
            pnames = [c for c, names in aliases.items()
                      if any(mentions(ptext, n) for n in names if len(str(n).strip()) >= 2)]
        mark = row.get("citation_no")
        print(f"\n  {rid} {'S%02d' % mark if mark else '(无角标)'} "
              f"{row.get('platform')} {row.get('goal_id')} kind={row.get('kind')}")
        print(f"    旧 {old[rid]['entity']}/{old[rid]['entity_name']}"
              f"  →  新 {new[rid]['entity']}/{new[rid]['entity_name']}")
        print(f"    本行自己的话: {new_mod._own_text(row)[:70]!r}")
        if parent is None:
            print("    父帖: (拿不到)")
        else:
            print(f"    父帖 {parent.get('id')} 点到 {pnames}；原文: {ptext[:90]!r}")

    # 归属分布总览
    print("\n## 归属分布（旧 → 新）")
    for label, res in (("旧", old), ("新", new)):
        cnt = collections.Counter(f"{v['entity']}:{v['entity_name']}" for v in res.values())
        print(f"  {label}: {dict(sorted(cnt.items()))}")

    dest = Path(__file__).resolve().parent / "roles-diff-out.json"
    dest.write_text(json.dumps({
        "unchanged": len(old) - len(changed), "changed": len(changed),
        "matrix": {f"{o} -> {n}": c for (o, n), c in matrix.items()},
        "changed_ids": changed,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n(落盘 {dest})")


if __name__ == "__main__":
    main()
