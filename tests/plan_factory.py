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
