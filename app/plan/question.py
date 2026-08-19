"""审核计划前的选项式决策天平。"""

from __future__ import annotations

from typing import Any

from app.plan.model import Plan


def make_questions(plan: Plan, query: str) -> list[dict[str, Any]]:
    """产出稳定的单条选项式追问，答案留空以阻塞批准。"""

    goal_ids = [goal.goal_id for goal in plan.goals]
    if not goal_ids:
        raise ValueError("decision_balance 至少需要一个可引用的 goal")
    subject = query.strip()[:24] or "本次调研"
    return [
        {
            "q_id": "q-1",
            "question": f"{subject}的结果优先服务哪类决策？",
            "options": ["产品路线与功能取舍", "市场传播与销售话术", "两者兼顾"],
            "input_type": "single",
            "answer": None,
            "affects": goal_ids,
            "answered_at": None,
        }
    ]


__all__ = ["make_questions"]
