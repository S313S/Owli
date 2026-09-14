"""§WX-1 判据 2：standard 档真跑一次**规划期**——口碑 goal 有国内社媒豆包卡、媒体 goal 有公众号·豆包，D-061/D-062 两闸不退化，D-067 综合 goal 不被掏空。

rebase 到 712a004（含 ALLOC-3 性质投递、D-065 空 goal 移出）后复验：「哪个 goal 是口碑类/媒体类」
不另写分类，调生产 `allocation.nature_score` 与 ALLOC-3 同一把尺子；0 章 goal 与 D-065 移出留痕
用生产 `normalize.empty_goal_removal` 从事件里认。

    ../Owli/.venv/bin/python scripts/acceptance/wx1/wx1_planning_run.py var/wx1-run r-wx1-0913-xxxx

抄 `scripts/acceptance/alloc2/alloc2_planning_run.py`（一次性库、直调 generate_plan、事件真落库），
只换档位（standard）、硬上限（30 min，standard 骨架最多 7 goal）与末尾判据。
题面沿用重采题面「国内大家对豆包的看法」。
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

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "var/wx1-run").resolve()
OUT.mkdir(parents=True, exist_ok=True)
# 第二个参数可指定 research_id——起跑前要把 id 报给调度进哨兵 IGNORE，得先定下来。
RID = sys.argv[2] if len(sys.argv) > 2 else f"r-wx1-{datetime.now().strftime('%m%d-%H%M')}"

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
from app.plan.normalize import _asks_backfill, _repair_acceptance, empty_goal_removal  # noqa: E402

LEAD = "豆包"
USER_OPINION = ("xhs", "douyin", "weibo")
INDUSTRY_VIEW = ("wechat_mp",)


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
    by_goal = {f"goal-{i}": sc for i, sc in enumerate(scaffolds, start=1)}
    affinity = goal_affinity(scaffolds, entities, subjects)
    cards = _cards(plan)
    triples_plan = sorted(cards)
    triples_alloc = sorted((g, str(s["source_id"]), str(s["entity"]).strip())
                           for g, v in allocation.items() for s in v)
    residual = _repair_acceptance(copy.deepcopy(plan))
    empty_goals = [goal.goal_id for goal in plan.goals if not goal.agents]

    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = [json.loads(r[0]) for r in conn.execute(
        "select payload from events where research_id=? order by sequence", (RID,))]
    conn.close()
    texts = [str((r.get("data") or {}).get("text", "")) for r in rows]
    fixes = [t for t in texts if t.startswith("机械修正") or "[修正" in t]
    removed = [hit for t in fixes
               if (hit := empty_goal_removal(t.removeprefix("机械修正：").strip())) is not None]

    # 候选 = objective 以上点名主角、标题不点名别的实体、非汇总标题（与 `_route_by_nature` 同口径）
    candidates = [
        g for g, sc in by_goal.items()
        if affinity.get(g, {}).get(LEAD, 0) >= 1
        and not any(affinity[g].get(o, 0) == 2 for o in subjects if o != LEAD)
        and not any(cue in sc["title"] for cue in SUMMARY_GOAL_CUES)
    ]

    def top_goals(source: str) -> list[str]:
        scores = {g: nature_score(by_goal[g], source) for g in candidates}
        best = max(scores.values(), default=0)
        return [g for g, v in scores.items() if v == best and v > 0]

    def holders(source: str) -> list[str]:
        return [g for g, s, e in cards if s == source and e == LEAD]

    opinion_goals = sorted({g for s in USER_OPINION for g in top_goals(s)})
    media_goals = sorted({g for s in INDUSTRY_VIEW for g in top_goals(s)})
    opinion_ok = bool(opinion_goals) and all(
        set(holders(s)) & set(top_goals(s)) for s in USER_OPINION)
    media_ok = bool(media_goals) and all(
        set(holders(s)) & set(top_goals(s)) for s in INDUSTRY_VIEW)
    removed_ids = {hit["goal_id"] for hit in removed}

    # §D-067 复验：综合 goal = 骨架标题含生产 SUMMARY_GOAL_CUES 的 goal（与 ALLOC-3 同一词表）。
    synthesis_skel = [g for g, sc in by_goal.items()
                      if any(cue in sc["title"] for cue in SUMMARY_GOAL_CUES)]
    plan_goals = {goal.goal_id: goal for goal in plan.goals}
    synthesis_detail: dict[str, str] = {}
    synthesis_ok = True
    for goal_id in synthesis_skel:
        goal = plan_goals.get(goal_id)
        if goal is None:
            synthesis_ok = False
            synthesis_detail[goal_id] = "已移出"
            continue
        deliverable = str((goal.deliverable or {}).get("path", ""))
        writers = [a for a in goal.agents if str((a.output or {}).get("path", "")) == deliverable]
        heads = [a for a in goal.agents if not a.depends_on]
        dry_heads = [a.agent_id for a in heads if not a.inputs]
        upstream_inputs = sum(
            1 for a in goal.agents for item in (a.inputs or [])
            if isinstance(item, dict) and item.get("from_goal") in set(goal.depends_on))
        ok = bool(goal.agents) and bool(writers) and not dry_heads
        synthesis_ok = synthesis_ok and ok
        synthesis_detail[goal_id] = (
            f"章 {len(goal.agents)} 撰写章 {[a.agent_id for a in writers]} 链头 {[a.agent_id for a in heads]}"
            f" 无输入链头 {dry_heads} 上游 goal 输入 {upstream_inputs} depends={goal.depends_on}")
    stale_backfill = [
        (goal.goal_id, index, str(item)[:80])
        for goal in plan.goals
        if not any(g == goal.goal_id for g, _, _ in cards)
        for index, item in enumerate(goal.acceptance)
        if _asks_backfill(str(item))
    ]

    checks = [
        ("〇 主角是豆包且至少一个 goal 标题点名豆包",
         any(v.get(LEAD, 0) == 2 for v in affinity.values()), f"affinity={affinity}"),
        ("① 口碑类 goal（性质分最高）有 小红书/抖音/微博·豆包",
         opinion_ok, f"口碑类={opinion_goals} 实落={ {s: holders(s) for s in USER_OPINION} }"),
        ("② 媒体类 goal（性质分最高）有 公众号·豆包",
         media_ok, f"媒体类={media_goals} 实落={ {s: holders(s) for s in INDUSTRY_VIEW} }"),
        ("③ 计划里无 0 章 goal（被 D-065 移出的另列）", not empty_goals,
         f"0 章={empty_goals} 已移出={sorted(removed_ids)}"),
        ("④ D-061 不退化：每张采集卡的 goal 与分配表相等（表外 0、缺 0；已移出 goal 在表里本就无卡）",
         triples_plan == triples_alloc,
         f"表外={sorted(set(triples_plan) - set(triples_alloc))} 缺={sorted(set(triples_alloc) - set(triples_plan))}"),
        ("⑤ D-062 不退化：最终计划再过验收条闸摘出 0 条", not residual, f"{residual[:3]}"),
        ("⑥ D-067：综合 goal 保留、撰写章在、链头都有输入（骨架无综合 goal 则本条无靶子记红）",
         bool(synthesis_skel) and synthesis_ok, f"综合={synthesis_skel} {synthesis_detail}"),
        ("⑦ D-067：无自带卡的 goal 上没有「补采产物」验收条（生产 _asks_backfill）",
         not stale_backfill, f"{stale_backfill[:3]}"),
    ]
    for label, ok, detail in checks:
        print(f"{'✓' if ok else '×'} {label}" + (f"    {detail}" if detail else ""))
    chapters = sum(len(goal.agents) for goal in plan.goals)
    kinds: dict[str, int] = {}
    for goal in plan.goals:
        for agent in goal.agents:
            kind = str(agent.capability.get("profile") or "")
            kinds[kind] = kinds.get(kind, 0) + 1
    print(f"\n耗时 {time.time() - T0:.1f}s｜goal {len(plan.goals)}｜章(agent) {chapters} {kinds}"
          f"｜采集卡 {len(cards)}｜移出 {removed or '无'}｜候选 {candidates}")
    for goal in plan.goals:
        sc = by_goal.get(goal.goal_id, {"title": goal.title, "objective": ""})
        scores = {s: nature_score(sc, s) for s in SOURCE_NATURE}
        print(f"  {goal.goal_id} 「{goal.title}」 depends={goal.depends_on} 章 {len(goal.agents)}"
              f" 性质分 {scores} 卡 {[(s, e) for g, s, e in cards if g == goal.goal_id]}")
    for text in fixes:
        print("  修正:", text[:240])
    return 0 if all(ok for _, ok, _ in checks) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
