"""§ALLOC-2 判据 2：真跑一次**规划期**，每个 goal 的采集卡实体 ⊆ 该 goal 标题/objective 点名的实体。

    ../Owli/.venv/bin/python scripts/acceptance/alloc2/alloc2_planning_run.py var/alloc2-run

抄 `scripts/acceptance/d061/d061_planning_run.py` 形态（一次性库、直调 generate_plan、事件真落库），
只换题面与末尾判据。题面沿用重采题面「国内大家对豆包的看法」——判据要的是骨架按实体分段时
卡归不归位，换题面等于换尺子。

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
SCALE = "fast"
HARD_CAP_SECONDS = 20 * 60

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "var/alloc2-run").resolve()
OUT.mkdir(parents=True, exist_ok=True)
# 第二个参数可指定 research_id——起跑前要把 id 报给调度进哨兵 IGNORE，得先定下来。
RID = sys.argv[2] if len(sys.argv) > 2 else f"r-alloc2-{datetime.now().strftime('%m%d-%H%M')}"

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


from app.plan.allocation import goal_affinity  # noqa: E402


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

    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = [json.loads(r[0]) for r in conn.execute(
        "select payload from events where research_id=? order by sequence", (RID,))]
    conn.close()
    baseline_rows = [r for r in rows if isinstance(r.get("raw"), dict)
                     and "protagonist_baseline_slots" in r["raw"]]
    baseline = {(b["goal_id"], b["source_id"], b["entity"])
                for r in baseline_rows for b in r["raw"]["protagonist_baseline_slots"]}

    def named(goal_id: str) -> set[str]:
        return {e for e, score in affinity.get(goal_id, {}).items() if score > 0}

    def titled(goal_id: str) -> set[str]:
        return {e for e, score in affinity.get(goal_id, {}).items() if score == 2}

    per_goal = {g: {e for gg, _, e in cards if gg == g} for g in {g for g, _, _ in cards}}
    violations = {g: ents - named(g) for g, ents in per_goal.items() if ents - named(g)}
    checks = [
        ("〇 骨架按实体分段（至少一个 goal 标题点名实体；否则本判据无靶子）",
         bool(affinity) and any(titled(g) for g in affinity),
         f"affinity={affinity}"),
        ("① 每 goal 采集卡实体 ⊆ 该 goal 标题/objective 点名实体",
         not violations, f"越界={violations}" if violations else ""),
        ("② 每个「标题点名单一实体」的 goal 至少一张该实体卡",
         all(titled(g) <= per_goal.get(g, set()) for g in affinity if len(titled(g)) == 1),
         f"{ {g: sorted(per_goal.get(g, set())) for g in affinity} }"),
        ("③ 六对 (source, entity) == 分配表；每 goal ≤2 位",
         {(s, e) for _, s, e in cards} == {(str(s["source_id"]), str(s["entity"]).strip())
                                           for v in allocation.values() for s in v}
         and all(sum(1 for g, _, _ in cards if g == goal) <= 2 for goal in per_goal), ""),
        ("④ 溢出主角卡的基线账落在 events 表里（allocation.json 里对应 goal 也真有那张卡）",
         all(any(g == bg and s == bs and e == be for g, s, e in cards) for bg, bs, be in baseline),
         f"{sorted(baseline)}"),
    ]
    for label, ok, detail in checks:
        print(f"{'✓' if ok else '×'} {label}" + (f"    {detail}" if detail else ""))
    print(f"\n耗时 {time.time() - T0:.1f}s｜卡：{cards}")
    print(f"分配表：{allocation}")
    for r in rows:
        text = str((r.get("data") or {}).get("text", ""))
        if text.startswith(("采集分配表", "主角对照基线卡", "机械修正", "跨语域采集位")):
            print("  events:", text[:240])
    return 0 if all(ok for _, ok, _ in checks) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
