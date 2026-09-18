#!/usr/bin/env python3
"""§RPT-8 离线复核：拿第三轮真稿与它的 tables.json，给修前修后两侧读数。

    python3 scripts/acceptance/rpt8/offline_recheck.py <正式稿.md> <tables.json> [<库.db> <research_id>]

⛔ 零引擎、只读：不重跑写手、不重出稿、不写任何库（库按 `mode=ro` 打开）。
给了库就连货 4② 的线索一起算——`clues` 是本包新加进 `tables.json` 的键，
第三轮那份产物里没有，只能回库取 `claims` 现算（这本身就是「读数真的存在」的实证）。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.report.polish.run import (attitude_line, attitude_traceability,  # noqa: E402
                                   clues_block, confidence_line, confidence_tables,
                                   finding_confidences, overall_confidence,
                                   oversized_finding_lines, singlesource_advice,
                                   weakevidence_advice)
from app.report.polish.sharding import parse_findings  # noqa: E402
from app.report.polish.tables import _clues  # noqa: E402

BEFORE = "修前（base ed1e9f7 的读数，照抄自那一版代码的行为）"
AFTER = "修后"


def _old_confidence_line(tables, counts) -> str:
    """base ed1e9f7 的 `confidence_line`，原样抄来作对照——⛔ 不 import 旧代码，
    这棵树上已经没有它了；抄一份只为把两侧读数摆在一起，不参与任何判定。"""
    words = {"SINGLE": "单源", "WEAK": "偏弱", "PASS": "多源互证", "CONFLICT": "多源冲突"}
    table = (tables or {}).get("crossref_mix") or {}
    got = {str(r.get("交叉验证结论")): int(r.get("主张数") or 0) for r in table.get("rows") or []}
    pairs = [(k, got[k]) for k in ("SINGLE", "WEAK", "PASS", "CONFLICT") if got.get(k)]
    total = int(table.get("n") or 0)
    if not total or not pairs:
        return ""
    body = " / ".join(f"{words[k]} {n}" for k, n in pairs)
    cited, pool = counts.get("正文实引"), counts.get("cited")
    tail = (f"正文实际引用证据 {cited} 条"
            + (f"（引用池共 {pool} 条）" if pool != cited else "") + "。") if cited else ""
    return f"主张 {total} 条：{body}。{tail}"


def _section(markdown: str, head: str, stop: str) -> str:
    return markdown.split(head, 1)[-1].split(stop, 1)[0] if head in markdown else ""


def main(argv: list[str]) -> int:
    if len(argv) not in (3, 5):
        print(__doc__)
        return 2
    md = Path(argv[1]).read_text(encoding="utf-8")
    data = json.loads(Path(argv[2]).read_text(encoding="utf-8"))
    sources, tables, counts = data["sources"], data["tables"], data.get("counts") or {}
    opening = md.split("# 关键发现", 1)[0]
    findings = parse_findings(opening)

    print("=" * 78)
    print("货 1 把握度分层")
    print("=" * 78)
    print(f"{BEFORE}：整篇一个档，判法只看「多数说法是不是单源」——")
    print(f"  主张 {tables['crossref_mix']['n']} 条里单源 "
          f"{next(r['主张数'] for r in tables['crossref_mix']['rows'] if r['交叉验证结论'] == 'SINGLE')}"
          f" 条 ⇒ 恒为「低」，各条发现之间分不出强弱。")
    rows = finding_confidences(findings, sources, tables)
    print(f"{AFTER}：逐条算，四条判出三档——")
    for index, tier, reason in rows:
        print(f"  第 {index} 条 {tier}：{reason}")
    print(f"  整篇总括（各条中位档，偶数条偏低）＝「{overall_confidence([t for _, t, _ in rows])}」")

    print()
    print("=" * 78)
    print("货 2 内部计数行")
    print("=" * 78)
    print(f"{BEFORE}摘要这一行：{_old_confidence_line(tables, counts)}")
    print(f"{AFTER}摘要这一行：{confidence_line(tables, counts, findings, sources)}")
    block = confidence_tables(tables, findings, sources)
    moved = [l for l in block.splitlines() if l.startswith("主张共")]
    print(f"{AFTER}那串数落在附录：{moved[0] if moved else '（没找到）'}")

    print()
    print("=" * 78)
    print("货 3 标题不得大过证据")
    print("=" * 78)
    print(f"{BEFORE}：没有这道闸，读数为空。")
    problems = oversized_finding_lines(opening, sources, tables)
    print(f"{AFTER}：{len(problems)} 处")
    for problem in problems:
        print(f"  · {problem}")

    print()
    print("=" * 78)
    print("货 4① 负向可追溯")
    print("=" * 78)
    print(f"{BEFORE}态度行：已编码评论 …（没有可追溯性那一句）")
    print(f"{AFTER}态度行：{attitude_line(tables, data.get('subjects') or [])}")
    print(f"  单独看那一句：{attitude_traceability(tables)}")

    print()
    print("=" * 78)
    print("货 4② 单人线索节")
    print("=" * 78)
    if len(argv) == 5:
        con = sqlite3.connect(f"file:{argv[3]}?mode=ro", uri=True)
        research_id = argv[4]
        extra = json.loads(con.execute(
            "select extra from reports where id=?", (research_id,)).fetchone()[0] or "{}")
        claims = extra.get("claims") or []
        # ⚠️ 不加 `citation_no is not null`：线索**不要求进引用池**，用户点名的那五条
        # 信号 `citation_no` 全是 None。
        rows_db = [{"id": r[0], "citation_no": r[1], "platform": r[2], "agent_name": r[3]}
                   for r in con.execute(
                       "select id, citation_no, platform, agent_name from evidence "
                       "where report_id=?", (research_id,))]
        # 对照实体的判定要按**证据行**算（线索不要求进池，按角标那份覆盖不到没进池的），
        # 判法与 `sources[].offtopic` 同一个 `is_contrast_source`。
        # ⚠️ §SEL-4：这段是 `tables.build_tables` 里那几行的**镜像**，它决定「传谁进 `_clues`」。
        # 生产侧 09-18 起只筛对照实体（海外放行），这边**必须同步**——只改生产侧不改这里，
        # 这把尺子量出来的还是旧筛法的条数，会把「海外进来了」量成「没进来」。
        from app.report.polish.tables import is_contrast_source

        subject_names = set(data.get("subjects") or [])
        entity_of = {c["agent_id"]: c.get("entity") for c in data.get("chapters") or []}
        # ⚠️ 题面不再参与这里的判定：只有「是不是海外」才看题面地域，而海外已放行。
        contrast_ids = set()
        for row in rows_db:
            entity = entity_of.get(str(row.get("agent_name") or ""), "")
            if is_contrast_source(
                    contrast=None if not entity else entity not in subject_names):
                contrast_ids.add(str(row["id"]))
        clues = _clues(claims, rows_db, contrast_ids)
        body_marks = {int(n) for n in __import__("re").findall(
            r"\[S(\d+)\]", md.split("## 信息源清单")[0])}
        print(f"{BEFORE}：`tables.json` 里连 clues 这个键都没有"
              f"（实测 {'有' if 'clues' in data else '没有'}），正文一条线索都没出现。")
        print(f"{AFTER}：库里 {len(claims)} 条主张，按「单源 + 亲历 + 不是对照实体 + 不是自标噪声」"
              f"筛出 {len(clues)} 条候选（⛔ 不要求进引用池：用户点名的那五条信号"
              f"citation_no 全是 None），呈现层剔除正文引过的再封顶：")
        print(clues_block(clues, body_marks) or "  （正文都引过了，整块不出）")
    else:
        print("（没给库，跳过——这一货的读数在库的 claims 里）")

    print()
    print("=" * 78)
    print("货 5 建议与证据强度")
    print("=" * 78)
    advice = _section(md, "# 对不同读者的含义", "# 附录").splitlines() \
        or _section(md, "# 建议", "# 附录").splitlines()
    crossref = {int(s["mark"][1:]): s.get("crossref") for s in sources if s.get("crossref")}
    old = singlesource_advice(advice, crossref)
    print(f"{BEFORE}（只有单源孤证那道闸）：{len(old)} 处")
    new = weakevidence_advice(advice, sources, tables)
    print(f"{AFTER}（新增证据强度那道闸）：{len(new)} 处")
    for problem in new:
        print(f"  · {problem.splitlines()[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
