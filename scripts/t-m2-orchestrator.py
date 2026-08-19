#!/usr/bin/env python3
"""M7 可复用：M2 Scheduler 两 goal 依赖、自动干预与确定失败样本。"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.orchestrator.scheduler import Scheduler, TaskRunResult
from app.plan.model import Plan


def _agent(agent_id: str, goal_id: str) -> dict[str, Any]:
    return {
        "agent_id": agent_id,
        "display_name": agent_id,
        "task": "执行 M2 回归任务并返回结构化结果。",
        "depends_on": [],
        "inputs": [],
        "engine": "claude",
        "model": None,
        "capability": {
            "profile": "readonly-analyst",
            "tools": ["fs.read"],
            "sources": [],
            "fs": {"read": [f"goals/{goal_id}/**"], "write": []},
            "network": "none",
            "shell": "none",
        },
        "prompt": {
            "preamble_ref": "common/v1",
            "body": "M2 回归",
            "assumptions_policy": "assume_and_declare",
        },
        "output": {
            "format": "markdown",
            "path": f"goals/{goal_id}/{agent_id}.md",
            "validators": ["file_exists"],
        },
        "extra_quota_credits": None,
        "origin": {"_node": "generated"},
        "status": "queued",
    }


def _goal(number: int, *, depends_on: list[str]) -> dict[str, Any]:
    goal_id = f"goal-{number}"
    return {
        "goal_id": goal_id,
        "title": f"M2 回归阶段 {number}",
        "objective": "形成可判定的 Scheduler 状态。",
        "depends_on": depends_on,
        "deliverable": {
            "format": "markdown",
            "path": f"goals/{goal_id}/result.md",
            "description": "状态迁移样本。",
        },
        "acceptance": ["状态必须进入 done 或 failed"],
        "intervention": {"on_complete": True, "prompt": "是否继续？"},
        "retry_policy": {
            "max_attempts_per_round": 2,
            "ask_engine_switch_at": 1,
            "max_rounds": 1,
            "goal_deadline_hours": 1,
            "on_exhausted": "fail_goal",
        },
        "on_upstream_failure": "skip",
        "agents": [_agent(f"agent-{number}", goal_id)],
        "status": "pending",
    }


def _plan() -> Plan:
    goals = [_goal(1, depends_on=[]), _goal(2, depends_on=["goal-1"])]
    return Plan.from_dict({
        "research_id": "r-m2-regression",
        "plan_rev": 1,
        "title": "M2 编排回归",
        "research_question": "验证两 goal 依赖与失败总闸",
        "use_case": "other",
        "status": "approved",
        "approved_at": "2026-08-19T00:00:00Z",
        "decision_balance": [{
            "q_id": "q-1",
            "question": "是否按默认路径回归？",
            "options": ["是", "否"],
            "input_type": "choice_2",
            "answer": "是",
            "affects": ["goal-1"],
            "answered_at": "2026-08-19T00:00:00Z",
        }],
        "expert_panel": None,
        "goals": goals,
        "change_log": [],
        "baseline": None,
        "baseline_source": "generated",
        "created_at": "2026-08-19T00:00:00Z",
        "updated_at": "2026-08-19T00:00:00Z",
    })


async def main() -> None:
    if os.getenv("OWLI_AUTO_CONFIRM") != "1":
        print("结构化验收: FAIL（请设置 OWLI_AUTO_CONFIRM=1）")
        return

    transitions: list[str] = []
    attempts = 0
    auto_tasks: list[asyncio.Task[Any]] = []
    scheduler: Scheduler

    async def run_task(agent: Any, context: Any) -> TaskRunResult:
        nonlocal attempts
        if context.goal_id == "goal-2":
            attempts += 1
            return TaskRunResult(False, engine=context.engine)
        return TaskRunResult(True, engine=context.engine)

    async def emit(event: dict[str, Any]) -> None:
        event_type = event.get("type")
        data = event.get("data", {})
        if event_type == "goal_update":
            transitions.append(f"{data['goal_id']}:{data['status']}")
        elif event_type == "card_update":
            card = data["card"]
            transitions.append(f"{card['card_type']}:{card['status']}")
            if card["card_type"] == "INTERVENE" and card["status"] == "pending":
                auto_tasks.append(asyncio.create_task(
                    scheduler.answer_card(
                        card["card_id"],
                        {"choice": "continue", "auto": True},
                    )
                ))

    def timer(_: float, __: Any) -> None:
        return None

    scheduler = Scheduler(
        _plan(),
        run_task,
        emit,
        lambda: datetime(2026, 8, 19, tzinfo=timezone.utc),
        timer,
    )
    await scheduler.start()
    while auto_tasks:
        pending = list(auto_tasks)
        auto_tasks.clear()
        await asyncio.gather(*pending)

    checks = {
        "goal-1": scheduler.goal_statuses["goal-1"] == "done",
        "goal-2": scheduler.goal_statuses["goal-2"] == "failed",
        "attempts": attempts == 2,
        "terminal": scheduler.status == "completed",
    }
    print("状态迁移序列")
    for index, item in enumerate(transitions, start=1):
        print(f"{index:02d}. {item}")
    print(f"必失败注入尝试次数={attempts}")
    print("结构化验收: " + ("PASS" if all(checks.values()) else "FAIL"))
    if not all(checks.values()):
        print(f"失败断言: {[name for name, passed in checks.items() if not passed]}")


if __name__ == "__main__":
    asyncio.run(main())
