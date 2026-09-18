"""§SEL-2 货 3 + 货 4：在**库副本**上跑修前/修后两版编码表，逐格对照。

⛔ 只读原件：`../Owli-src5/var/src5-backfill.db` 一律 `mode=ro`；
需要可写副本时用 **sqlite backup API**（⛔ 不用 `cp`，会漏 WAL，见 `sqlite-cp-misses-wal`）。

两版怎么来：旧版从 git 取 base 那份 `tables.py` / `coding.py` 当独立模块加载，
新版用工作树的。⛔ 不手抄一份旧逻辑当尺子。

用法：
  ../Owli/.venv/bin/python scripts/acceptance/sel2/tables_diff.py [--base a7184dc]
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
SRC_DB = BASE / "Owli-src5/var/src5-backfill.db"
COPY_DB = BASE / "Owli-sel2/var/sel2-base.db"
REPORT = "r-3b3482ca7f8b"


def backup_copy(src: Path, dest: Path) -> None:
    """⚠️ 用 backup API，⛔ 不用 `cp`：`cp` 漏 WAL，读到旧状态，像「功能没生效」。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    target = sqlite3.connect(str(dest))
    with target:
        source.backup(target)
    source.close()
    target.close()


def load_base_module(base_rev: str, relpath: str, name: str):
    raw = subprocess.run(["git", "-C", str(ROOT), "show", f"{base_rev}:{relpath}"],
                         capture_output=True, check=True).stdout
    tmp = Path(tempfile.mkdtemp(prefix="sel2-base-")) / f"{name}.py"
    tmp.write_bytes(raw)
    spec = importlib.util.spec_from_file_location(name, tmp)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_rows(db: Path, report_id: str):
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
    conn.close()
    return rows, plan


def cells(tables: dict, name: str) -> dict:
    """把一张表压成 {(主键各列): (条数, marks)}，便于逐格对照。"""
    table = tables.get(name) or {}
    out = {}
    for row in table.get("rows") or []:
        key = tuple(str(row.get(col)) for col in (table.get("columns") or [])[:-1])
        out[key] = (row.get("条数"), tuple(row.get("marks") or []))
    return out, table.get("n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="a7184dc")
    args = ap.parse_args()

    print(f"# §SEL-2 货 3/货 4 · 库副本上的修前修后对照 · base={args.base}")
    backup_copy(SRC_DB, COPY_DB)
    print(f"库副本（backup API，⛔ 非 cp）：{COPY_DB}  {COPY_DB.stat().st_size} B")

    rows, plan = load_rows(COPY_DB, REPORT)
    by_id = {str(r["id"]): r for r in rows}

    old_tables = load_base_module(args.base, "app/report/polish/tables.py", "sel2_base_tables")
    from app.report.polish import tables as new_tables
    from app.reliability import coding as new_coding
    from app.reliability.parentage import parent_index

    old_roles = old_tables.coding_entity_roles(plan, rows)
    new_roles = new_tables.coding_entity_roles(plan, rows)
    parents = parent_index(rows)

    # ── 货 3 读数 1：改判矩阵 ────────────────────────────────────────
    changed = [rid for rid in old_roles if old_roles[rid] != new_roles[rid]]
    print(f"\n## 货 3 读数 1 · 归属改判：不变 {len(old_roles)-len(changed)} / 有变 {len(changed)}")
    matrix = collections.Counter(
        (f"{old_roles[r]['entity']}:{old_roles[r]['entity_name']}",
         f"{new_roles[r]['entity']}:{new_roles[r]['entity_name']}") for r in changed)
    for (o, n), cnt in matrix.most_common():
        print(f"  {o:<20} → {n:<20} {cnt}")

    # ── 货 3 读数 2：文心一言名下、父帖其实是 DeepSeek 的那一批 ─────────
    print("\n## 货 3 读数 2 · 旧记在「文心一言」名下、父帖点的却是 DeepSeek 的逐条")
    suspects = [r for r in changed
                if old_roles[r]["entity_name"] == "文心一言"
                and new_roles[r]["entity_name"] == "DeepSeek"]
    parent_group = collections.Counter(
        str(parents[r]["id"]) for r in suspects if r in parents)
    print(f"  共 {len(suspects)} 条；按父帖分组 {dict(parent_group)}")
    for rid in suspects:
        row, parent = by_id[rid], parents.get(rid)
        ptext = new_tables._own_text(parent) if parent is not None else ""
        print(f"\n  {rid} S{row.get('citation_no') or '--'} {row.get('platform')} "
              f"goal={row.get('goal_id')} agent={row.get('agent_name')}")
        print(f"    旧 {old_roles[rid]['entity_name']} → 新 {new_roles[rid]['entity_name']}")
        print(f"    评论自己的话: {new_tables._own_text(row)[:60]!r}")
        print(f"    父帖 {parent.get('id') if parent else None} 点名原文: {ptext[:100]!r}")

    # ── 货 3 读数 3：三张编码表逐格 ─────────────────────────────────
    print("\n## 货 3 读数 3 · 三张编码表逐格（修前 → 修后）")
    citations = {str(r.get("id")): int(r["citation_no"]) for r in rows
                 if r.get("citation_no") is not None}
    grade_by_mark = {int(r["citation_no"]): r.get("grade") for r in rows
                     if r.get("citation_no") is not None}
    names = new_tables.quote_gate_names(plan)
    kwargs = dict(citations=citations, entity_names=names, grade_by_mark=grade_by_mark)
    old_out = new_coding.polish_tables(rows, entity_roles=old_roles, **kwargs)
    new_out = new_coding.polish_tables(rows, entity_roles=new_roles, **kwargs)
    for name in ("attitude_by_topic", "scenario_attitude", "contrast_attitude"):
        o_cells, o_n = cells(old_out, name)
        n_cells, n_n = cells(new_out, name)
        print(f"\n### {name}  n: {o_n} → {n_n}")
        for key in sorted(set(o_cells) | set(n_cells)):
            o = o_cells.get(key, ("—", ()))
            n = n_cells.get(key, ("—", ()))
            flag = "" if o == n else "   ← 变了"
            if o == n:
                continue
            print(f"  {'/'.join(key):<24} 条数 {o[0]} → {n[0]}；"
                  f"marks {list(o[1])} → {list(n[1])}{flag}")

    # ── 货 4：S26 三问 ──────────────────────────────────────────────
    print("\n## 货 4 · S26 三问")
    s26 = next((r for r in rows if r.get("citation_no") == 26), None)
    print(f"  行 {s26['id']} kind={s26['kind']} platform={s26['platform']} "
          f"grade={s26['grade']} citation_no={s26['citation_no']}")
    print(f"  旧归属 {old_roles[str(s26['id'])]} → 新归属 {new_roles[str(s26['id'])]}")
    from app.reliability.relevance import rows_naming_entities
    gate_new = {str(r["id"]) for r in rows_naming_entities(rows, plan)}
    old_relevance = load_base_module(args.base, "app/reliability/relevance.py", "sel2_base_rel")
    gate_old = {str(r["id"]) for r in old_relevance.rows_naming_entities(rows, plan)}
    print(f"  撰写期相关闸：旧 {'过' if str(s26['id']) in gate_old else '挡'} "
          f"→ 新 {'过' if str(s26['id']) in gate_new else '挡'}")
    subj_old = sum(1 for v in old_roles.values() if v["entity"] == new_tables.ENTITY_SUBJECT)
    subj_new = sum(1 for v in new_roles.values() if v["entity"] == new_tables.ENTITY_SUBJECT)
    print(f"  全库判成「主体」的行：{subj_old} → {subj_new}")
    print(f"  attitude_by_topic 分母 n：{cells(old_out,'attitude_by_topic')[1]} "
          f"→ {cells(new_out,'attitude_by_topic')[1]}")
    for label, out in (("修前", old_out), ("修后", new_out)):
        cell = next((r for r in (out.get("attitude_by_topic") or {}).get("rows") or []
                     if r.get("主题") == "功能与能力" and r.get("态度") == "中"), None)
        print(f"  「功能与能力·中」{label}：条数 {cell and cell.get('条数')}，"
              f"marks {cell and cell.get('marks')}")
    still_in_pool = s26.get("citation_no") is not None
    print(f"\n  ⚠️ 只重出正式稿（不重跑撰写期）时，S26 还在不在信息源池表："
          f"{'**还在**' if still_in_pool else '不在'}"
          f"（`citation_no={s26.get('citation_no')}` 是库里已经写死的；"
          f"`tables.py` 的 sources 只按 `citation_no is not None` 收）")

    # ── 货 4 单独归因：把词表清空 = 只有货 2 的世界 ──────────────────
    # ⚠️ 上面那些 n 的大变化几乎全是货 2 带来的，⛔ 别把两件事糊成一件。
    print("\n## 货 4 单独归因（词表清空 = 只货 2；有词表 = 货 2+货 4）")
    import app.reliability.platform_noise as noise
    saved = noise.PLATFORM_UI_AFFORDANCES
    noise.PLATFORM_UI_AFFORDANCES = ()
    try:
        h2_only_roles = new_tables.coding_entity_roles(plan, rows)
        h2_only = new_coding.polish_tables(rows, entity_roles=h2_only_roles, **kwargs)
        gate_h2 = {str(r["id"]) for r in rows_naming_entities(rows, plan)}
    finally:
        noise.PLATFORM_UI_AFFORDANCES = saved
    only_h4 = [rid for rid in h2_only_roles if h2_only_roles[rid] != new_roles[rid]]
    print(f"  只货 2 → 货 2+货 4，归属再变 {len(only_h4)} 条：{only_h4}")
    print(f"  相关闸：只货 2 放行 {len(gate_h2)} 行 → 货 2+货 4 放行 {len(gate_new)} 行")
    print(f"  attitude_by_topic 分母 n：base {cells(old_out,'attitude_by_topic')[1]} → "
          f"只货 2 {cells(h2_only,'attitude_by_topic')[1]} → "
          f"货 2+货 4 {cells(new_out,'attitude_by_topic')[1]}")
    for label, out in (("只货 2", h2_only), ("货 2+货 4", new_out)):
        cell = next((r for r in (out.get("attitude_by_topic") or {}).get("rows") or []
                     if r.get("主题") == "功能与能力" and r.get("态度") == "中"), None)
        print(f"  「功能与能力·中」{label}：条数 {cell and cell.get('条数')}，"
              f"marks {cell and cell.get('marks')}")

    # ── 货 3 读数 4：assign_coding_entities 会不会刷新旧值 ────────────
    print("\n## 货 3 读数 4 · assign_coding_entities 在副本上跑一次")
    from app.store.dao import Store
    store = Store(COPY_DB)
    first = new_coding.assign_coding_entities(store, REPORT)
    second = new_coding.assign_coding_entities(store, REPORT)
    print(f"  第一次写 {first} 行；第二次写 {second} 行（判法没变就该是 0）")


if __name__ == "__main__":
    main()
