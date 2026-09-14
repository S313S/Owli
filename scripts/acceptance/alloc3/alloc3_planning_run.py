"""§ALLOC-3 判据 3：standard 档真跑一次**规划期**——主角卡按 goal 性质分卡，D-061/D-062 两闸不退化。

    ../Owli/.venv/bin/python scripts/acceptance/alloc3/alloc3_planning_run.py var/alloc3-run r-alloc3-0914-xxxx

抄 `scripts/acceptance/wx1/wx1_planning_run.py`（WX-1 分支上，一次性库、直调 generate_plan、事件真落库），
只换末尾判据。题面沿用「国内大家对豆包的看法」。
读数：每 goal 卡数、有无 0 章 goal、每张卡的 goal 与分配表相等、D-062 闸再过摘 0 条、
主角国内四源卡落在性质分最高的候选 goal（骨架是引擎抽的，本轮未必有口碑/媒体 goal——如实报）。
两闸「不退化」不另写判定：D-061 看最终计划采集卡与分配表逐对相等；D-062 把最终计划再过一遍
生产 `normalize._repair_acceptance`（深拷贝），一条都不该再摘——摘得出来就是闸没收干净。

⛔ 只跑规划期：直调 `generate_plan`，不起服务、不进执行期、不经 Scheduler，
所以不需要 stopper 也不会自动起跑（`OWLI_AUTO_CONFIRM` 一律别设——设了会让
别处的计划一出就自动开跑）。硬上限 20 分钟。

与 `scripts/fixtures/planning_fixture.py` 的差别只有一处，但是要命的一处：
那份夹具把 `store.on_plan_event` 换成了「只打印」，事件根本不进库；本包的判据是
**events 里有删卡记录**，所以这里照生产的走法（`app/api/main.py:202` 那条路）
把每条计划事件真写进 events 表。判据落库不落日志。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, ".")

QUERY = "国内大家对豆包的看法"
SCALE = "standard"
HARD_CAP_SECONDS = 30 * 60

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "var/alloc3-run").resolve()
OUT.mkdir(parents=True, exist_ok=True)
# 第二个参数可指定 research_id——起跑前要把 id 报给调度进哨兵 IGNORE，得先定下来。
RID = sys.argv[2] if len(sys.argv) > 2 else f"r-alloc3-{datetime.now().strftime('%m%d-%H%M')}"

DB = OUT / "owli.db"
if DB.exists():
    DB.unlink()
with sqlite3.connect(DB) as connection:
    connection.executescript(Path("app/store/schema.sql").read_text(encoding="utf-8"))

from app.adapters.routing import RoutedAdapter  # noqa: E402
from app.plan.generate import generate_plan  # noqa: E402
from app.store.dao import Store  # noqa: E402

store = Store(DB)
created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
store.create_report(id=RID, title=QUERY, research_question=QUERY,
                    created_at=created_at, extra={"plan_generated_at": created_at})
store.runs_root = OUT / "runs"

T0 = time.time()


async def on_plan_event(event) -> None:
    """照生产走法落库：`app/api/main.py` 的 publish_plan_event 就是这个形状。"""
    text = str(getattr(event, "text", ""))
    raw = getattr(event, "raw", None)
    store.append_event(
        RID,
        event_type="normalized_event",
        payload={"type": "normalized_event",
                 "raw": raw if isinstance(raw, dict) else None,
                 "data": {"goal_id": "planning",
                          "agent_id": str(getattr(event, "turn_id", "") or "plan"),
                          "text": text,
                          "is_error": bool(getattr(event, "is_error", False))}},
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    if text.startswith(("机械修正", "采集分配表", "主角对照基线卡", "跨语域采集位")) or getattr(event, "is_error", False):
        print(f"[+{time.time() - T0:6.1f}s] {text[:300]}", flush=True)


store.on_plan_event = on_plan_event


def _cards(plan):
    return [(goal.goal_id, str((agent.capability.get("sources") or [""])[0]),
             str(agent.entity or "").strip())
            for goal in plan.goals for agent in goal.agents
            if agent.capability.get("profile") == "web-collector" and agent.entity]


import copy  # noqa: E402

from app.plan.allocation import (  # noqa: E402
    SOURCE_NATURE, SUMMARY_GOAL_CUES, goal_affinity, nature_score,
)
from app.plan.normalize import _repair_acceptance  # noqa: E402

LEAD = "豆包"


async def main() -> int:
    adapter = RoutedAdapter(utc_clock=lambda: datetime.now(timezone.utc))
    print(f"research_id = {RID}｜题目 = {QUERY}｜scale = {SCALE}", flush=True)
    plan = await asyncio.wait_for(
        generate_plan(QUERY, store, adapter, scale=SCALE), timeout=HARD_CAP_SECONDS)
    (OUT / "plan.json").write_text(
        json.dumps(plan.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    segments = store.runs_root / RID / "plan-segments"
    allocation = json.loads((segments / "allocation.json").read_text("utf-8"))
    skeleton = json.loads((segments / "skeleton.json").read_text("utf-8"))
    subjects = [str(s).strip() for s in skeleton["subjects"]]
    entities = [dict(card) for card in plan.to_dict().get("entities") or []]
    for card in entities:
        card.setdefault("id", card.get("canonical"))
    scaffolds = [{"title": g["title"], "objective": g["objective"]} for g in skeleton["goals"]]
    affinity = goal_affinity(scaffolds, entities, subjects)
    cards = _cards(plan)
    triples_plan = sorted(cards)
    triples_alloc = sorted((g, str(s["source_id"]), str(s["entity"]).strip())
                           for g, v in allocation.items() for s in v)
    residual = _repair_acceptance(copy.deepcopy(plan))
    empty_goals = [goal.goal_id for goal in plan.goals if not goal.agents]

    # 性质投递读数：候选 = objective 以上点名主角、标题不点名别的实体、非汇总标题
    candidates = [
        f"goal-{i}" for i, sc in enumerate(scaffolds, start=1)
        if affinity.get(f"goal-{i}", {}).get(LEAD, 0) >= 1
        and not any(affinity[f"goal-{i}"].get(o, 0) == 2 for o in subjects if o != LEAD)
        and not any(cue in sc["title"] for cue in SUMMARY_GOAL_CUES)
    ]
    misplaced = []
    for goal_id, source, entity in triples_alloc:
        if entity != LEAD or source not in SOURCE_NATURE or not candidates:
            continue
        top = max(nature_score(scaffolds[int(g.split("-")[1]) - 1], source) for g in candidates)
        here = nature_score(scaffolds[int(goal_id.split("-")[1]) - 1], source)
        if here < top:
            misplaced.append((goal_id, source, here, top))

    checks = [
        ("〇 主角是豆包且至少一个 goal 标题点名豆包",
         any(v.get(LEAD, 0) == 2 for v in affinity.values()), f"affinity={affinity}"),
        ("① 主角国内四源卡都落在性质分最高的候选 goal", not misplaced, f"未归位={misplaced}"),
        ("② D-061 不退化：每张采集卡的 goal 与分配表相等（表外 0、缺 0）",
         triples_plan == triples_alloc,
         f"表外={sorted(set(triples_plan) - set(triples_alloc))} 缺={sorted(set(triples_alloc) - set(triples_plan))}"),
        ("③ D-062 不退化：最终计划再过验收条闸摘出 0 条", not residual, f"{residual[:3]}"),
    ]
    for label, ok, detail in checks:
        print(f"{'✓' if ok else '×'} {label}" + (f"    {detail}" if detail else ""))
    chapters = sum(len(goal.agents) for goal in plan.goals)
    print(f"\n耗时 {time.time() - T0:.1f}s｜goal {len(plan.goals)}｜章(agent) {chapters}｜采集卡 {len(cards)}"
          f"｜0 章 goal {empty_goals or '无'}｜候选 {candidates}")
    for index, goal in enumerate(plan.goals, start=1):
        scores = {s: nature_score(scaffolds[index - 1], s) for s in SOURCE_NATURE}
        print(f"  {goal.goal_id} 「{goal.title}」 depends={goal.depends_on} 章 {len(goal.agents)}"
              f" 性质分 {scores} 卡 {[(s, e) for g, s, e in cards if g == goal.goal_id]}")
    return 0 if all(ok for _, ok, _ in checks) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
