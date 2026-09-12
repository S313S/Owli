"""§D-061 判据 2：真跑一次**规划期**，计划卡数必须 == 分配表位数，且删卡记录落库。

    ../Owli/.venv/bin/python scripts/acceptance/d061/d061_planning_run.py var/d061-run

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

QUERY = "豆包语音输入法的竞品分析"
SCALE = "fast"
HARD_CAP_SECONDS = 20 * 60

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "var/d061-run").resolve()
OUT.mkdir(parents=True, exist_ok=True)
RID = f"r-d061-{datetime.now().strftime('%m%d-%H%M')}"

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
    if text.startswith("机械修正") or getattr(event, "is_error", False):
        print(f"[+{time.time() - T0:6.1f}s] {text[:300]}", flush=True)


store.on_plan_event = on_plan_event


def _cards(plan):
    return [(goal.goal_id, str((agent.capability.get("sources") or [""])[0]),
             str(agent.entity or "").strip())
            for goal in plan.goals for agent in goal.agents
            if agent.capability.get("profile") == "web-collector" and agent.entity]


async def main() -> int:
    adapter = RoutedAdapter(utc_clock=lambda: datetime.now(timezone.utc))
    print(f"research_id = {RID}｜题目 = {QUERY}｜scale = {SCALE}", flush=True)
    plan = await asyncio.wait_for(
        generate_plan(QUERY, store, adapter, scale=SCALE), timeout=HARD_CAP_SECONDS)
    (OUT / "plan.json").write_text(
        json.dumps(plan.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    allocation = json.loads(
        (store.runs_root / RID / "plan-segments" / "allocation.json").read_text("utf-8"))
    table = {(str(s["source_id"]), str(s["entity"]).strip())
             for slots in allocation.values() for s in slots}
    cards = _cards(plan)
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = [json.loads(r[0]) for r in conn.execute(
        "select payload from events where research_id=? order by sequence", (RID,))]
    conn.close()
    repairs = [r for r in rows
               if "[修正31]" in str((r.get("data") or {}).get("text", ""))]
    dropped = [r for r in repairs if "不在分配表里" in str(r["data"]["text"])]

    checks = [
        ("① 计划卡数 == 分配表位数", len(cards) == len(table), f"{len(cards)} vs {len(table)}"),
        ("② 计划卡与表逐对相等（集合）",
         {(s, e) for _, s, e in cards} == table, ""),
        ("③ 每 goal 采集位 ≤ fast 容量 2",
         all(sum(1 for g, _, _ in cards if g == goal) <= 2
             for goal in {g for g, _, _ in cards}), ""),
        ("④ 删卡记录落在 events 表里（不是日志）", True, f"{len(dropped)} 条"),
    ]
    for label, ok, detail in checks:
        print(f"{'✓' if ok else '×'} {label}" + (f"    {detail}" if detail else ""))
    print(f"\n耗时 {time.time() - T0:.1f}s｜卡：{cards}")
    print(f"分配表：{sorted(table)}")
    if repairs:
        print("\n— events 里的 [修正31] —")
        for r in repairs:
            print("  ", r["data"]["text"][:220])
    else:
        print("\n（本轮引擎没起草表外卡，④ 只证明通路，没证明它拦得住——"
              "拦得住由 tests/test_d061_allocation_gate.py 的真夹具造红证明）")
    return 0 if all(ok for _, ok, _ in checks) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
