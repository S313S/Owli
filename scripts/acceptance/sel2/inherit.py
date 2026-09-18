"""§SEL-2 货 1 只算不改：评论继承父帖相关性之后，池会变成什么样。

⛔ 只读：库一律 `mode=ro`，不写任何表、不落任何事件、不碰 `../Owli-src5` 的任何文件。

**两侧用同一份实现量**：新旧行为都走 `rows_naming_entities(..., inherit_parent=)`，
⛔ 不在本脚本里另写一份继承逻辑当尺子（`verification-ruler-needs-verifying`）。
层 2~4 逐字沿用 `../Owli-sel1/scripts/acceptance/sel1/funnel.py`（commit eb8019d）的读法。

用法：
  ../Owli/.venv/bin/python scripts/acceptance/sel2/inherit.py [--db PATH] [--report r-xxxx]
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from app.orchestrator.sectioning import (  # noqa: E402
    EVIDENCE_POOL_LIMIT,
    SECTION_EVIDENCE_POOL_LIMIT,
    _evidence_grade,
    _numbered_evidence_rows,
    _rating_sort_key,
    _section_evidence_rows,
)
from app.reliability.relevance import entity_names_of, rows_naming_entities  # noqa: E402

BASE = Path("/Users/xiaoci/Downloads/Workspace/VibeCoding/InformationCollection")
DEFAULT_DB = str(BASE / "Owli-src5/var/src5-backfill.db")
DEFAULT_RUN = str(BASE / "Owli-src5/var/runs/r-3b3482ca7f8b")
DEFAULT_REPORT = "r-3b3482ca7f8b"

MARK = re.compile(r"\[S(\d{2})\]")
#: 与 `../Owli-sel1/scripts/acceptance/sel1/citations.py` 同一份名单（程序直填的附录表）。
PROGRAM_TABLE_HEADINGS = {
    "方法与样本", "假设与不确定性",
    "把握度读数（主张的交叉验证与被引证据等级）", "哪些没采到", "各表口径",
    "词表命中参考（只数触发词，不是情感判断）",
    "代表原声（逐字摘录，按互动量排序）",
    "对照实体的评论（只作参照，不计入正文态度表）",
    "证据的时间范围", "信息源清单",
}
#: §SEL-1 说的四个「薄主题」。
THIN_TOPICS = ("办公", "付费", "会员", "订阅")


def connect(db: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def load_evidence(conn: sqlite3.Connection, report_id: str) -> list[dict]:
    """逐字照抄 `Store.list_evidence` 的读法（SELECT * ORDER BY id + JSON 解列）。"""
    out = []
    for row in conn.execute("SELECT * FROM evidence WHERE report_id = ? ORDER BY id",
                            (report_id,)).fetchall():
        item = dict(row)
        for field in ("author_meta", "raw_metrics", "norm_context", "extra"):
            if item.get(field) is not None:
                item[field] = json.loads(item[field])
        out.append(item)
    return out


def tally(rows, key) -> dict:
    out: dict[str, int] = collections.Counter()
    for row in rows:
        out[str(row.get(key) or "(空)")] += 1
    return dict(sorted(out.items()))


def grade_tally(rows) -> dict:
    out: dict[str, int] = collections.Counter()
    for row in rows:
        out[_evidence_grade(row) or "(空)"] += 1
    return dict(sorted(out.items()))


def section_specs(conn: sqlite3.Connection, report_id: str) -> list[tuple[str, str, str]]:
    """实跑落库的节清单：(section_id, goal_id, chapter_kind)。"""
    specs = []
    for _seq, payload in conn.execute(
        "SELECT sequence, payload FROM events WHERE research_id = ? "
        "AND type = 'section_pool_composed' ORDER BY sequence", (report_id,)
    ):
        data = json.loads(payload)["data"]
        specs.append((data["section_id"], data["goal_id"], data.get("chapter_kind")))
    return specs


def run_funnel(rows, plan, specs, *, inherit: bool) -> dict:
    """层 1~4 走一遍，返回各层结果。层 2~4 与 funnel.py 逐字同构。"""
    named = rows_naming_entities(rows, plan, inherit_parent=inherit) or rows
    keepable = [r for r in named if _evidence_grade(r) != "D"]
    ordered = sorted(keepable or named,
                     key=lambda row: (str(row.get("goal_id") or ""), *_rating_sort_key(row)))
    numbered, quotas, actual, _floor = _numbered_evidence_rows(ordered)
    citation_by_id = {str(r["id"]): i for i, r in enumerate(numbered, start=1)}
    pools: dict[str, list[dict]] = {}
    for sid, gid, kind in specs:
        eligible = [r for r in numbered if str(r.get("goal_id") or "") == gid]
        if not eligible and numbered:
            eligible = numbered
        visible = _section_evidence_rows(eligible, gid, chapter_kind=kind)
        visible.sort(key=lambda r: citation_by_id[str(r["id"])])
        pools[sid] = visible
    union_ids = {str(r["id"]) for pool in pools.values() for r in pool}
    return {
        "named": named, "keepable": keepable, "numbered": numbered,
        "quotas": quotas, "actual": actual, "pools": pools,
        "union_ids": union_ids,
        "union_rows": [r for r in numbered if str(r["id"]) in union_ids],
        "citation_by_id": citation_by_id,
        "sections_of": {
            rid: sorted(sid for sid, pool in pools.items()
                        if rid in {str(r["id"]) for r in pool})
            for rid in union_ids
        },
    }


def body_cited_marks(run: Path, rid: str) -> tuple[set[str], dict]:
    """正式稿**正文**（非程序附录表）真引到的角标，与 tables.json 的 sources 映射。"""
    tables = json.loads((run / f"exports/{rid}.polished.consulting.tables.json").read_text())
    src_by_mark = {s["mark"]: s for s in tables["sources"]}
    text = (run / f"exports/{rid}.polished.consulting.md").read_text()
    heading, buf, body = "(开头)", [], set()
    chunks = []
    for line in text.splitlines():
        if line.startswith("## ") and not line.startswith("### "):
            chunks.append((heading, "\n".join(buf)))
            heading, buf = line[3:].strip(), []
        else:
            buf.append(line)
    chunks.append((heading, "\n".join(buf)))
    for head, chunk in chunks:
        if head in PROGRAM_TABLE_HEADINGS:
            continue
        body |= {f"S{int(x):02d}" for x in MARK.findall(chunk)}
    return body, src_by_mark


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--run", default=DEFAULT_RUN)
    ap.add_argument("--report", default=DEFAULT_REPORT)
    args = ap.parse_args()

    conn = connect(args.db)
    plan = json.loads(conn.execute("SELECT plan_snapshot FROM reports WHERE id = ?",
                                   (args.report,)).fetchone()[0])
    rows = load_evidence(conn, args.report)
    by_id = {str(r["id"]): r for r in rows}
    specs = section_specs(conn, args.report)

    print(f"# §SEL-2 货 1 · 评论继承父帖相关性 · {args.report}")
    print(f"库 {args.db}\n实体叫法({len(entity_names_of(plan))}): {entity_names_of(plan)}")
    print(f"层 0 库内证据 {len(rows)}；kind {tally(rows,'kind')}；平台 {tally(rows,'platform')}")

    old = run_funnel(rows, plan, specs, inherit=False)
    new = run_funnel(rows, plan, specs, inherit=True)

    # ── 读数 1：放进来多少 ─────────────────────────────────────────
    print("\n## 读数 1 放进来多少（层 1 相关性硬闸）")
    for label, res in (("旧(不继承)", old), ("新(继承父帖)", new)):
        n = res["named"]
        print(f"  {label}: 进 {len(n)} / 挡 {len(rows)-len(n)}")
        print(f"      按平台 {tally(n,'platform')}")
        print(f"      按 kind {tally(n,'kind')}")
        print(f"      按等级 {grade_tally(n)}")
    gained = [by_id[i] for i in
              {str(r['id']) for r in new['named']} - {str(r['id']) for r in old['named']}]
    print(f"  ⇒ 新放进来 {len(gained)} 条")
    print(f"      按平台 {tally(gained,'platform')}")
    print(f"      按 kind {tally(gained,'kind')}")
    print(f"      按等级 {grade_tally(gained)}")
    print(f"      其中非 D：{sum(1 for r in gained if _evidence_grade(r)!='D')}")
    print(f"  层 2 D 闸后：旧 {len(old['keepable'])} → 新 {len(new['keepable'])}")
    print(f"  层 3 编号池(上限 {EVIDENCE_POOL_LIMIT})：旧 {len(old['numbered'])} → 新 {len(new['numbered'])}")
    print(f"      旧 quotas {old['quotas']} / actual {old['actual']}")
    print(f"      新 quotas {new['quotas']} / actual {new['actual']}")
    print(f"  层 4 每节可见池(上限 {SECTION_EVIDENCE_POOL_LIMIT}) 并集："
          f"旧 {len(old['union_ids'])} → 新 {len(new['union_ids'])}")
    print(f"      旧并集按平台 {tally(old['union_rows'],'platform')}")
    print(f"      新并集按平台 {tally(new['union_rows'],'platform')}")
    print(f"      旧并集按 kind {tally(old['union_rows'],'kind')}")
    print(f"      新并集按 kind {tally(new['union_rows'],'kind')}")

    # ── 读数 2：挤掉谁 ─────────────────────────────────────────────
    body, src_by_mark = body_cited_marks(Path(args.run), args.report)
    db_mark = {str(r["id"]): r["citation_no"] for r in rows if r.get("citation_no") is not None}
    print(f"\n## 读数 2 挤掉谁（正文实引 {len(body)} 条：{sorted(body)}）")
    evicted = sorted(old["union_ids"] - new["union_ids"],
                     key=lambda i: old["citation_by_id"][i])
    admitted = new["union_ids"] - old["union_ids"]
    print(f"  旧池 {len(old['union_ids'])} → 新池 {len(new['union_ids'])}；"
          f"挤掉 {len(evicted)}、新进 {len(admitted)}")
    print(f"  挤掉的按平台 {tally([by_id[i] for i in evicted],'platform')}；"
          f"按 kind {tally([by_id[i] for i in evicted],'kind')}")
    print(f"  新进的按平台 {tally([by_id[i] for i in admitted],'platform')}；"
          f"按 kind {tally([by_id[i] for i in admitted],'kind')}")
    hit_body = []
    print("\n  逐条（复算号 / 库角标 / 平台 / 等级 / 所在节 / 是否正文实引）：")
    for rid in evicted:
        row = by_id[rid]
        recalc = f"S{old['citation_by_id'][rid]:02d}"
        real = db_mark.get(rid)
        real_mark = f"S{int(real):02d}" if real is not None else "—"
        in_body = real_mark in body
        if in_body:
            hit_body.append(real_mark)
        print(f"    {recalc} 库{real_mark} {row.get('platform'):<12} "
              f"{_evidence_grade(row) or '?'} {','.join(old['sections_of'][rid]):<28} "
              f"{'★正文实引' if in_body else ''} {str(row.get('title'))[:34]}")
    print(f"\n  ⇒ 挤掉的里面有 {len(hit_body)} 条是当前正文实引的 "
          f"{len(body)} 条之一：{sorted(hit_body)}")

    # ── 读数 3：薄主题救不救得回 ───────────────────────────────────
    print("\n## 读数 3 薄主题（含关键词的证据：库 / 旧池 / 新池）")
    for topic in THIN_TOPICS:
        def has(row):
            return topic in f"{row.get('title') or ''}{row.get('content_excerpt') or ''}"
        lib = [r for r in rows if has(r)]
        o = [r for r in old["union_rows"] if has(r)]
        n = [r for r in new["union_rows"] if has(r)]
        print(f"  {topic}: 库 {len(lib)} / 旧池 {len(o)} / 新池 {len(n)}"
              f"   （库里 kind {tally(lib,'kind')}）")

    # ── 读数 4：对照实体救不救得回 ─────────────────────────────────
    print("\n## 读数 4 对照实体（goal-2..5 各自可进池条数）")
    # goal → 它那一章评的实体（`plan.goals[].agents[].entity`，与 `chapter_rows` 同一处读法）。
    entity_of: dict[str, str] = {}
    for idx, goal in enumerate(plan.get("goals") or [], start=1):
        ents = sorted({str(a.get("entity")) for a in (goal.get("agents") or []) if a.get("entity")})
        entity_of[str(goal.get("id") or f"goal-{idx}")] = "/".join(ents) or "(横向对比章)"
    goals = sorted({str(r.get("goal_id") or "") for r in rows})
    print(f"  goal→实体 {entity_of}")
    for gid in goals:
        lib = [r for r in rows if str(r.get("goal_id") or "") == gid]
        o = [r for r in old["union_rows"] if str(r.get("goal_id") or "") == gid]
        n = [r for r in new["union_rows"] if str(r.get("goal_id") or "") == gid]
        on = [r for r in old["numbered"] if str(r.get("goal_id") or "") == gid]
        nn = [r for r in new["numbered"] if str(r.get("goal_id") or "") == gid]
        print(f"  {gid} {entity_of.get(gid,'?'):<12}: 库 {len(lib):>3} | "
              f"编号池 旧 {len(on):>2} → 新 {len(nn):>2}"
              f" | 可见池 旧 {len(o):>2} → 新 {len(n):>2}")

    out = {
        "report_id": args.report,
        "old": {k: (len(v) if isinstance(v, (list, set)) else v)
                for k, v in old.items() if k in ("named", "keepable", "numbered", "union_ids")},
        "new": {k: (len(v) if isinstance(v, (list, set)) else v)
                for k, v in new.items() if k in ("named", "keepable", "numbered", "union_ids")},
        "evicted": evicted, "admitted": sorted(admitted),
        "evicted_body_marks": sorted(hit_body), "body_marks": sorted(body),
    }
    dest = Path(__file__).resolve().parent / "inherit-out.json"
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n(中间结果落盘 {dest})")


if __name__ == "__main__":
    main()
