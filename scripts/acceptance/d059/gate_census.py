#!/usr/bin/env python3
"""§D-059 量尺：原声实体闸到底放行了哪些句子——**量在生产那条路上**。

    python3 scripts/acceptance/d059/gate_census.py --db <库> --runs <runs 根> \
        --id r-3e04f808dffd --out var/d059/census-before.json

⛔ 不自己实现「这句点没点名」。名单走 `polish/tables.py` 里生产那一行表达式、
判定走 `coding._names_the_entity`、成表走 `coding.polish_tables`——量出来的
和正式稿里摆出来的是同一套字。自造一把尺子量出来的绿是假绿（§verification-ruler）。

分三堆（堆的定义与判据口径一一对应）：
  `names_subject` 点名了研究主体（豆包/Doubao）           ← 判据 2：一条都不许误伤
  `only_rival`    只点名竞品、没提主体                     ← 判据 1/3：修前该收是红，修后必须全排除
  `names_none`    谁都没点                                 ← 加闸前后都该排除
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.reliability.coding import _names_the_entity, coded_rows, polish_tables  # noqa: E402
from app.report.polish.tables import _entity_aliases  # noqa: E402

#: 研究主体的叫法，**人工写死在量尺里**。⛔ 有意不调用被测代码算这份名单——
#: 尺子要独立于被测对象，拿被测函数量被测函数，改坏了也量不出来。
SUBJECT_NAMES = ("豆包", "Doubao", "豆包AI", "豆包大模型", "字节豆包")


# ⛔ 不另写一个 store：直接 import 生产整理正式稿用的那一个。
# 照抄一份出来，改了一边忘了另一边，量出来的就不是生产（§D-059 货 2 正是这么躲过一天的）。
sys.path.insert(0, str(ROOT / "scripts" / "acceptance" / "rpt1"))
from rpt1_polish import ReadOnlyStore  # noqa: E402


def _plan_of(report) -> dict:
    plan = report.get("plan_snapshot") or {}
    return json.loads(plan) if isinstance(plan, str) else plan


def _production_entity_names(plan) -> list[str]:
    """⚠️ 这一行必须与 `polish/tables.py` 里喂给 `polish_tables` 的那一行同形。

    修复前后它的值会变——**变的正是这包要改的东西**，所以尺子照抄表达式、
    不照抄结果。"""
    from app.report.polish.tables import quote_gate_names

    return quote_gate_names(plan)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--runs", required=True)
    ap.add_argument("--id", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    store = ReadOnlyStore(Path(args.db).resolve())
    report = store.get_report(args.id)
    if report is None:
        raise SystemExit(f"× 库里没有 {args.id}")
    plan = _plan_of(report)
    rows = store.list_evidence(args.id)

    accepted = _production_entity_names(plan)
    citations = {str(r.get("id")): int(r["citation_no"]) for r in rows
                 if r.get("citation_no") is not None}

    coded = coded_rows(rows)
    buckets = {"names_subject": [], "only_rival": [], "names_none": []}
    for item in coded:
        quote = item["coding"].get("quote") or ""
        if not quote:
            continue
        subject = _names_the_entity(quote, SUBJECT_NAMES)
        passes = _names_the_entity(quote, accepted)
        rec = {"evidence_id": str(item.get("id")), "platform": item.get("platform"),
               "citation_no": citations.get(str(item.get("id"))),
               "attitude": item["coding"].get("attitude"),
               "quote": quote, "闸放行": passes}
        if subject:
            buckets["names_subject"].append(rec)
        elif passes:
            buckets["only_rival"].append(rec)
        else:
            buckets["names_none"].append(rec)

    # 真出表：判据 3「22 条全部被排除」要落在**摆出来的那张表**上，不只落在闸上。
    tables = polish_tables(rows, citations=citations, entity_names=accepted)
    in_table = [{"评级": r.get("主题") or r.get("场景"), "原声": r.get("原声"),
                 "marks": r.get("marks")}
                for r in (tables.get("quotes") or {}).get("rows", [])]
    rival_quotes = {r["quote"] for r in buckets["only_rival"]}
    rival_in_table = [r for r in in_table if r["原声"] in rival_quotes]

    # ⭐ 判据 4：上面那段量的是「闸函数怎么判」，这段量的是「**生产真的喂了它什么**」。
    # 走 `collect_inputs` —— `rpt1_polish.py` 整理正式稿走的就是这个入口，一步不差；
    # 只在 `polish_tables` 上挂一层记录器把真实入参抄下来，不改行为、不付引擎钱。
    # ⛔ 不用「我自己算一遍名单」充数：那量的是尺子，不是生产。
    # 挂在 `app.reliability.coding` 上，不是挂在 tables 上：`build_tables` 是在函数体里
    # 现 import 的（`from app.reliability.coding import polish_tables`），所以要换的是**源头**那个属性。
    from app.reliability import coding as _coding_mod
    from app.report.polish.tables import collect_inputs

    seen: dict = {}
    original = _coding_mod.polish_tables

    def _recording(rows, *, citations=None, entity_names=None):
        seen["entity_names"] = list(entity_names or [])
        return original(rows, citations=citations, entity_names=entity_names)

    _coding_mod.polish_tables = _recording
    try:
        draft = (Path(args.runs) / args.id / "goals"
                 / Path(report["report_path"]).parent.name
                 / Path(report["report_path"]).name)
        built = collect_inputs(store, args.id, draft.read_text("utf-8"))
    finally:
        _coding_mod.polish_tables = original

    live_quotes = [r.get("原声") for r
                   in (built["tables"].get("quotes") or {}).get("rows", [])]
    chain = {
        "入口": "app.report.polish.tables.collect_inputs（= rpt1_polish.py 走的那个）",
        "真实喂给原声闸的名单": seen.get("entity_names"),
        "名单里有没有竞品": sorted(
            n for n in seen.get("entity_names", [])
            if not _names_the_entity(n, SUBJECT_NAMES)),
        "正式稿原声表条数": len(live_quotes),
        "⛔表里仍在夸竞品的原声": [q for q in live_quotes if q in rival_quotes],
    }

    out = {
        "report_id": args.id,
        "生产调用链实测": chain,
        "闸名单条数": len(accepted),
        "闸名单": accepted,
        "带原声已编码": sum(len(v) for v in buckets.values()),
        "计数": {k: len(v) for k, v in buckets.items()},
        "放行计数": {k: sum(1 for r in v if r["闸放行"]) for k, v in buckets.items()},
        "出表原声条数": len(in_table),
        "⛔出表的竞品原声": rival_in_table,
        "明细": buckets,
        "dropped": tables.get("dropped_quotes"),
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2), "utf-8")
    print(json.dumps({k: v for k, v in out.items() if k not in ("明细", "闸名单")},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
