"""§WX-1 判据⑦ 独立尺子：自带采集卡被删光的 goal，最终验收条里还在要「本 goal 采集产物」的条。

    ../Owli/.venv/bin/python scripts/acceptance/wx1/wx1_stale_acceptance.py var/wx1-run-0914b

⛔ **刻意不 import `app.plan.normalize`**（坑 23′）：上一版 ⑦ 调了被测的 `_asks_backfill`，它只认
「补采」一词，尺子跟着同盲、读 0 = 假绿（r-wx1-0914-1425 goal-5 仍留「本 goal 新增 2 条采集
output.path」）。本尺子只读两样落盘物——`plan.json`（最终计划）与 `owli.db` events 表里的
删卡留痕原文——判定全部自己写，按**结构**匹配：

- A 被删卡：验收条点名被删卡的 `源·实体` 组合、被删章 id、或 `goals/<goal>/<被删章>.json` 路径；
- B 采集主语：验收条出现「新增采集 / 采集章 / 采集 JSON / 采集数据 / 本 goal 采集」一类主语。
范围：删卡留痕里出现过、且最终计划里本 goal 已无任何采集章（capability.sources 非空）的 goal。
输出每条命中与命中规则；不判「该不该摘」，只报「还在」，由人核。
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

# 用法一（一次性小跑库）：wx1_stale_acceptance.py <out_dir>          读 <out_dir>/plan.json + <out_dir>/owli.db 全部事件
# 用法二（留服库，多研究同库）：wx1_stale_acceptance.py <plan.json> <db> <research_id>  事件按 research_id 过滤
if len(sys.argv) >= 4:
    PLAN_PATH, DB_PATH, RESEARCH_ID = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve(), sys.argv[3]
else:
    OUT = Path(sys.argv[1]).resolve()
    PLAN_PATH, DB_PATH, RESEARCH_ID = OUT / "plan.json", OUT / "owli.db", None
PLAN = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
if "goals" not in PLAN and isinstance(PLAN.get("data"), dict):
    PLAN = PLAN["data"].get("plan") or PLAN["data"]

# 删卡留痕原文形状：「[修正31] goal-5/data-collection-19 采集卡「reddit·字节跳动」不在分配表里，已删除」
DELETED = re.compile(r"\[修正31\] (goal-\d+)/(\S+?) 采集卡「([^「」·]+)·([^「」]+)」不在分配表里")
SUBJECT = re.compile(r"新增\s*\d*\s*(条|份|张)?\s*采集|采集章|采集\s*JSON|采集数据|本\s*goal\s*(新增)?\s*采集|采集\s*output")


def deleted_cards() -> dict[str, list[tuple[str, str, str]]]:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    if RESEARCH_ID:
        cursor = conn.execute(
            "select payload from events where research_id=? order by sequence", (RESEARCH_ID,))
    else:
        cursor = conn.execute("select payload from events order by sequence")
    texts = [str((json.loads(r[0]).get("data") or {}).get("text", "")) for r in cursor]
    conn.close()
    found: dict[str, set[tuple[str, str, str]]] = {}
    for text in texts:
        for goal_id, agent_id, source, entity in DELETED.findall(text):
            found.setdefault(goal_id, set()).add((agent_id, source, entity))
    return {goal_id: sorted(cards) for goal_id, cards in found.items()}


def main() -> int:
    deleted = deleted_cards()
    hits = []
    for goal in PLAN["goals"]:
        goal_id = goal["goal_id"]
        if goal_id not in deleted:
            continue
        if any((agent.get("capability") or {}).get("sources") for agent in goal["agents"]):
            continue  # 本 goal 还有自带采集章，「本 goal 采集」类验收条仍可达，不在本尺子范围
        cards = deleted[goal_id]
        for index, item in enumerate(goal.get("acceptance") or []):
            text = str(item)
            rules = []
            for agent_id, source, entity in cards:
                if f"{source}·{entity}" in text or agent_id in text or f"goals/{goal_id}/{agent_id}" in text:
                    rules.append(f"A:{source}·{entity}")
            if SUBJECT.search(text):
                rules.append("B:采集主语")
            if rules:
                hits.append({"goal": goal_id, "index": index, "rules": sorted(set(rules)), "text": text[:120]})
    print(json.dumps({"deleted_cards": deleted, "stale_count": len(hits), "hits": hits},
                     ensure_ascii=False, indent=2))
    return 0 if not hits else 1


if __name__ == "__main__":
    raise SystemExit(main())
