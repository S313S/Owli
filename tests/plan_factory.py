from __future__ import annotations

from copy import deepcopy


def make_agent(agent_id: str, goal_id: str) -> dict:
    return {
        "agent_id": agent_id,
        "display_name": f"执行单元 {agent_id}",
        "entity": None,
        "task": "按固定查询式处理近 30 天资料并输出可复核结果。",
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
            "body": "查询式=feishu；时间窗=近 30 天；阈值=至少 3 条。",
            "assumptions_policy": "assume_and_declare",
        },
        "output": {
            "format": "markdown",
            "shape": "object",
            "path": f"goals/{goal_id}/{agent_id}.md",
            "validators": ["file_exists"],
        },
        "chapter": {
            "chapter_id": "ch-1",
            "chapter_type": "audit",
            "plan_path": f"goals/{goal_id}/ch-1.md",
            "opening": {
                "inputs": [],
                "task": "按固定查询式处理近 30 天资料并输出可复核结果。",
                "acceptance": ["文件存在且通过 validators"],
            },
            "closing": {
                "output": {"path": f"goals/{goal_id}/{agent_id}.md"},
                "entities": [],
                "expected_count": 1,
                "notes": {},
            },
        },
        "extra_quota_credits": None,
        "origin": {"_node": "generated"},
        "status": "queued",
    }


def make_goal(number: int) -> dict:
    goal_id = f"goal-{number}"
    return {
        "goal_id": goal_id,
        "title": f"阶段 {number} 证据产物",
        "objective": "形成可独立验收并供下游消费的阶段产物。",
        "depends_on": [] if number == 1 else [f"goal-{number - 1}"],
        "deliverable": {
            "format": "markdown",
            "shape": "object",
            "path": f"goals/{goal_id}/result.md",
            "description": "带来源与明确数量的阶段结果。",
        },
        "acceptance": ["文件存在且至少包含 3 条带链接的记录"],
        "intervention": {"on_complete": True, "prompt": "是否继续下一阶段？"},
        "retry_policy": {
            "max_attempts_per_round": 10,
            "ask_engine_switch_at": 5,
            "max_rounds": 2,
            "goal_deadline_hours": 12,
            "on_exhausted": "fail_goal",
        },
        "on_upstream_failure": "skip",
        "agents": [make_agent(f"agent-{number}", goal_id)],
        "status": "pending",
    }


def make_plan_dict() -> dict:
    goals = [make_goal(number) for number in range(1, 4)]
    return {
        "research_id": "r-01JXOWLI0000000000TEST00",
        "plan_rev": 1,
        "title": "飞书竞品优缺点挖掘",
        "research_question": "飞书与主要竞品相比有哪些优缺点？",
        "use_case": "product_competitor",
        "market_profile": "global_product",
        "market_profile_justification": "产品面向全球市场。",
        "subjects": [],
        "subjects_justification": "历史测试计划未声明研究实体。",
        "scale": "standard",
        "status": "awaiting_review",
        "approved_at": None,
        "decision_balance": [{
            "q_id": "q-1",
            "question": "本次优先服务哪类判断？",
            "options": ["产品路线", "市场话术"],
            "input_type": "single",
            "answer": "产品路线",
            "affects": ["goal-1"],
            "answered_at": "2026-08-19T01:00:00Z",
        }],
        "expert_panel": None,
        "goals": goals,
        "change_log": [],
        "baseline": {
            "title": "飞书竞品优缺点挖掘",
            "use_case": "product_competitor",
            "goals": deepcopy(goals),
        },
        "baseline_source": "generated",
        "created_at": "2026-08-19T00:00:00Z",
        "updated_at": "2026-08-19T01:00:00Z",
    }


def chapter_slots(raw_agents: list) -> list:
    """章号 → 模型写的那个 agent（`ch-N` 对应 `chapter_slots(...)[N-1]`）。

    §RATE-1 货 2：生成器在每个采集 agent 后自动插一个评级章，章号随之后移；
    评级章的章规格由系统确定性生成、不走引擎，所以这里只占位（None）不产出。
    """
    from app.plan.generate import _classify

    slots: list = []
    for item in raw_agents:
        slots.append(item)
        name = str(item.get("display_name") or item.get("name") or "")
        if _classify(name, "")[1] == "web-collector":
            slots.append(None)
    return slots


def attach_rating_agents(plan: dict) -> dict:
    """§RATE-1 货 2：给手写计划夹具补上「每个采集章配一个评级章」（规则 30）。

    形状与生成器排出来的一致：只依赖它评的那一章、走评级五件验证器、不产 deliverable。
    """
    serial = 0
    for goal in plan.get("goals", []):
        goal_id = str(goal["goal_id"])
        collectors = [
            agent for agent in goal["agents"]
            if agent.get("capability", {}).get("profile") == "web-collector"
        ]
        for collector in collectors:
            serial += 1  # agent_id 全计划唯一（与生成器的 counters 同口径）
            suffix = "" if serial == 1 else f"-{serial}"
            agent_id = f"reliability-audit{suffix}"
            rating = make_agent(agent_id, goal_id)
            rating["display_name"] = "可靠度审计"
            rating["task"] = (
                f"逐条评级 {collector['output']['path']} 里的每一条证据，"
                "并原样回带 permalink。"
            )
            rating["depends_on"] = [collector["agent_id"]]
            rating["output"] = {
                "format": "json", "shape": "array",
                "path": f"goals/{goal_id}/{agent_id}.json",
                "validators": [
                    "file_exists", "no_item_missing_rating",
                    "field_domain_whitelist:reliability_closed_set",
                    "rating_notes_matches_regex",
                    "rating_notes_scores_match_columns",
                ],
            }
            chapter_id = f"ch-{len(goal['agents']) + 1}"
            rating["chapter"] = {
                "chapter_id": chapter_id,
                "chapter_type": "audit",
                "plan_path": f"goals/{goal_id}/{chapter_id}.md",
                "opening": {
                    "inputs": [{"path": collector["output"]["path"]}],
                    "task": rating["task"],
                    "acceptance": ["逐条评级并回带原 permalink"],
                },
                "closing": {
                    "output": {"path": rating["output"]["path"]},
                    "entities": [], "expected_count": None, "notes": {},
                },
            }
            goal["agents"].append(rating)
    return plan
