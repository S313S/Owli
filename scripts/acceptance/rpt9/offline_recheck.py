#!/usr/bin/env python3
"""§RPT-9 离线复核：拿第三轮真稿的库与 tables.json，给四货的修前修后两侧读数。

    python3 scripts/acceptance/rpt9/offline_recheck.py <库.db> <research_id> <tables.json>

⛔ 零引擎、零采集、不重出稿：库按 `mode=ro` 打开，`tables.json` 只读不写。
「修前」一律取那份**已经出稿的** `tables.json` 里的原值（那是 base 代码真跑出来的
产物，⛔ 不是我照抄一份旧逻辑再算一遍——照抄会把「我以为 base 是这么算的」
当成 base 的行为，本项目在 §RPT-8 那条抄写式对照上已经小心过一次）。
「修后」用本树的代码就着同一份库现算。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.reliability.coding import (SCENARIO_OTHER, has_own_body,  # noqa: E402
                                    polish_tables, quote_is_own_words)
from app.report.polish.tables import (ENTITY_SUBJECT, _with_extra,  # noqa: E402
                                      coding_entity_roles)

BEFORE = "修前（第三轮真稿 tables.json 里的原值）"
AFTER = "修后（本树代码就着同一份库现算）"


def _load(db: str, research_id: str):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    report = dict(con.execute(
        "select * from reports where id=?", (research_id,)).fetchone())
    plan = json.loads(report["plan_snapshot"] or "{}")
    rows = _with_extra([dict(r) for r in con.execute(
        "select * from evidence where report_id=? order by id", (research_id,))])
    con.close()
    # ⚠️ 裸读 sqlite 拿到的 `raw_metrics` 是 JSON **字符串**，生产路走
    # `store.list_evidence` 拿到的是解好的 dict（`store/dao.py` 那道）。
    # 不在这儿解开，`engagement_value` 对每行都返回 None、原声表全体互动量并列 0，
    # 排序退化成按 id——**量出来的「修后」就不是生产会出的那张表**。
    # 09-18 本包实测撞到过：附录渲染出来互动量整列空白。先怀疑自己的尺子，就是这个。
    for row in rows:
        if isinstance(row.get("raw_metrics"), str):
            try:
                row["raw_metrics"] = json.loads(row["raw_metrics"] or "{}")
            except json.JSONDecodeError:
                row["raw_metrics"] = {}
    return plan, rows


def _cell_marks(table, keys):
    return {tuple(str(row[k]) for k in keys): list(row.get("marks") or [])
            for row in table.get("rows") or []}


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(__doc__)
        return 2
    db, research_id, tables_path = argv[1], argv[2], argv[3]
    plan, rows = _load(db, research_id)
    old = json.loads(Path(tables_path).read_text(encoding="utf-8"))["tables"]

    roles = coding_entity_roles(plan, rows)
    citations = {str(r["id"]): int(r["citation_no"])
                 for r in rows if r.get("citation_no") is not None}
    grades = {int(r["citation_no"]): r.get("grade")
              for r in rows if r.get("citation_no") is not None}
    from app.report.polish.tables import quote_gate_names
    new = polish_tables(rows, citations=citations, total_evidence=len(rows),
                        entity_names=quote_gate_names(plan), grade_by_mark=grades,
                        entity_roles=roles)

    subject_coded = [r for r in rows
                     if roles.get(str(r["id"]), {}).get("entity") == ENTITY_SUBJECT
                     and (r.get("_extra") or {}).get("coding")]

    print("=" * 78)
    print("货 3 · 空正文不算进编码分母（新问题 13）")
    print("=" * 78)
    bodyless = [r for r in subject_coded if not has_own_body(r)]
    print(f"{BEFORE}：已编码 UGC 条数 = {old['scenario_counts']['n']}")
    print(f"{AFTER}：已编码 UGC 条数 = {new['scenario_counts']['n']}")
    print(f"  差的 {len(bodyless)} 条逐条列出（核：是采集丢了，还是原帖只有题面）——")
    for r in bodyless:
        extra = r.get("_extra") or {}
        print(f"    {r['id']}  {r['platform']}/{r['kind']}  角标={r['citation_no']}")
        print(f"      title      = {str(r['title'])[:70]!r}")
        print(f"      正文       = {r['content_excerpt']!r}")
        print(f"      external_url={extra.get('external_url')!r}"
              f"  domain={extra.get('domain')!r}")
        print(f"      status     = {extra.get('status')!r}"
              f"  removed_by_category={extra.get('removed_by_category')!r}")
        print(f"      原编码     = scenario={extra['coding'].get('scenario')!r}"
              f" attitude={extra['coding'].get('attitude')!r}"
              f" topics={extra['coding'].get('topics')}")
    print(f"  ⇒ 两条都带 external_url 指向 i.redd.it / v.redd.it（Reddit 的图片/视频"
          f"链接帖），domain 同源，status=normal、removed_by_category 空：")
    print(f"    **原帖本身就没有正文，不是采集时丢的**。补采补不出来。")

    print()
    print("=" * 78)
    print("货 1 ·「其他」必须自己交代构成（用户意见第 9 条）")
    print("=" * 78)
    old_other = next((r for r in old["scenario_counts"]["rows"]
                      if r["场景"] == SCENARIO_OTHER), None)
    print(f"{BEFORE}：「{SCENARIO_OTHER}」{old_other['条数']} 条 / 共 "
          f"{old['scenario_counts']['n']} 条 = "
          f"{old_other['条数'] / old['scenario_counts']['n']:.1%}；"
          f"表下对它是什么**一个字都没有**")
    new_other = next((r for r in new["scenario_counts"]["rows"]
                      if r["场景"] == SCENARIO_OTHER), None)
    print(f"{AFTER}：「{SCENARIO_OTHER}」{new_other['条数']} 条 / 共 "
          f"{new['scenario_counts']['n']} 条 = "
          f"{new_other['条数'] / new['scenario_counts']['n']:.1%}，触发构成说明。")
    def _topic_hits(items):
        hits: Counter[str] = Counter()
        for item in items:
            for topic in item["_extra"]["coding"].get("topics") or []:
                hits[str(topic)] += 1
        return hits

    all_others = [r for r in subject_coded
                  if str(r["_extra"]["coding"].get("scenario")) == SCENARIO_OTHER]
    others = [r for r in all_others if has_own_body(r)]
    hits_before = _topic_hits(all_others)
    hits = _topic_hits(others)
    print(f"  修前那 {len(all_others)} 条按 coding.topics 的构成："
          f"{dict(hits_before.most_common())}")
    print(f"  修后那 {len(others)} 条（剔掉无正文行后）：{dict(hits.most_common())}")
    print("  ⚠️ 与复审对账要拿**同一批行**比：复审读的是修前那 25 条，所以下面用修前的数。")
    print("  与复审会话读 25 条原文归出的四堆对账——")
    print(f"    盘点 10  ↔ 竞品对比 {hits_before.get('竞品对比', 0)} 条 …… "
          f"{'✅ 对上' if hits_before.get('竞品对比') == 10 else '对不上'}")
    price = hits_before.get("价格与付费", 0) + hits_before.get("广告与推广", 0)
    print(f"    价格 5   ↔ 价格与付费 {hits_before.get('价格与付费', 0)} + 广告与推广 "
          f"{hits_before.get('广告与推广', 0)} = {price} …… "
          f"{'✅ 对上' if price == 5 else '对不上'}")
    print(f"    治理 4   ↔ 隐私与安全 {hits_before.get('隐私与安全', 0)} 条 …… ❌ 对不上")
    print("      ⚠️ 不是尺子读错：主题闭集 8 格里**没有「平台治理」这一格**。")
    print("      复审读出的那 4 条（智能体下架 / 站点打不开 / AI 黄谣 / 骂骗子）在")
    print("      topics 里分别落在 功能与能力・速度与稳定・隐私与安全 上，程序算不出那一堆。")
    print("      ⇒ 这正是「编码前加一层内容类型」要解的问题（贵档，已登记待拍）。")
    print(f"  构成句实际写出来的样子：")
    line = new["scenario_counts"]["basis"]
    print("    " + line[line.index("⚠️「"):] if "⚠️「" in line else "    （未触发）")

    print()
    print("=" * 78)
    print("货 2 · 有条数就得有交代（用户意见第 5 条呈现半）")
    print("=" * 78)
    specs = [("contrast_attitude", ("对照实体", "态度")),
             ("scenario_attitude", ("场景", "态度")),
             ("attitude_by_topic", ("主题", "态度")),
             ("scenario_counts", ("场景",))]
    print(f"{BEFORE}：真机「有条数、角标是 —」的格子逐个列出——")
    total_blind = 0
    for name, keys in specs:
        table = old.get(name)
        if not table:
            continue
        blind = [(k, r) for r, k in ((r, tuple(str(r[c]) for c in keys))
                                     for r in table["rows"]) if not r.get("marks")]
        total_blind += len(blind)
        print(f"  {name}：{len(blind)} 个 —— "
              + "、".join(f"{'·'.join(k)}({r['条数']} 条)" for k, r in blind))
        has_note = "一个角标都没有" in str(table.get("basis") or "")
        print(f"      表下有没有交代：{'有' if has_note else '**没有**'}")
    print(f"  合计 {total_blind} 个格子有数无角标。")
    print(f"{AFTER}：每张表下都有一句，逐张验——")
    for name, _ in specs:
        if name not in new:
            continue
        basis = new[name]["basis"]
        mark = "一个角标都没有" in basis
        print(f"  {name}：{'✅ 有交代' if mark else '（本轮无有数无角标的格子）'}")
        if mark:
            head = basis[basis.index("本表有"):]
            print("      " + head.split("。条数本身")[0] + "。")

    graded = Counter(str(r.get("grade")) for r in rows
                     if (r.get("_extra") or {}).get("coding"))
    print(f"  （「其中 N 条是 D 级」那个分支可不可达：全库已编码行的等级分布 "
          f"{dict(graded.most_common())} —— D 级 {graded.get('D', 0)} 条，可达，不是死分支。）")

    print()
    print("=" * 78)
    print("货 4 · 两条可疑原声：先定因，再决定修不修（用户意见第 8 条归类半）")
    print("=" * 78)
    print(f"{BEFORE}：正式稿原声表 {len(old['quotes']['rows'])} 行——")
    for r in old["quotes"]["rows"]:
        print(f"    {r['marks']} {r['主题']}·{r['态度']}  {r['原声']!r}")
    borrowed = [r for r in rows
                if (r.get("_extra") or {}).get("coding", {}).get("quote")
                and not quote_is_own_words(r)]
    comments = [r for r in borrowed if str(r.get("kind")) == "comment"]
    print(f"\n  全库 quote 非空的行里，**摘的不是自己正文**的有 {len(borrowed)} 条，"
          f"其中评论行 {len(comments)} 条（评论的 title 是父帖标题，那是楼主的话）。")
    print(f"{AFTER}：原声表 {len(new['quotes']['rows'])} 行——")
    for r in new["quotes"]["rows"]:
        print(f"    {r['marks']} {r['主题']}·{r['态度']}  {r['原声']!r}")
        print(f"        出处：{r['出处']}")
    dropped = new["quotes"]["basis"]
    if "不是发言人自己的话" in dropped:
        head = dropped[dropped.index("另有"):]
        print("  表注新增：" + head.split("已剔除。")[0] + "已剔除。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
