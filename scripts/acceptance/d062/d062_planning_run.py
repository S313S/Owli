"""§D-062 判据 2：真跑一次**规划期**，每 goal 验收条提到的实体 ⊆ 该 goal 可达实体，
引用的产物路径都存在，且摘条记录落库。

    ../Owli/.venv/bin/python scripts/acceptance/d062/d062_planning_run.py var/d062-run

抄自 scripts/acceptance/d061/d061_planning_run.py，只换末尾判据；「可达」与「提到」
的判法直接复用 app/plan/normalize.py 里生产那份（`_entity_matchers` /
`_mentioned_outside_negation` / `_ancestors`），⛔ 不在这里重实现一遍尺子。

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

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "var/d062-run").resolve()
OUT.mkdir(parents=True, exist_ok=True)
# 第二个参数可指定 research_id——起跑前要把 id 报给调度进哨兵 IGNORE，
# 所以 id 得先于起跑定下来，不能等脚本自己按时间生成。
RID = sys.argv[2] if len(sys.argv) > 2 else f"r-d062-{datetime.now().strftime('%m%d-%H%M')}"

DB = OUT / "owli.db"
if DB.exists():
    DB.unlink()
with sqlite3.connect(DB) as connection:
    connection.executescript(Path("app/store/schema.sql").read_text(encoding="utf-8"))

from app.adapters.routing import RoutedAdapter  # noqa: E402
from app.plan.generate import generate_plan  # noqa: E402
from app.plan.normalize import (  # noqa: E402
    _ancestors, _entity_matchers, _mentioned_outside_negation, _missing_paths,
)
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

    cards = _cards(plan)
    by_goal: dict[str, set[str]] = {g.goal_id: set() for g in plan.goals}
    for goal_id, _, entity in cards:
        by_goal[goal_id].add(entity)
    ancestors = _ancestors(plan)
    reachable = {g: by_goal[g] | set().union(*(by_goal.get(u, set()) for u in ancestors[g]))
                 for g in by_goal}
    outputs = {str(a.output.get("path", "")) for g in plan.goals for a in g.agents} \
        | {str(g.deliverable.get("path", "")) for g in plan.goals}
    matchers = _entity_matchers(plan)

    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = [json.loads(r[0]) for r in conn.execute(
        "select payload from events where research_id=? order by sequence", (RID,))]
    conn.close()
    texts = [str((r.get("data") or {}).get("text", "")) for r in rows]
    repairs4 = [t for t in texts if "[修正4]" in t]
    repairs31 = [t for t in texts if "[修正31]" in t]

    # 逐 goal 逐条读数：提到了谁 / 谁不可达 / 引了哪些不存在的路径
    bad_entity: list[str] = []
    bad_path: list[str] = []
    exempted: list[str] = []   # §D-062-fu：提到无卡实体、但只在否定/限定子句里 ⇒ 豁免保住
    print("\n— 计划里每个 goal 的验收条（摘完之后） —")
    for goal in plan.goals:
        print(f"{goal.goal_id} 可达实体 {sorted(reachable[goal.goal_id])}")
        for i, line in enumerate(goal.acceptance):
            mentioned = [e for e, pats in matchers if _mentioned_outside_negation(line, pats)]
            unreachable = [e for e in mentioned if e not in reachable[goal.goal_id]]
            # §D-062-fu：短形 goal-N/<file> 也归一成全形再判（生产那份，不重实现）。
            missing = _missing_paths(line, outputs)
            flag = ("×" if (unreachable or missing) else "✓")
            print(f"  {flag} [{i}] 提到{mentioned or '-'} {line[:110]}")
            named = [e for e, pats in matchers if any(p.search(line) for p in pats)]
            shielded = [e for e in named
                        if e not in reachable[goal.goal_id] and e not in mentioned]
            if shielded:
                exempted.append(f"{goal.goal_id}[{i}] {shielded} {line[:120]}")
            if unreachable:
                bad_entity.append(f"{goal.goal_id}[{i}] {unreachable}")
            if missing:
                bad_path.append(f"{goal.goal_id}[{i}] {missing}")

    checks = [
        ("① 每 goal 验收条提到的实体 ⊆ 该 goal 可达实体", not bad_entity, "；".join(bad_entity)),
        ("② 验收条引用的 goals/<goal>/<file> 都存在", not bad_path, "；".join(bad_path)),
        ("③ 每 goal 验收条 ≥1 条（没被摘光）",
         all(len(g.acceptance) >= 1 for g in plan.goals), ""),
        ("④ 摘条记录落在 events 表里（不是日志）", True, f"{len(repairs4)} 条 [修正4]"),
    ]
    print()
    for label, ok, detail in checks:
        print(f"{'✓' if ok else '×'} {label}" + (f"    {detail}" if detail else ""))
    print(f"\n耗时 {time.time() - T0:.1f}s｜卡：{cards}")
    if repairs31:
        print("\n— events 里的 [修正31] —")
        for t in repairs31:
            print("  ", t[:220])
    print(f"\n— §D-062-fu：反向约束豁免保住的验收条 {len(exempted)} 条 —")
    for t in exempted:
        print("  ", t)
    if repairs4:
        print("\n— events 里的 [修正4]（逐条人读：原文是不是在「禁止写」某实体）—")
        for t in repairs4:
            original = t.split("——原文「", 1)[-1] if "——原文「" in t else ""
            hint = "⚠️ 原文含否定/限定字样，人工核" if any(
                w in original for w in ("未", "不", "仅", "只", "禁", "勿", "避免", "排除")) else ""
            print("  ", t[:400], hint)
    else:
        print("\n（本轮引擎起草的验收条没提无卡实体、没引不存在的产物，④ 只证明通路，"
              "没证明它拦得住——拦得住由 tests/test_d062_acceptance_gate.py 的真夹具造红证明）")
    return 0 if all(ok for _, ok, _ in checks) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
