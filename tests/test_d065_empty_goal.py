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
