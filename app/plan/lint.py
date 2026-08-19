"""计划树保存与批准前的 13 条阻断校验和 6 类质量提示。"""

from __future__ import annotations

import re
from collections import Counter, deque
from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping

from app.plan.model import Plan


_KEBAB = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_BAD_ACCEPTANCE = ("良好", "充分", "尽量", "合理")
_QUESTION_INSTRUCTIONS = ("询问用户", "确认后再继续", "请告诉我")
_FORBIDDEN_FIELDS = {
    "estimated_minutes", "estimated_tokens", "estimated_cost",
    "planned_steps", "step_count",
}

# 值为 (最少参数个数, 最多参数个数, 参数是否必须为整数)。
_VALIDATORS: dict[str, tuple[int, int | None, bool]] = {
    "file_exists": (0, 0, False),
    "zip_entry_glob_exists": (1, 1, False),
    "openpyxl_reload_ok": (0, 0, False),
    "json_array_min_items": (1, 1, True),
    "json_array_between": (2, 2, True),
    "each_item_has": (1, None, False),
    "field_domain_whitelist": (1, 1, False),
    "sections_exist": (1, None, False),
    "section_exists": (1, 1, False),
    "list_items_min": (1, 1, True),
    "each_insight_has_citation": (0, 0, False),
    "table_rows_min": (1, 1, True),
    "table_rows_between": (2, 2, True),
    "table_no_empty_cells": (0, 0, False),
    "each_row_urls_reachable": (1, None, False),
    "citation_marks_resolvable": (0, 0, False),
    "no_orphan_citation": (0, 0, False),
    "db_row_exists": (1, 1, False),
    "db_field_non_empty": (1, 1, False),
    "claims_backfilled": (1, None, False),
    "no_item_missing_rating": (0, 0, False),
    "rating_notes_matches_regex": (0, 0, False),
    "rating_notes_scores_match_columns": (0, 0, False),
    "no_baseline_prefix_left": (0, 0, False),
    "norm_method_in_enum": (0, 0, False),
    "norm_context_required_keys": (0, 0, False),
    "xlsx_sheets_exact": (1, None, False),
}


def _data(plan: Plan | Mapping[str, Any]) -> dict[str, Any]:
    return plan.to_dict() if isinstance(plan, Plan) else dict(plan)


def _agents(goals: Iterable[dict[str, Any]]):
    for goal in goals:
        for agent in goal.get("agents", []):
            yield goal, agent


def _cycle(ids: list[str], dependencies: dict[str, list[str]]) -> list[str] | None:
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        state[node] = 1
        stack.append(node)
        for dependency in dependencies.get(node, []):
            if dependency not in dependencies:
                continue
            if state.get(dependency) == 1:
                start = stack.index(dependency)
                return stack[start:]
            if state.get(dependency, 0) == 0:
                found = visit(dependency)
                if found:
                    return found
        stack.pop()
        state[node] = 2
        return None

    for node in ids:
        if state.get(node, 0) == 0:
            found = visit(node)
            if found:
                return found
    return None


def _graph_errors(
    ids: list[str], dependencies: dict[str, list[str]], location: str
) -> list[str]:
    messages: list[str] = []
    known = set(ids)
    for node, refs in dependencies.items():
        for ref in refs:
            if ref not in known:
                messages.append(
                    f"[规则2] {location}/{node}.depends_on 引用了不存在的 id：{ref}"
                )
    indegree = {node: 0 for node in ids}
    downstream: dict[str, list[str]] = {node: [] for node in ids}
    for node, refs in dependencies.items():
        for ref in refs:
            if ref in known:
                indegree[node] += 1
                downstream[ref].append(node)
    queue = deque(node for node in ids if indegree[node] == 0)
    visited = 0
    while queue:
        node = queue.popleft()
        visited += 1
        for child in downstream[node]:
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    found = _cycle(ids, dependencies) if visited != len(ids) else None
    if found:
        messages.append(f"[规则2] {location} 依赖成环，环上的 id：{found}")
    return messages


def _rule_1(goals: list[dict[str, Any]]) -> list[str]:
    messages: list[str] = []
    goal_ids = [str(goal.get("goal_id", "")) for goal in goals]
    for value, count in Counter(goal_ids).items():
        if not value or count > 1:
            messages.append(f"[规则1] plan.goals 的 goal_id 不唯一或为空：{value!r}")
    agent_ids = [str(agent.get("agent_id", "")) for _, agent in _agents(goals)]
    for value, count in Counter(agent_ids).items():
        if not value or count > 1:
            messages.append(f"[规则1] agent_id 必须全 plan 唯一：{value!r}")
        if value and not _KEBAB.fullmatch(value):
            messages.append(f"[规则1] agent_id 不是合法 kebab-case：{value}")
    return messages


def _rule_2(goals: list[dict[str, Any]]) -> list[str]:
    goal_ids = [str(goal.get("goal_id", "")) for goal in goals]
    dependencies = {
        str(goal.get("goal_id", "")): list(goal.get("depends_on", []))
        for goal in goals
    }
    messages = _graph_errors(goal_ids, dependencies, "plan.goals")
    for goal in goals:
        goal_id = str(goal.get("goal_id", ""))
        agents = goal.get("agents", [])
        agent_ids = [str(agent.get("agent_id", "")) for agent in agents]
        agent_dependencies = {
            str(agent.get("agent_id", "")): list(agent.get("depends_on", []))
            for agent in agents
        }
        messages.extend(_graph_errors(agent_ids, agent_dependencies, goal_id))
    return messages


def _rule_3(goals: list[dict[str, Any]]) -> list[str]:
    messages: list[str] = []
    all_agent_goals = {
        str(agent.get("agent_id", "")): str(goal.get("goal_id", ""))
        for goal, agent in _agents(goals)
    }
    for goal, agent in _agents(goals):
        goal_id = str(goal.get("goal_id", ""))
        agent_id = str(agent.get("agent_id", ""))
        local_ids = {
            str(item.get("agent_id", "")) for item in goal.get("agents", [])
        }
        for dependency in agent.get("depends_on", []):
            if dependency not in local_ids and dependency in all_agent_goals:
                messages.append(
                    f"[规则3] {goal_id}/{agent_id}.depends_on 跨 goal 引用 {dependency}；"
                    "请上升为 goal.depends_on"
                )
        upstream = set(goal.get("depends_on", []))
        for index, item in enumerate(agent.get("inputs", [])):
            from_goal = item.get("from_goal") if isinstance(item, dict) else None
            if from_goal not in upstream:
                messages.append(
                    f"[规则3] {goal_id}/{agent_id}.inputs[{index}].from_goal={from_goal!r} "
                    f"未在 {goal_id}.depends_on 声明"
                )
    return messages


def _rule_4(goals: list[dict[str, Any]]) -> list[str]:
    messages: list[str] = []
    for goal in goals:
        goal_id = str(goal.get("goal_id", ""))
        acceptance = goal.get("acceptance", [])
        if not acceptance:
            messages.append(f"[规则4] {goal_id}.acceptance 至少需要 1 条可判定标准")
            continue
        for index, item in enumerate(acceptance):
            hit = next((word for word in _BAD_ACCEPTANCE if word in str(item)), None)
            if hit:
                messages.append(
                    f"[规则4] {goal_id}.acceptance[{index}] 含不可判定表述“{hit}”：{item}"
                )
    return messages


def _rule_5(goals: list[dict[str, Any]]) -> list[str]:
    return [
        f"[规则5] {agent.get('agent_id')}.output.validators 至少需要 1 个校验器"
        for _, agent in _agents(goals)
        if not agent.get("output", {}).get("validators")
    ]


def _rule_6(goals: list[dict[str, Any]]) -> list[str]:
    messages: list[str] = []
    for _, agent in _agents(goals):
        agent_id = str(agent.get("agent_id", ""))
        capability = agent.get("capability", {})
        tools = set(capability.get("tools", []))
        sources = set(capability.get("sources", []))
        for tool in tools:
            if isinstance(tool, str) and tool.startswith("source."):
                source = tool.split(".", 1)[1]
                if source != "*" and source not in sources:
                    messages.append(
                        f"[规则6] {agent_id}.capability.tools 声明 {tool}，"
                        f"但 capability.sources 未包含 {source}"
                    )
        write_paths = capability.get("fs", {}).get("write", [])
        if write_paths and "fs.write" not in tools:
            messages.append(
                f"[规则6] {agent_id}.capability.fs.write 非空，但 tools 未声明 fs.write"
            )
        for mode in ("read", "write"):
            for index, path in enumerate(capability.get("fs", {}).get(mode, [])):
                pure = PurePosixPath(str(path))
                if pure.is_absolute() or ".." in pure.parts:
                    messages.append(
                        f"[规则6] {agent_id}.capability.fs.{mode}[{index}] 路径越界：{path}"
                    )
    return messages


def _rule_7(goals: list[dict[str, Any]]) -> list[str]:
    messages: list[str] = []
    for _, agent in _agents(goals):
        capability = agent.get("capability", {})
        if capability.get("shell", "none") != "none" and agent.get("engine") != "codex":
            messages.append(
                f"[规则7] {agent.get('agent_id')}.engine 必须为 codex："
                f"capability.shell={capability.get('shell')}"
            )
    return messages


def _rule_8(goals: list[dict[str, Any]]) -> list[str]:
    return [
        f"[规则8] {agent.get('agent_id')}.capability.justification 不能为空：network=open 需要理由"
        for _, agent in _agents(goals)
        if agent.get("capability", {}).get("network") == "open"
        and not str(agent.get("capability", {}).get("justification", "")).strip()
    ]


def _rule_9(goals: list[dict[str, Any]]) -> list[str]:
    messages: list[str] = []
    for _, agent in _agents(goals):
        body = str(agent.get("prompt", {}).get("body", ""))
        hit = next((word for word in _QUESTION_INSTRUCTIONS if word in body), None)
        if hit:
            messages.append(
                f"[规则9] {agent.get('agent_id')}.prompt.body 含向用户提问的指令“{hit}”"
            )
    return messages


def _rule_10(value: Any, location: str = "plan") -> list[str]:
    messages: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_location = f"{location}.{key}"
            if key in _FORBIDDEN_FIELDS or key.startswith("estimated_"):
                messages.append(
                    f"[规则10] {child_location} 是禁止进入计划书的预估字段"
                )
            messages.extend(_rule_10(child, child_location))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            messages.extend(_rule_10(child, f"{location}[{index}]"))
    return messages


def _rule_11(goals: list[dict[str, Any]]) -> list[str]:
    return [
        f"[规则11] {goal.get('goal_id')}.intervention.on_complete 必须恒为 true"
        for goal in goals
        if goal.get("intervention", {}).get("on_complete") is not True
    ]


def _empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _rule_12(plan: dict[str, Any]) -> list[str]:
    messages: list[str] = []
    for index, question in enumerate(plan.get("decision_balance", [])):
        if _empty(question.get("answer")):
            q_id = question.get("q_id") or f"decision_balance[{index}]"
            messages.append(f"[规则12] {q_id}.answer 不能为空，否则不能批准计划")
    return messages


def _validator_problem(specification: Any) -> str | None:
    if not isinstance(specification, str) or not specification:
        return "校验器规格必须是非空字符串"
    name, separator, raw = specification.partition(":")
    if not re.fullmatch(r"[a-z0-9_]+", name):
        return f"校验器名不符合 snake_case：{name!r}"
    contract = _VALIDATORS.get(name)
    if contract is None:
        return f"校验器 {name} 不在 validator-registry 封闭枚举中"
    minimum, maximum, integers = contract
    if separator:
        if ":" in raw or " " in raw:
            return "参数不允许含冒号或空格"
        arguments = raw.split(",")
        if any(argument == "" for argument in arguments):
            return "参数不允许为空"
    else:
        arguments = []
    if len(arguments) < minimum or (maximum is not None and len(arguments) > maximum):
        expected = str(minimum) if minimum == maximum else f"{minimum}..{maximum or 'N'}"
        return f"参数个数应为 {expected}，实际为 {len(arguments)}"
    if integers:
        for argument in arguments:
            try:
                int(argument)
            except ValueError:
                return f"参数必须是整数，实际为 {argument!r}"
    return None


def _rule_13(goals: list[dict[str, Any]]) -> list[str]:
    messages: list[str] = []
    for _, agent in _agents(goals):
        validators = agent.get("output", {}).get("validators", [])
        for index, specification in enumerate(validators):
            problem = _validator_problem(specification)
            if problem:
                messages.append(
                    f"[规则13] {agent.get('agent_id')}.output.validators[{index}] "
                    f"{specification!r} 非法：{problem}"
                )
    return messages


def _warnings(goals: list[dict[str, Any]]) -> list[str]:
    messages: list[str] = []
    for _, agent in _agents(goals):
        agent_id = agent.get("agent_id")
        validators = agent.get("output", {}).get("validators", [])
        if any(str(item).partition(":")[0] == "section_exists" for item in validators):
            messages.append(
                f"[警告1] {agent_id}.output.validators 使用单数别名 section_exists；"
                "新计划建议改为 sections_exist"
            )
    if not 3 <= len(goals) <= 7:
        messages.append(f"[警告2] plan.goals 共 {len(goals)} 个，建议保持在 3–7 个")
    for goal in goals:
        title = str(goal.get("title", ""))
        deliverable = goal.get("deliverable", {})
        if any(word in title for word in ("搜索", "阅读", "总结", "整理")) and not (
            str(deliverable.get("path", "")).strip()
            and str(deliverable.get("description", "")).strip()
        ):
            messages.append(
                f"[警告3] {goal.get('goal_id')}.title={title!r} 疑似按工种拆分且无独立产物"
            )
    for _, agent in _agents(goals):
        body = str(agent.get("prompt", {}).get("body", ""))
        missing = []
        if not re.search(r"阈值|至少|≥|>|<=|≤", body):
            missing.append("阈值")
        if not re.search(r"时间窗|近\s*\d+\s*[天月年]|日期", body):
            missing.append("时间窗")
        if not re.search(r"查询式|query|关键词", body, re.IGNORECASE):
            missing.append("查询式")
        if missing:
            messages.append(
                f"[警告4] {agent.get('agent_id')}.prompt.body 缺可复现参数：{missing}"
            )
    platforms: dict[str, list[str]] = {}
    for _, agent in _agents(goals):
        for source in agent.get("capability", {}).get("sources", []):
            platforms.setdefault(str(source), []).append(str(agent.get("agent_id")))
    for source, agent_ids in platforms.items():
        if len(agent_ids) > 1:
            messages.append(f"[警告5] 平台 {source} 被多个 agent 重复采集：{agent_ids}")
    for goal in goals:
        count = len(goal.get("agents", []))
        if count > 5:
            messages.append(f"[警告6] {goal.get('goal_id')} 下有 {count} 个 agent，超过 5 个")
    return messages


def lint(plan: Plan | Mapping[str, Any]) -> dict[str, list[str]]:
    """按 agents-spec §10 的编号顺序返回全部问题，不在首错处短路。"""
    raw = _data(plan)
    goals = list(raw.get("goals", []))
    errors: list[str] = []
    errors.extend(_rule_1(goals))
    errors.extend(_rule_2(goals))
    errors.extend(_rule_3(goals))
    errors.extend(_rule_4(goals))
    errors.extend(_rule_5(goals))
    errors.extend(_rule_6(goals))
    errors.extend(_rule_7(goals))
    errors.extend(_rule_8(goals))
    errors.extend(_rule_9(goals))
    errors.extend(_rule_10(raw))
    errors.extend(_rule_11(goals))
    errors.extend(_rule_12(raw))
    errors.extend(_rule_13(goals))
    return {"errors": errors, "warnings": _warnings(goals)}
