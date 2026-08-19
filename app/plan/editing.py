"""计划编辑、恢复初始化与批准冻结的领域规则。"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from typing import Any, Mapping

from app.plan.lint import lint
from app.plan.model import Plan
from app.plan.store import PlanRevisionConflict, commit_changes, save_plan
from app.store.dao import Store


AGENT_EDITABLE_FIELDS = (
    "agent_id", "display_name", "task", "depends_on", "inputs", "engine",
    "model", "capability", "output",
)
GOAL_EDITABLE_FIELDS = (
    "title", "objective", "depends_on", "deliverable", "acceptance",
    "on_upstream_failure",
)


class PlanEditRejected(ValueError):
    def __init__(self, message: str, details: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.details = details or []


class PlanLintRejected(ValueError):
    def __init__(self, result: dict[str, list[str]]) -> None:
        super().__init__("计划未通过 plan_lint，修改没有保存")
        self.result = result


class PlanApprovalRejected(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _short(value: Any) -> str:
    if isinstance(value, str):
        rendered = value
    else:
        rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    rendered = rendered.replace("\n", " ")
    return rendered if len(rendered) <= 80 else f"{rendered[:77]}..."


def _change(
    *,
    number: int,
    at: str,
    phase: str,
    scope: str,
    target_id: str,
    field: str,
    before: Any,
    after: Any,
    label: str,
) -> dict[str, Any]:
    return {
        "change_id": f"chg-{number}",
        "at": at,
        "phase": phase,
        "scope": scope,
        "target_id": target_id,
        "field": field,
        "before": copy.deepcopy(before),
        "after": copy.deepcopy(after),
        "summary": f"{label}：{_short(before)} → {_short(after)}",
        "reason": None,
        "actor": "user",
        "artifact_discarded": None,
        "feedback_id": None,
    }


def _reject(field: str, before: Any, after: Any) -> dict[str, Any]:
    return {
        "field": field,
        "before": copy.deepcopy(before),
        "after": copy.deepcopy(after),
        "reason": "该字段由系统或计划生成器维护，前端不可编辑",
    }


def _indexed(items: list[dict[str, Any]], key: str) -> dict[str, tuple[int, dict[str, Any]]]:
    return {str(item.get(key)): (index, item) for index, item in enumerate(items)}


def _phase(plan: Plan) -> str:
    return "runtime_intervention" if plan.approved_at else "plan_review"


def _agent_origin_key(field: str) -> str:
    return "prompt.body" if field == "prompt.body" else field


def _validate_and_collect(
    current: Plan,
    submitted: Mapping[str, Any],
    *,
    at: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    before = current.to_dict()
    proposed = copy.deepcopy(dict(submitted))
    rejected: list[dict[str, Any]] = []
    changes: list[dict[str, Any]] = []
    phase = _phase(current)

    if proposed.get("research_id") != current.research_id:
        rejected.append(_reject("research_id", current.research_id, proposed.get("research_id")))
    if proposed.get("plan_rev") != current.plan_rev:
        raise PlanRevisionConflict(
            f"计划版本冲突：{current.research_id} 当前 rev={current.plan_rev}，"
            f"请求 rev={proposed.get('plan_rev')}"
        )

    for field in (
        "research_question", "status", "approved_at", "expert_panel", "change_log",
        "baseline", "baseline_source", "created_at", "updated_at",
    ):
        if proposed.get(field) != before.get(field):
            rejected.append(_reject(field, before.get(field), proposed.get(field)))

    next_number = len(current.change_log) + 1

    def record(scope: str, target_id: str, field: str, old: Any, new: Any, label: str) -> None:
        nonlocal next_number
        if old == new:
            return
        changes.append(_change(
            number=next_number, at=at, phase=phase, scope=scope,
            target_id=target_id, field=field, before=old, after=new, label=label,
        ))
        next_number += 1

    for field, label in (("title", "计划标题"), ("use_case", "调研类型")):
        record("plan", current.research_id, field, before.get(field), proposed.get(field), label)

    old_questions = list(before.get("decision_balance", []))
    new_questions = list(proposed.get("decision_balance", []))
    if len(old_questions) != len(new_questions):
        rejected.append(_reject("decision_balance", old_questions, new_questions))
    else:
        for index, (old, new) in enumerate(zip(old_questions, new_questions)):
            for field in set(old) | set(new):
                path = f"decision_balance[{index}].{field}"
                if field in {"answer", "answered_at"}:
                    record("plan", current.research_id, path, old.get(field), new.get(field), "追问答案")
                elif old.get(field) != new.get(field):
                    rejected.append(_reject(path, old.get(field), new.get(field)))

    old_goals = _indexed(list(before.get("goals", [])), "goal_id")
    new_goals = _indexed(list(proposed.get("goals", [])), "goal_id")
    old_order = list(old_goals)
    new_order = list(new_goals)
    if old_order != new_order:
        record("plan", current.research_id, "goals", old_order, new_order, "子目标结构")

    for goal_id, (new_index, new_goal) in new_goals.items():
        old_entry = old_goals.get(goal_id)
        if old_entry is None:
            for agent in new_goal.get("agents", []):
                agent["origin"] = {**agent.get("origin", {}), "_node": "user"}
            continue
        old_index, old_goal = old_entry
        for field in ("goal_id", "retry_policy", "status"):
            if old_goal.get(field) != new_goal.get(field):
                rejected.append(_reject(
                    f"goals[{new_index}].{field}", old_goal.get(field), new_goal.get(field)
                ))
        for field in GOAL_EDITABLE_FIELDS:
            record(
                "goal", goal_id, f"goals[{new_index}].{field}",
                old_goal.get(field), new_goal.get(field), field,
            )
        old_intervention = old_goal.get("intervention", {})
        new_intervention = new_goal.get("intervention", {})
        if old_intervention.get("on_complete") != new_intervention.get("on_complete"):
            rejected.append(_reject(
                f"goals[{new_index}].intervention.on_complete",
                old_intervention.get("on_complete"), new_intervention.get("on_complete"),
            ))
        record(
            "goal", goal_id, f"goals[{new_index}].intervention.prompt",
            old_intervention.get("prompt"), new_intervention.get("prompt"), "干预问法",
        )

        old_agents = _indexed(list(old_goal.get("agents", [])), "agent_id")
        new_agents = _indexed(list(new_goal.get("agents", [])), "agent_id")
        if list(old_agents) != list(new_agents):
            record(
                "goal", goal_id, f"goals[{new_index}].agents",
                list(old_agents), list(new_agents), "Agent 结构",
            )
        for agent_id, (agent_index, new_agent) in new_agents.items():
            old_agent_entry = old_agents.get(agent_id)
            if old_agent_entry is None:
                new_agent["origin"] = {**new_agent.get("origin", {}), "_node": "user"}
                continue
            _, old_agent = old_agent_entry
            if new_agent.get("origin") != old_agent.get("origin"):
                rejected.append(_reject(
                    f"goals[{new_index}].agents[{agent_index}].origin",
                    old_agent.get("origin"), new_agent.get("origin"),
                ))
            for field in ("status", "extra_quota_credits"):
                if old_agent.get(field) != new_agent.get(field):
                    rejected.append(_reject(
                        f"goals[{new_index}].agents[{agent_index}].{field}",
                        old_agent.get(field), new_agent.get(field),
                    ))
            for field in AGENT_EDITABLE_FIELDS:
                old_value = old_agent.get(field)
                new_value = new_agent.get(field)
                if old_value != new_value:
                    new_agent.setdefault("origin", {})[_agent_origin_key(field)] = "user"
                    record(
                        "agent", agent_id,
                        f"goals[{new_index}].agents[{agent_index}].{field}",
                        old_value, new_value, field,
                    )
            old_prompt = old_agent.get("prompt", {})
            new_prompt = new_agent.get("prompt", {})
            for field in ("preamble_ref", "assumptions_policy"):
                if old_prompt.get(field) != new_prompt.get(field):
                    rejected.append(_reject(
                        f"goals[{new_index}].agents[{agent_index}].prompt.{field}",
                        old_prompt.get(field), new_prompt.get(field),
                    ))
            if old_prompt.get("body") != new_prompt.get("body"):
                new_agent.setdefault("origin", {})["prompt.body"] = "user"
                record(
                    "agent", agent_id,
                    f"goals[{new_index}].agents[{agent_index}].prompt.body",
                    old_prompt.get("body"), new_prompt.get("body"), "prompt.body",
                )

    if rejected:
        raise PlanEditRejected("包含不可编辑字段，修改没有保存", rejected)
    if not changes:
        raise PlanEditRejected("计划内容没有发生变化")
    proposed["plan_rev"] = current.plan_rev
    proposed["updated_at"] = before["updated_at"]
    return proposed, changes


def apply_edit(
    store: Store,
    current: Plan,
    submitted: Mapping[str, Any],
    *,
    at: str | None = None,
) -> tuple[Plan, dict[str, list[str]]]:
    timestamp = at or _now()
    raw, changes = _validate_and_collect(current, submitted, at=timestamp)
    candidate = Plan.from_dict(raw)
    lint_result = lint(candidate, for_approval=False)
    if lint_result["errors"]:
        raise PlanLintRejected(lint_result)
    updated = commit_changes(store, candidate, changes, expected_rev=current.plan_rev)
    return updated, lint_result


def approve(store: Store, current: Plan, *, at: str | None = None) -> Plan:
    if current.approved_at:
        return current
    unanswered = [
        item for item in current.decision_balance
        if item.get("answer") in (None, "", [], {})
    ]
    if unanswered:
        raise PlanApprovalRejected(f"还有 {len(unanswered)} 个追问未回答，不能批准计划")
    lint_result = lint(current, for_approval=True)
    if lint_result["errors"]:
        raise PlanLintRejected(lint_result)
    timestamp = at or _now()
    updated = Plan.from_dict(current.to_dict())
    updated.status = "approved"
    updated.approved_at = timestamp
    updated.updated_at = timestamp
    updated.plan_rev = current.plan_rev + 1
    return save_plan(store, updated, expected_rev=current.plan_rev)


def _baseline_agent(plan: Plan, agent_id: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    baseline = plan.to_dict()["baseline"]
    for goal in baseline["goals"]:
        for agent in goal["agents"]:
            if agent["agent_id"] == agent_id:
                return goal, agent
    return None


def _reset_agent_values(current: dict[str, Any] | None, baseline: dict[str, Any]) -> dict[str, Any]:
    restored = copy.deepcopy(baseline)
    origin = copy.deepcopy(baseline.get("origin", {"_node": "generated"}))
    old_origin = {} if current is None else current.get("origin", {})
    for field in AGENT_EDITABLE_FIELDS:
        if current is None or current.get(field) != baseline.get(field) or old_origin.get(field) in {"user", "reset"}:
            origin[_agent_origin_key(field)] = "reset"
    old_prompt = {} if current is None else current.get("prompt", {})
    if current is None or old_prompt.get("body") != baseline.get("prompt", {}).get("body") or old_origin.get("prompt.body") in {"user", "reset"}:
        origin["prompt.body"] = "reset"
    restored["origin"] = origin
    if current is not None:
        restored["status"] = current["status"]
        restored["extra_quota_credits"] = current["extra_quota_credits"]
    return restored


def reset(
    store: Store,
    current: Plan,
    *,
    scope: str,
    target_id: str | None = None,
    at: str | None = None,
) -> Plan:
    if scope not in {"plan", "goal", "agent"}:
        raise PlanEditRejected("scope 只能是 plan、goal 或 agent")
    if scope != "plan" and not target_id:
        raise PlanEditRejected("agent/goal 级恢复必须提供 target_id")
    timestamp = at or _now()
    raw = current.to_dict()
    baseline = raw["baseline"]
    phase = _phase(current)

    if scope == "agent":
        found = _baseline_agent(current, str(target_id))
        current_location: tuple[int, int, dict[str, Any]] | None = None
        for goal_index, goal in enumerate(raw["goals"]):
            for agent_index, agent in enumerate(goal["agents"]):
                if agent["agent_id"] == target_id:
                    current_location = (goal_index, agent_index, agent)
                    break
        if current_location is None:
            raise PlanEditRejected(f"找不到 agent：{target_id}")
        goal_index, agent_index, old_agent = current_location
        if found is None:
            raw["goals"][goal_index]["agents"].pop(agent_index)
        else:
            _, baseline_agent = found
            raw["goals"][goal_index]["agents"][agent_index] = _reset_agent_values(old_agent, baseline_agent)
        field = f"goals[{goal_index}].agents[{agent_index}].reset"
    elif scope == "goal":
        baseline_goals = _indexed(baseline["goals"], "goal_id")
        current_goals = _indexed(raw["goals"], "goal_id")
        current_entry = current_goals.get(str(target_id))
        if current_entry is None:
            raise PlanEditRejected(f"找不到 goal：{target_id}")
        current_index, old_goal = current_entry
        baseline_entry = baseline_goals.get(str(target_id))
        if baseline_entry is None:
            raw["goals"].pop(current_index)
        else:
            _, baseline_goal = baseline_entry
            restored_goal = copy.deepcopy(baseline_goal)
            old_agents = _indexed(old_goal["agents"], "agent_id")
            restored_goal["agents"] = [
                _reset_agent_values(old_agents.get(agent["agent_id"], (0, None))[1], agent)
                for agent in baseline_goal["agents"]
            ]
            raw["goals"][current_index] = restored_goal
        field = f"goals[{current_index}].reset"
    else:
        old_goals = _indexed(raw["goals"], "goal_id")
        restored_goals = copy.deepcopy(baseline["goals"])
        for goal in restored_goals:
            old_goal = old_goals.get(goal["goal_id"], (0, None))[1]
            old_agents = {} if old_goal is None else _indexed(old_goal["agents"], "agent_id")
            goal["agents"] = [
                _reset_agent_values(old_agents.get(agent["agent_id"], (0, None))[1], agent)
                for agent in goal["agents"]
            ]
        raw["title"] = baseline["title"]
        raw["use_case"] = baseline["use_case"]
        raw["goals"] = restored_goals
        field = "plan.reset"

    candidate = Plan.from_dict(raw)
    lint_result = lint(candidate, for_approval=False)
    if lint_result["errors"]:
        raise PlanLintRejected(lint_result)
    change = _change(
        number=len(current.change_log) + 1,
        at=timestamp,
        phase=phase,
        scope=scope,
        target_id=current.research_id if scope == "plan" else str(target_id),
        field=field,
        before="current",
        after="baseline",
        label="恢复初始化",
    )
    return commit_changes(store, candidate, [change], expected_rev=current.plan_rev)
