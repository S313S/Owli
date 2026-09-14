"""§D-065：删卡闸把一个 goal 删成 0 章 → 执行期静默冻结、报告永远不出。

夹具是真的：WX-1 规划期小跑 `r-wx1-0913-1355`（standard）的 plan.json 与 plan-segments
原样拷进 tests/fixtures/d065。goal-4「社媒与用户口碑」唯一一张 web_search·豆包 是分配表外卡，
被 D-061 删卡闸删掉并连带删光该 goal 全部章；goal-6 依赖 goal-4。

两层各锁一道：
- 规划层（A）：`normalize_plan` 把空 goal 移出计划、下游改接上游、摘掉指向它的输入，留痕；
- 执行层（B）：计划里仍有 0 章 goal 时（计划编辑接口、旧计划），scheduler 上游就绪即判 done、
  不发核对卡、发 `goal_gate` reason=empty_goal，研究能走到终态。

量在 scheduler 状态对象上（`status` / `goal_statuses` / `emitted_events`），不在日志上。
"""

from __future__ import annotations

import asyncio
import copy
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from plan_factory import make_agent, make_goal, make_plan_dict  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "d065" / "plan.json"


def _wx1_plan():
    from app.plan.model import Plan

    return Plan.from_dict(json.loads(FIXTURE.read_text(encoding="utf-8")))


def _minimal_plan(*, empty_upstream_policy: str = "skip"):
    """3 goal：goal-1 一章 → goal-2 被删光（0 章）→ goal-3 吃 goal-2 与 goal-1 的产物。"""
    from app.plan.model import Plan

    source = make_plan_dict()
    first = make_goal(1)
    hollow = make_goal(2)
    hollow["agents"] = []
    hollow["on_upstream_failure"] = empty_upstream_policy
    last = make_goal(3)
    last["depends_on"] = ["goal-2"]
    reader = make_agent("agent-3", "goal-3")
    reader["inputs"] = [
        {"from_goal": "goal-2", "artifact": "goals/goal-2/result.md"},
        {"from_goal": "goal-1", "artifact": "goals/goal-1/agent-1.md"},
    ]
    reader["chapter"] = copy.deepcopy(reader["chapter"])
    reader["chapter"].setdefault("opening", {})["inputs"] = [
        {"path": "goals/goal-2/result.md"},
        {"path": "goals/goal-1/agent-1.md"},
    ]
    last["agents"] = [reader]
    source["goals"] = [first, hollow, last]
    source["baseline"] = None
    return Plan.from_dict(source)


async def _drive_to_idle(plan, *, fail_goals: frozenset[str] = frozenset()):
    """假 run_task 全成功（fail_goals 里的除外）、核对卡一律「继续」，驱动到无可调度。"""
    from app.orchestrator.scheduler import Scheduler, TaskRunResult

    events: list[dict[str, Any]] = []
    started: list[str] = []

    async def run_task(agent, context):
        started.append(context.goal_id)
        return TaskRunResult(
            succeeded=context.goal_id not in fail_goals, engine=agent.engine,
        )

    def timer(_delay: float, _callback: Any) -> None:
        return None

    from datetime import datetime, timezone

    scheduler = Scheduler(
        plan, run_task, events.append,
        lambda: datetime(2026, 9, 14, tzinfo=timezone.utc), timer,
    )
    for goal in plan.goals:
        goal.retry_policy["max_attempts_per_round"] = 1
        goal.retry_policy["max_rounds"] = 1
    await scheduler.start()
    answered: set[str] = set()
    while True:
        pending = [
            event["data"]["card"] for event in events
            if event.get("type") == "card_update"
            and event["data"]["card"]["card_type"] == "INTERVENE"
            and event["data"]["card"]["card_id"] not in answered
        ]
        if not pending:
            break
        for card in pending:
            answered.add(card["card_id"])
            await scheduler.answer_card(card["card_id"], {"choice": "continue"})
    await scheduler.wait_idle()
    return scheduler, events, started


def _empty_goal_gates(events: list[dict[str, Any]]) -> list[str]:
    return [
        event["data"]["goal_id"] for event in events
        if event.get("type") == "goal_gate"
        and event["data"].get("reason") == "empty_goal"
    ]


def _intervene_goals(events: list[dict[str, Any]]) -> set[str]:
    return {
        event["data"]["card"]["goal_id"] for event in events
        if event.get("type") == "card_update"
        and event["data"]["card"]["card_type"] == "INTERVENE"
    }


# ── 执行层（B）──────────────────────────────────────────────────────────


def test_wx1_真夹具_空_goal_不再冻结_研究到终态():
    plan = _wx1_plan()
    assert [len(goal.agents) for goal in plan.goals] == [19, 3, 10, 0, 6, 4]

    scheduler, events, started = asyncio.run(_drive_to_idle(plan))

    assert scheduler.status == "completed", scheduler.goal_statuses
    assert scheduler.goal_statuses["goal-4"] == "done"
    assert scheduler.goal_statuses["goal-6"] == "done"
    assert "goal-6" in started
    assert _empty_goal_gates(events) == ["goal-4"]
    assert "goal-4" not in _intervene_goals(events)


def test_最小夹具_中间_goal_删光_下游照跑_不发核对卡():
    scheduler, events, started = asyncio.run(_drive_to_idle(_minimal_plan()))

    assert scheduler.status == "completed", scheduler.goal_statuses
    assert scheduler.goal_statuses == {
        "goal-1": "done", "goal-2": "done", "goal-3": "done",
    }
    assert started == ["goal-1", "goal-3"]
    assert _empty_goal_gates(events) == ["goal-2"]
    assert _intervene_goals(events) == {"goal-1", "goal-3"}


def test_空_goal_的上游失败时照常连带跳过_不被当成_done():
    scheduler, events, started = asyncio.run(
        _drive_to_idle(_minimal_plan(), fail_goals=frozenset({"goal-1"}))
    )

    assert scheduler.status == "completed"
    assert scheduler.goal_statuses == {
        "goal-1": "failed", "goal-2": "skipped", "goal-3": "skipped",
    }
    assert _empty_goal_gates(events) == []
    assert started == ["goal-1"]


# ── 规划层（A）──────────────────────────────────────────────────────────


def _goal(plan, goal_id):
    return next(goal for goal in plan.goals if goal.goal_id == goal_id)


def _points_at(plan, goal_id: str) -> list[str]:
    hits: list[str] = []
    for goal in plan.goals:
        for agent in goal.agents:
            for item in agent.inputs:
                if item.get("from_goal") == goal_id:
                    hits.append(f"{agent.agent_id}.inputs:{item.get('artifact')}")
            opening = (agent.chapter or {}).get("opening") or {}
            for item in opening.get("inputs", []):
                if str(item.get("path", "")).startswith(f"goals/{goal_id}/"):
                    hits.append(f"{agent.agent_id}.opening:{item.get('path')}")
    return hits


def test_wx1_真夹具_规划层移出空_goal_下游改接且_lint_全过():
    from app.plan.lint import lint
    from app.plan.normalize import empty_goal_removal, normalize_plan

    plan = _wx1_plan()
    assert _points_at(plan, "goal-4")  # 尺子通电：旧计划确有指向 goal-4 的输入

    notes = normalize_plan(plan)

    assert [goal.goal_id for goal in plan.goals] == [
        "goal-1", "goal-2", "goal-3", "goal-5", "goal-6",
    ]
    assert _goal(plan, "goal-6").depends_on == ["goal-1", "goal-5"]
    assert _goal(plan, "goal-5").depends_on == ["goal-1", "goal-2", "goal-3"]
    assert _points_at(plan, "goal-4") == []
    removals = [empty_goal_removal(note) for note in notes]
    assert [item for item in removals if item] == [
        {"goal_id": "goal-4", "title": "国内社媒与用户口碑对豆包的评价采集"},
    ]
    assert any("goal-6（goal-4、goal-5 → goal-1、goal-5）" in note for note in notes)
    assert any("goal-6/data-cleaning-6" in note for note in notes)
    allocation = json.loads(
        (FIXTURE.parent / "plan-segments" / "allocation.json").read_text(encoding="utf-8")
    )
    assert lint(plan, collection_plan=allocation)["errors"] == []
    assert normalize_plan(plan) == []  # 章级第二次 normalize 不再重复留痕


def test_最小夹具_规划层移出空_goal_再交_scheduler_照常完成():
    from app.plan.normalize import normalize_plan

    plan = _minimal_plan()
    notes = normalize_plan(plan)

    assert [goal.goal_id for goal in plan.goals] == ["goal-1", "goal-3"]
    last = _goal(plan, "goal-3")
    assert last.depends_on == ["goal-1"]
    assert last.agents[0].inputs == [
        {"from_goal": "goal-1", "artifact": "goals/goal-1/agent-1.md"},
    ]
    assert last.agents[0].chapter["opening"]["inputs"] == [
        {"path": "goals/goal-1/agent-1.md"},
    ]
    assert notes[0].startswith("[修正31] goal-2「阶段 2 证据产物」一章不剩，已移出计划")

    scheduler, events, started = asyncio.run(_drive_to_idle(plan))
    assert scheduler.status == "completed"
    assert scheduler.goal_statuses == {"goal-1": "done", "goal-3": "done"}
    assert started == ["goal-1", "goal-3"]


def test_连续空_goal_改接穿透到非空上游():
    from app.plan.model import Plan
    from app.plan.normalize import normalize_plan

    source = make_plan_dict()
    goals = [make_goal(number) for number in range(1, 5)]
    goals[1]["agents"] = []
    goals[2]["agents"] = []
    goals[3]["depends_on"] = ["goal-3", "goal-1"]
    source["goals"] = goals
    source["baseline"] = None
    plan = Plan.from_dict(source)

    normalize_plan(plan)

    assert [goal.goal_id for goal in plan.goals] == ["goal-1", "goal-4"]
    assert _goal(plan, "goal-4").depends_on == ["goal-1"]


def test_没有空_goal_时规划层一条不动():
    from app.plan.model import Plan
    from app.plan.normalize import _repair_empty_goals

    source = make_plan_dict()
    source["baseline"] = None
    plan = Plan.from_dict(source)
    before = plan.to_dict()

    assert _repair_empty_goals(plan) == []
    assert plan.to_dict() == before


def test_空_goal_修正事件带结构化记录_别的修正不带():
    from app.plan.generate import _emit_repairs

    captured: list[Any] = []

    class Sink:
        def on_plan_event(self, event):
            captured.append(event)

    asyncio.run(_emit_repairs(Sink(), "r-x", [
        "[修正31] goal-4「口碑」一章不剩，已移出计划（原因）",
        "[修正31] goal-1/x 采集卡「weibo·豆包」不在分配表里，已删除",
    ]))

    assert captured[0].raw["empty_goal_removed"] == {"goal_id": "goal-4", "title": "口碑"}
    assert "empty_goal_removed" not in captured[1].raw
