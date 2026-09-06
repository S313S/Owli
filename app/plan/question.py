"""审核计划前的选项式决策天平。"""

from __future__ import annotations

from typing import Any, Mapping

from app.plan.model import Plan

#: q-2 的闭集。**顺序有讲究**：`不明` 排第一，是给自动批准档（`OWLI_AUTO_CONFIRM`）
#: 兜底的——它一律点第一个选项，点中「竞品团队」就等于替用户瞎猜读者是谁。
AUDIENCE_ROLES: tuple[str, ...] = ("不明", "竞品团队", "本产品团队", "投资与分析", "媒体与内容")
#: q-2 / q-3 跳过时的默认答案。预填在生成期，所以这两问**从不阻塞批准、也不发卡片**
#: （`runtime._publish_question` 见了已有答案就不发），用户想改在计划编辑页改。
AUDIENCE_UNKNOWN = "不明"


def make_questions(plan: Plan, query: str) -> list[dict[str, Any]]:
    """产出稳定的选项式追问。q-1 答案留空以阻塞批准；q-2 / q-3 预填默认值，可跳过。

    §RPT-2 货 1：q-1 只问「服务哪类决策」，不问「谁的决策」，于是正式稿的建议
    写给了字节的产品经理，而提问的多半是竞品 PM 或投资人（提货单 §2.2 第 4 条）。
    补的两问都可跳过——宁可写「对不同读者的含义」分三行，也不多拦一道批准。
    """

    goal_ids = [goal.goal_id for goal in plan.goals]
    if not goal_ids:
        raise ValueError("decision_balance 至少需要一个可引用的 goal")
    subject = query.strip()[:24] or "本次调研"
    skipped_at = plan.created_at
    return [
        {
            "q_id": "q-1",
            "question": f"{subject}的结果优先服务哪类决策？",
            "options": ["产品路线与功能取舍", "市场传播与销售话术", "两者兼顾"],
            "input_type": "single",
            "answer": None,
            "affects": goal_ids,
            "answered_at": None,
        },
        {
            "q_id": "q-2",
            "question": "这份报告主要给谁看？（可跳过，跳过按「不明」处理）",
            "options": list(AUDIENCE_ROLES),
            "input_type": "single",
            "answer": AUDIENCE_UNKNOWN,
            "affects": goal_ids,
            "answered_at": skipped_at,
        },
        {
            "q_id": "q-3",
            "question": "读者最想知道「这对我意味着什么」的哪一点？（一句话，可跳过）",
            "options": [],
            "input_type": "text",
            "answer": AUDIENCE_UNKNOWN,
            "affects": goal_ids,
            "answered_at": skipped_at,
        },
    ]


def audience_from(plan: Plan | Mapping[str, Any]) -> dict[str, str]:
    """把 q-2 / q-3 的答案取成 `{audience_role, audience_stake}`，缺就是「不明」。

    正式稿输入靠它拿读者身份；写手据此决定建议写给谁（提货单 货 4 ①）。
    `plan` 收 `Plan` 或它的 dict 快照（`reports.plan_snapshot` 存的就是后者）。
    """
    picked = {"audience_role": AUDIENCE_UNKNOWN, "audience_stake": AUDIENCE_UNKNOWN}
    keys = {"q-2": "audience_role", "q-3": "audience_stake"}
    balance = (plan.get("decision_balance") if isinstance(plan, Mapping)
               else plan.decision_balance) or []
    for item in balance:
        if not isinstance(item, Mapping):
            continue
        key = keys.get(str(item.get("q_id")))
        answer = item.get("answer")
        if key and isinstance(answer, str) and answer.strip():
            picked[key] = answer.strip()
    return picked


__all__ = ["AUDIENCE_ROLES", "AUDIENCE_UNKNOWN", "audience_from", "make_questions"]
