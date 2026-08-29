"""计划树保存与批准前的 29 条阻断校验和 7 类质量提示。"""

from __future__ import annotations

import re
from collections import Counter, deque
from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping

from app.plan.model import (
    Plan, SECTIONED_CHAPTER_KINDS, agent_kind_of, rated_collector_id,
)


_KEBAB = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_BAD_ACCEPTANCE = ("良好", "充分", "尽量", "合理")
# D-024：黑词表本身留着，但「给黑词下机器定义」的句子必须放行——X-1 规划期被
# 「证据条数≥3 记为『充分』、1-2 条记为『部分』」整份计划判死，那句恰恰是最可判定的。
# 口径（早上用户拍板）：同一条 acceptance 里出现量化定义即视为已判定，不要求量化
# 紧贴黑词。代价是「尽量覆盖 3 条渠道」这类混合句会漏网，权衡后接受：漏一条提示
# 远轻于误杀整份计划。
_QUANTIFIED_ACCEPTANCE = re.compile(
    r"[≥≤<>]=?\s*\d"          # ≥3 / >= 10 / <5
    r"|\d+\s*条"              # 3 条 / 不少于 5 条
    r"|记为\s*[「『\"\']"      # …记为「充分」这类档位映射句式
)
_QUESTION_INSTRUCTIONS = ("询问用户", "确认后再继续", "请告诉我")
_PENDING_ENTITY = re.compile(r"待定实体\d+")
_FORBIDDEN_FIELDS = {
    "estimated_minutes", "estimated_tokens", "estimated_cost",
    "planned_steps", "step_count",
}
# 这是 planner 可选信息源的许可名单，不是每档都必须选全的义务清单。
# 分档依据是相关讨论发生在哪个语言/平台生态，而不是产品公司的国籍：实测小红书
# 有大量海外产品讨论，Reddit 却几乎没有国内产品讨论，因此两档的许可范围有意不对称。
# 放宽最多引入一个规划器可以不选的噪音源；卡死则会让该源永远不可达（D-013 即如此）。
_SOURCE_MARKET_PROFILES = {
    "cn_product": {"web_search", "x", "xhs", "douyin"},
    "global_product": {
        "web_search", "x", "hacker_news", "product_hunt", "reddit", "xhs",
        "douyin",
    },
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
    "sectioned_document_valid": (0, 0, False),
    "no_item_missing_rating": (0, 0, False),
    "rating_notes_matches_regex": (0, 0, False),
    "rating_notes_scores_match_columns": (0, 0, False),
    "no_baseline_prefix_left": (0, 0, False),
    "norm_method_in_enum": (0, 0, False),
    "norm_context_required_keys": (0, 0, False),
    "xlsx_sheets_exact": (1, None, False),
}

_FINDINGS_FIELDS = frozenset({
    "competitor", "pros", "cons", "statement", "supporting_evidence",
    "evidence_id", "is_singleton", "conflicts", "gaps",
})
_RATINGS_FIELDS = frozenset({
    "score_authority", "score_freshness", "score_crossref",
    "score_completeness", "score_independence", "rating_notes", "rated_by",
})
_VALIDATOR_FIELDS: dict[str, frozenset[str]] = {
    "no_item_missing_rating": _RATINGS_FIELDS,
    "rating_notes_matches_regex": frozenset({"rating_notes"}),
    "rating_notes_scores_match_columns": _RATINGS_FIELDS,
    "field_domain_whitelist": frozenset({"rating_notes", "rated_by"}),
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


def _rated_chapter(goal: Mapping[str, Any], agent: Mapping[str, Any]) -> str:
    """§RATE-1 货 2：这一章是不是「只评一个采集章的评级章」，是则返回那章 agent_id。"""
    return rated_collector_id(
        output=agent.get("output", {}),
        depends_on=agent.get("depends_on", []),
        deliverable_path=str(goal.get("deliverable", {}).get("path", "")),
        collector_ids=[
            str(item.get("agent_id", "")) for item in goal.get("agents", [])
            if item.get("capability", {}).get("profile") == "web-collector"
        ],
    )


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
    goal_dependencies = {
        str(goal.get("goal_id", "")): {
            str(item) for item in goal.get("depends_on", [])
        }
        for goal in goals
    }
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
        upstream: set[str] = set()
        pending = list(goal_dependencies.get(goal_id, set()))
        while pending:
            dependency = pending.pop()
            if dependency in upstream:
                continue
            upstream.add(dependency)
            pending.extend(goal_dependencies.get(dependency, set()))
        for index, item in enumerate(agent.get("inputs", [])):
            from_goal = item.get("from_goal") if isinstance(item, dict) else None
            if from_goal not in upstream:
                messages.append(
                    f"[规则3] {goal_id}/{agent_id}.inputs[{index}].from_goal={from_goal!r} "
                    f"未在 {goal_id} 的 depends_on 祖先链声明"
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
            text = str(item)
            hit = next((word for word in _BAD_ACCEPTANCE if word in text), None)
            if hit and not _QUANTIFIED_ACCEPTANCE.search(text):
                messages.append(
                    f"[规则4] {goal_id}.acceptance[{index}] 含不可判定表述“{hit}”：{item}"
                )
    return messages


def _rule_5(goals: list[dict[str, Any]]) -> list[str]:
    return [
        f"[规则5] {goal.get('goal_id')}/{agent.get('agent_id')}"
        ".output.validators 至少需要 1 个校验器"
        for goal, agent in _agents(goals)
        if not agent.get("output", {}).get("validators")
    ]


def _rule_6(goals: list[dict[str, Any]]) -> list[str]:
    messages: list[str] = []
    for goal, agent in _agents(goals):
        goal_id = str(goal.get("goal_id", ""))
        agent_id = str(agent.get("agent_id", ""))
        capability = agent.get("capability", {})
        tools = set(capability.get("tools", []))
        sources = set(capability.get("sources", []))
        for tool in tools:
            if isinstance(tool, str) and tool.startswith("source."):
                source = tool.split(".", 1)[1]
                if source != "*" and source not in sources:
                    messages.append(
                        f"[规则6] {goal_id}/{agent_id}.capability.tools 声明 {tool}，"
                        f"但 capability.sources 未包含 {source}"
                    )
        write_paths = capability.get("fs", {}).get("write", [])
        if write_paths and "fs.write" not in tools:
            messages.append(
                f"[规则6] {goal_id}/{agent_id}.capability.fs.write 非空，"
                "但 tools 未声明 fs.write"
            )
        for mode in ("read", "write"):
            for index, path in enumerate(capability.get("fs", {}).get(mode, [])):
                pure = PurePosixPath(str(path))
                if pure.is_absolute() or ".." in pure.parts:
                    messages.append(
                        f"[规则6] {goal_id}/{agent_id}.capability.fs."
                        f"{mode}[{index}] 路径越界：{path}"
                    )
    return messages


def _rule_7(goals: list[dict[str, Any]]) -> list[str]:
    messages: list[str] = []
    for goal, agent in _agents(goals):
        capability = agent.get("capability", {})
        if capability.get("shell", "none") != "none" and agent.get("engine") != "codex":
            messages.append(
                f"[规则7] {goal.get('goal_id')}/{agent.get('agent_id')}"
                ".engine 必须为 codex："
                f"capability.shell={capability.get('shell')}"
            )
    return messages


def _rule_8(goals: list[dict[str, Any]]) -> list[str]:
    return [
        f"[规则8] {goal.get('goal_id')}/{agent.get('agent_id')}"
        ".capability.justification 不能为空：network=open 需要理由"
        for goal, agent in _agents(goals)
        if agent.get("capability", {}).get("network") == "open"
        and not str(agent.get("capability", {}).get("justification", "")).strip()
    ]


def _rule_9(goals: list[dict[str, Any]]) -> list[str]:
    messages: list[str] = []
    for goal, agent in _agents(goals):
        body = str(agent.get("prompt", {}).get("body", ""))
        hit = next((word for word in _QUESTION_INSTRUCTIONS if word in body), None)
        if hit:
            messages.append(
                f"[规则9] {goal.get('goal_id')}/{agent.get('agent_id')}"
                f".prompt.body 含向用户提问的指令“{hit}”"
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


def _rule_29(plan: dict[str, Any]) -> list[str]:
    """批准前不允许活动计划里残留复用实体占位符。"""

    locations: list[str] = []

    def inspect(location: str, value: Any) -> None:
        if isinstance(value, str):
            if _PENDING_ENTITY.search(value):
                locations.append(location)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                inspect(f"{location}[{index}]", child)
        elif isinstance(value, dict):
            for key, child in value.items():
                inspect(f"{location}.{key}", child)

    for index, question in enumerate(plan.get("decision_balance", [])):
        inspect(f"decision_balance[{index}].question", question.get("question"))
        inspect(f"decision_balance[{index}].options", question.get("options"))
    for goal in plan.get("goals", []):
        goal_id = str(goal.get("goal_id") or "goal")
        for field in ("title", "objective", "acceptance"):
            inspect(f"{goal_id}.{field}", goal.get(field))
        inspect(
            f"{goal_id}.deliverable.description",
            goal.get("deliverable", {}).get("description"),
        )
        inspect(
            f"{goal_id}.intervention.prompt",
            goal.get("intervention", {}).get("prompt"),
        )
        for agent in goal.get("agents", []):
            agent_id = str(agent.get("agent_id") or "agent")
            anchor = f"{goal_id}/{agent_id}"
            for field in ("entity", "task"):
                inspect(f"{anchor}.{field}", agent.get(field))
            inspect(f"{anchor}.prompt.body", agent.get("prompt", {}).get("body"))
            chapter = agent.get("chapter")
            if isinstance(chapter, dict):
                inspect(f"{anchor}.chapter.opening", chapter.get("opening"))
                closing = chapter.get("closing")
                if isinstance(closing, dict):
                    inspect(
                        f"{anchor}.chapter.closing.entities",
                        closing.get("entities"),
                    )

    if not locations:
        return []
    unique_locations = list(dict.fromkeys(locations))
    preview = "、".join(unique_locations[:12])
    suffix = f" 等共 {len(unique_locations)} 处" if len(unique_locations) > 12 else ""
    return [
        f"[规则29] 批准前必须把待定实体占位符替换为真实实体：{preview}{suffix}"
    ]


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
    for goal, agent in _agents(goals):
        validators = agent.get("output", {}).get("validators", [])
        for index, specification in enumerate(validators):
            problem = _validator_problem(specification)
            if problem:
                messages.append(
                    f"[规则13] {goal.get('goal_id')}/{agent.get('agent_id')}"
                    f".output.validators[{index}] "
                    f"{specification!r} 非法：{problem}"
                )
    return messages


def _rule_14(goals: list[dict[str, Any]]) -> list[str]:
    """数组校验器不得与同一 agent 的结构化 object 契约冲突。"""
    messages: list[str] = []
    for goal, agent in _agents(goals):
        validators = agent.get("output", {}).get("validators", [])
        has_array_validator = any(
            str(item).partition(":")[0] == "json_array_min_items"
            for item in validators
        )
        if has_array_validator and agent.get("output", {}).get("shape") == "object":
            messages.append(
                f"[规则14] {goal.get('goal_id')}/{agent.get('agent_id')} 使用 "
                "json_array_min_items，但 output.shape=object"
            )
    return messages


def _rule_15(goals: list[dict[str, Any]]) -> list[str]:
    """同一 goal 内不同 agent 不得覆盖同一产物路径。"""
    messages: list[str] = []
    for goal in goals:
        by_path: dict[str, list[str]] = {}
        for agent in goal.get("agents", []):
            path = str(agent.get("output", {}).get("path", "")).strip()
            if path:
                normalized = str(PurePosixPath(path.replace("\\", "/")))
                by_path.setdefault(normalized, []).append(str(agent.get("agent_id", "")))
        for path, agent_ids in by_path.items():
            if len(agent_ids) > 1:
                messages.append(
                    f"[规则15] {goal.get('goal_id')} 内多个 agent 的 output.path "
                    f"相同：{path}；agent={agent_ids}"
                )
    return messages


def _rule_16(goals: list[dict[str, Any]]) -> list[str]:
    """R4：计划不得要求把不同平台的原始热度直接求和或加权。"""
    messages: list[str] = []
    heat = re.compile(
        r"热度|points?|votes?(?:_count)?|likes?(?:_count)?|views?|播放量|点赞量",
        re.IGNORECASE,
    )
    aggregate = re.compile(r"相加|求和|加总|总和|加权(?:合成|求和|相加)?")
    negation = re.compile(r"禁止|不得|不允许|绝不|不可")
    platform_signals = (
        re.compile(r"\b(?:HN|Hacker\s*News|points?)\b", re.IGNORECASE),
        re.compile(r"\b(?:PH|Product\s*Hunt|votes?(?:_count)?)\b", re.IGNORECASE),
        re.compile(r"\bX\b|like_count", re.IGNORECASE),
        re.compile(r"B站|bilibili|\bview\b", re.IGNORECASE),
        re.compile(r"小红书|xhs|liked_count", re.IGNORECASE),
        re.compile(r"抖音|douyin|digg_count", re.IGNORECASE),
    )
    for goal, agent in _agents(goals):
        texts = [
            str(agent.get("task", "")),
            str(agent.get("prompt", {}).get("body", "")),
            *(str(item) for item in goal.get("acceptance", [])),
        ]
        for text in texts:
            for clause in re.split(r"[。；\n]", text):
                if (
                    (
                        "跨平台" in clause
                        or sum(bool(pattern.search(clause)) for pattern in platform_signals) >= 2
                    )
                    and heat.search(clause)
                    and aggregate.search(clause)
                    and not negation.search(clause)
                ):
                    messages.append(
                        f"[规则16] {goal.get('goal_id')}/{agent.get('agent_id')} "
                        f"要求跨平台聚合原始热度：{clause.strip()}"
                    )
                    break
            else:
                continue
            break
    return messages


def _rule_17(goals: list[dict[str, Any]]) -> list[str]:
    """deliverable 与其确定性归属 agent 的 shape 必须一致。"""
    messages: list[str] = []
    for goal in goals:
        deliverable = goal.get("deliverable", {})
        path = str(deliverable.get("path", ""))
        expected_shape = deliverable.get("shape")
        for agent in goal.get("agents", []):
            output = agent.get("output", {})
            if str(output.get("path", "")) != path:
                continue
            if output.get("shape") != expected_shape:
                messages.append(
                    f"[规则17] {goal.get('goal_id')}/{agent.get('agent_id')} "
                    f"output.path 归属 deliverable，但 shape 不一致："
                    f"deliverable.shape={expected_shape!r}，"
                    f"output.shape={output.get('shape')!r}"
                )
    return messages


def _rule_18(goals: list[dict[str, Any]]) -> list[str]:
    """无采集能力的下游 deliverable 不得声明硬最小条数校验。"""
    messages: list[str] = []
    minimum_validators = {
        "json_array_min_items", "json_array_between", "list_items_min",
        "table_rows_min", "table_rows_between",
    }
    for goal in goals:
        if not goal.get("depends_on"):
            continue
        can_collect = any(
            agent.get("capability", {}).get("sources")
            or agent.get("capability", {}).get("network") not in ("none", "", None)
            for agent in goal.get("agents", [])
        )
        if can_collect:
            continue
        deliverable_path = str(goal.get("deliverable", {}).get("path", ""))
        for agent in goal.get("agents", []):
            output = agent.get("output", {})
            if str(output.get("path", "")) != deliverable_path:
                continue
            validators = {
                str(item).partition(":")[0]
                for item in output.get("validators", [])
            }
            hard_minimums = sorted(validators & minimum_validators)
            if hard_minimums:
                messages.append(
                    f"[规则18] {goal.get('goal_id')}/{agent.get('agent_id')} "
                    f"无采集能力且依赖上游，deliverable 硬最小条数"
                    f"校验无法由本 goal 保证：{hard_minimums}"
                )
    return messages


def _rule_19(goals: list[dict[str, Any]]) -> list[str]:
    """整计划产物路径唯一；只豁免本 goal 最终 agent 对齐 deliverable。"""

    owners: dict[str, list[tuple[str, str]]] = {}
    for goal in goals:
        goal_id = str(goal.get("goal_id", ""))
        deliverable_path = str(goal.get("deliverable", {}).get("path", "")).strip()
        if deliverable_path:
            normalized = str(PurePosixPath(deliverable_path.replace("\\", "/")))
            owners.setdefault(normalized, []).append((goal_id, "deliverable"))
        agents = list(goal.get("agents", []))
        # §RATE-1 货 2：评级章可能排在交付物章后面，「最终 agent」按**最后一个
        # 非评级章**算，否则交付物归属会被判成路径冲突。
        final_index = max(
            (
                index for index, agent in enumerate(agents)
                if not _rated_chapter(goal, agent)
            ),
            default=len(agents) - 1,
        )
        for index, agent in enumerate(agents):
            path = str(agent.get("output", {}).get("path", "")).strip()
            if not path:
                continue
            normalized = str(PurePosixPath(path.replace("\\", "/")))
            role = (
                "final-agent"
                if index == final_index
                else f"agent:{agent.get('agent_id')}"
            )
            owners.setdefault(normalized, []).append((goal_id, role))

    messages: list[str] = []
    for path, path_owners in owners.items():
        allowed = (
            len(path_owners) == 2
            and path_owners[0][0] == path_owners[1][0]
            and {item[1] for item in path_owners} == {"deliverable", "final-agent"}
        )
        if len(path_owners) > 1 and not allowed:
            messages.append(
                f"[规则19] 整计划产物路径冲突：{path}；owners={path_owners}"
            )
    return messages


def _rule_20(goals: list[dict[str, Any]]) -> list[str]:
    """声明引用校验或信息源章节的输出必须验证角标双向闭合。"""

    required = {"citation_marks_resolvable", "no_orphan_citation"}
    messages: list[str] = []
    for goal, agent in _agents(goals):
        specifications = [
            str(item) for item in agent.get("output", {}).get("validators", [])
        ]
        validators = {
            str(item).partition(":")[0]
            for item in specifications
        }
        citation_signal = bool(required & validators) or any(
            item.partition(":")[0] == "sections_exist"
            and "信息源" in item.partition(":")[2].split(",")
            for item in specifications
        )
        if not citation_signal:
            continue
        missing = sorted(required - validators)
        if missing:
            messages.append(
                f"[规则20] {goal.get('goal_id')}/{agent.get('agent_id')} "
                f"引用输出缺少双向角标校验器：{missing}"
            )
    return messages


def _is_collection_agent(agent: Mapping[str, Any]) -> bool:
    """只读结构化 kind/profile 判断采集职能。"""

    return (
        agent.get("agent_kind") == "data_collection"
        or agent.get("capability", {}).get("profile") == "web-collector"
    )


def _collection_reuse_violations(
    goals: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """只按结构化采集能力、实体与产物路径识别同源同实体重采。"""

    first_by_key: dict[tuple[str, str], dict[str, str]] = {}
    violations: list[dict[str, str]] = []
    for goal in goals:
        goal_id = str(goal.get("goal_id", ""))
        for agent in goal.get("agents", []):
            if not _is_collection_agent(agent):
                continue
            sources = agent.get("capability", {}).get("sources", [])
            entity = str(agent.get("entity") or "").strip()
            output_path = str(agent.get("output", {}).get("path", "")).strip()
            if not isinstance(sources, list) or not entity or not output_path:
                continue
            current = {
                "goal_id": goal_id,
                "agent_id": str(agent.get("agent_id", "")),
                "agent_kind": "data_collection",
                "entity": entity,
                "output_path": output_path,
            }
            for source in sources:
                source_id = str(source).strip()
                if not source_id:
                    continue
                key = (source_id, entity)
                first = first_by_key.get(key)
                if first is None:
                    first_by_key[key] = current
                else:
                    violations.append({
                        "source_id": source_id,
                        "entity": entity,
                        "first_goal_id": first["goal_id"],
                        "first_agent_id": first["agent_id"],
                        "first_output_path": first["output_path"],
                        "goal_id": goal_id,
                        "agent_id": current["agent_id"],
                        "agent_kind": current["agent_kind"],
                        "output_path": output_path,
                    })
    return violations


def duplicate_collection_goal_ids(
    plan: Plan | Mapping[str, Any],
) -> set[str]:
    """返回应重生成的后出现违规段，不把首次采集段算作 offender。"""

    raw = _data(plan)
    return {
        item["goal_id"]
        for item in _collection_reuse_violations(list(raw.get("goals", [])))
    }


def _rule_21(goals: list[dict[str, Any]]) -> list[str]:
    messages: list[str] = []
    for item in _collection_reuse_violations(goals):
        messages.append(
            f"[规则21] {item['goal_id']}/{item['agent_id']} "
            f"(agent_kind={item['agent_kind']}) 重复完整采集 "
            f"source_id={item['source_id']}、entity={item['entity']}；首次采集="
            f"{item['first_goal_id']}/{item['first_agent_id']}，"
            f"output.path={item['first_output_path']}；本次 output.path="
            f"{item['output_path']}。请删除重复采集 agent，改为 inputs 引用上游产物 "
            f"{item['first_output_path']}"
        )
    return messages


def _rule_22(goals: list[dict[str, Any]]) -> list[str]:
    """消费上游的章必须以结构化 inputs 覆盖所消费产物。"""

    collections: list[dict[str, str]] = []
    agent_outputs: dict[str, str] = {}
    consumers: list[tuple[str, dict[str, Any], dict[str, Any], list[dict[str, str]]]] = []
    for goal in goals:
        goal_id = str(goal.get("goal_id", ""))
        for agent in goal.get("agents", []):
            agent_id = str(agent.get("agent_id", ""))
            output_path = str(agent.get("output", {}).get("path", "")).strip()
            if output_path:
                agent_outputs[agent_id] = output_path
            chapter = agent.get("chapter")
            if (
                isinstance(chapter, dict)
                and chapter.get("chapter_type") == "collection"
            ):
                path = str(
                    chapter.get("closing", {}).get("output", {}).get("path", "")
                )
                if path:
                    collections.append({
                        "location": f"{goal_id}/{chapter.get('chapter_id')}",
                        "path": path,
                    })
    for goal in goals:
        goal_id = str(goal.get("goal_id", ""))
        for agent in goal.get("agents", []):
            agent_id = str(agent.get("agent_id", ""))
            chapter = agent.get("chapter")
            if not isinstance(chapter, dict):
                continue
            chapter_type = chapter.get("chapter_type")
            expected = []
            for dependency in agent.get("depends_on", []):
                path = agent_outputs.get(str(dependency))
                if path:
                    expected.append({
                        "location": f"{goal_id}/{dependency}",
                        "path": path,
                    })
            for item in agent.get("inputs", []):
                if isinstance(item, dict) and item.get("artifact"):
                    expected.append({
                        "location": f"{goal_id}/{agent_id}.inputs",
                        "path": str(item["artifact"]),
                    })
            if chapter_type in {"comparison", "cross_validation"}:
                expected.extend(collections)
            if chapter_type != "collection" and expected:
                unique: dict[str, dict[str, str]] = {}
                for item in expected:
                    unique.setdefault(item["path"], item)
                consumers.append((goal_id, agent, chapter, list(unique.values())))
    messages: list[str] = []
    for goal_id, agent, chapter, expected in consumers:
        inputs = chapter.get("opening", {}).get("inputs", [])
        paths = {
            str(item.get("path"))
            for item in inputs
            if isinstance(item, dict) and item.get("path")
        }
        missing = [item for item in expected if item["path"] not in paths]
        if missing:
            detail = "、".join(
                f"{item['location']} output.path={item['path']}" for item in missing
            )
            messages.append(
                f"[规则22] {goal_id}/{chapter.get('chapter_id')} "
                f"({agent.get('agent_id')}) inputs 未覆盖全卷采集章：{detail}。"
                "请把以上 output.path 逐条加入 chapter.opening.inputs"
            )
    return messages


def _rule_23(raw: Mapping[str, Any], goals: list[dict[str, Any]]) -> list[str]:
    profile = str(raw.get("market_profile", ""))
    justification = str(raw.get("market_profile_justification", "")).strip()
    if profile not in _SOURCE_MARKET_PROFILES or not justification:
        return [
            "[规则23] plan.market_profile 必须取 cn_product/global_product，"
            "且 market_profile_justification 不能为空"
        ]
    applicable = set(_SOURCE_MARKET_PROFILES[profile])
    messages: list[str] = []
    for goal, agent in _agents(goals):
        chapter = agent.get("chapter")
        chapter_is_collection = (
            isinstance(chapter, dict)
            and chapter.get("chapter_type") == "collection"
        )
        if not _is_collection_agent(agent) and not chapter_is_collection:
            continue
        for source in agent.get("capability", {}).get("sources", []):
            source_id = str(source)
            if source_id not in applicable:
                messages.append(
                    f"[规则23] {goal.get('goal_id')}/{agent.get('agent_id')} "
                    f"采集章 source_id={source_id} 不适用于题目市场属性 "
                    f"{profile}；可用源={','.join(sorted(applicable))}"
                )
    return messages


def _rule_25(raw: Mapping[str, Any], goals: list[dict[str, Any]]) -> list[str]:
    """骨架研究实体必须被至少一个采集 agent 的结构化 entity 覆盖。"""

    subjects = {
        str(item).strip()
        for item in raw.get("subjects", [])
        if isinstance(item, str) and item.strip()
    }
    if not subjects:
        return []
    collected = {
        str(agent.get("entity")).strip()
        for _, agent in _agents(goals)
        if _is_collection_agent(agent)
        and isinstance(agent.get("entity"), str)
        and str(agent.get("entity")).strip()
    }
    anchor = str(goals[-1].get("goal_id", "goal-1")) if goals else "goal-1"
    return [
        f"[规则25] {anchor}/subjects 缺少实体采集 agent：{entity}；"
        "请在本 goal 补充该实体的采集 agent，或在其他 goal 分摊该实体"
        for entity in sorted(subjects - collected)
    ]


def _rule_26(goals: list[dict[str, Any]]) -> list[str]:
    """非采集章只可声明其 inputs 结构化可达的采集实体。"""

    chapters_by_output: dict[str, dict[str, Any]] = {}
    records: list[dict[str, Any]] = []
    for goal, agent in _agents(goals):
        chapter = agent.get("chapter")
        if not isinstance(chapter, dict):
            continue
        closing = chapter.get("closing", {})
        opening = chapter.get("opening", {})
        output = closing.get("output", {}) if isinstance(closing, dict) else {}
        path = str(output.get("path", "")).strip() if isinstance(output, dict) else ""
        record = {
            "goal_id": str(goal.get("goal_id", "")),
            "agent_id": str(agent.get("agent_id", "")),
            "chapter_id": str(chapter.get("chapter_id", "")),
            "chapter_type": str(chapter.get("chapter_type", "")),
            "entities": {
                str(item).strip()
                for item in closing.get("entities", [])
                if isinstance(item, str) and item.strip()
            } if isinstance(closing, dict) else set(),
            "inputs": [
                str(item.get("path", "")).strip()
                for item in opening.get("inputs", [])
                if isinstance(item, dict) and str(item.get("path", "")).strip()
            ] if isinstance(opening, dict) else [],
        }
        records.append(record)
        if path:
            chapters_by_output[path] = record

    messages: list[str] = []
    for record in records:
        if record["chapter_type"] == "collection":
            continue
        reachable_entities: set[str] = set()
        pending = list(record["inputs"])
        visited: set[str] = set()
        while pending:
            path = pending.pop()
            if path in visited:
                continue
            visited.add(path)
            upstream = chapters_by_output.get(path)
            if upstream is None:
                continue
            if upstream["chapter_type"] == "collection":
                reachable_entities.update(upstream["entities"])
            else:
                pending.extend(upstream["inputs"])
        for entity in sorted(record["entities"] - reachable_entities):
            messages.append(
                f"[规则26] {record['goal_id']}/{record['chapter_id']} "
                f"({record['agent_id']}) closing.entities 超出 inputs 可达采集实体："
                f"{entity}。请为该实体补采集章，或从本章 entities 中删除"
            )
    return messages


def _rule_24(
    goals: list[dict[str, Any]], max_chapters_per_goal: int | None,
) -> list[str]:
    if max_chapters_per_goal is None:
        return []
    messages: list[str] = []
    for goal in goals:
        # §RATE-1 货 2：评级章由生成器按采集章自动排出，不占模型的章数预算
        # （否则 fast 档 4 章的 goal 一排评级章就必红）。
        count = sum(
            1 for agent in goal.get("agents", [])
            if not _rated_chapter(goal, agent)
        )
        if count > max_chapters_per_goal:
            messages.append(
                f"[规则24] {goal.get('goal_id')} 章数上限为 "
                f"{max_chapters_per_goal}，实际为 {count}；请合并本 goal 的章"
            )
    return messages



# format=json 且顶层必须为数组的验证器（validator-registry §2.2/§2.7）；
# 与节化文档信封（§2.2b）互斥，出现在节化章即拒绝保存。
_ARRAY_VALIDATORS = frozenset({
    "json_array_min_items", "json_array_between", "each_item_has",
    "field_domain_whitelist", "no_item_missing_rating",
    "rating_notes_matches_regex", "rating_notes_scores_match_columns",
})


def _rule_30(goals: list[dict[str, Any]]) -> list[str]:
    """§RATE-1 货 2：有采集章就必须有评级章，且只连它那一章（采集即评级）。

    评级要真的决定「引不引」，就必须在写作**之前**跑完；生成器自动排出，这条
    规则只负责在它没排到时把红报出来（不自动修）。
    """
    messages: list[str] = []
    for goal in goals:
        agents = list(goal.get("agents", []))
        collectors = [
            str(agent.get("agent_id", "")) for agent in agents
            if agent.get("capability", {}).get("profile") == "web-collector"
        ]
        rated = {
            _rated_chapter(goal, agent): agent
            for agent in agents if _rated_chapter(goal, agent)
        }
        for agent_id in collectors:
            rating = rated.get(agent_id)
            if rating is None:
                messages.append(
                    f"[规则30] {goal.get('goal_id')}/{agent_id} 是采集章，"
                    "但没有对应的评级章：写作前拿不到真实等级"
                )
            elif list(rating.get("depends_on", [])) != [agent_id]:
                messages.append(
                    f"[规则30] {goal.get('goal_id')}/{rating.get('agent_id')} "
                    f"只能依赖它评的那一章 {agent_id}，实际为 "
                    f"{list(rating.get('depends_on', []))}"
                )
    return messages


def _rule_27(goals: list[dict[str, Any]]) -> list[str]:
    """节化章（agents-spec §2.3.1）：shape 恒 object，禁止数组类验证器。"""
    messages: list[str] = []
    for goal, agent in _agents(goals):
        kind = agent_kind_of(
            str(agent.get("agent_id", "")),
            agent.get("capability", {}).get("profile"),
        )
        output = agent.get("output", {})
        if (
            kind not in SECTIONED_CHAPTER_KINDS
            or output.get("format") not in {"markdown", "json"}
        ):
            continue
        location = f"{goal.get('goal_id')}/{agent.get('agent_id')}"
        if output.get("shape") != "object":
            messages.append(
                f"[规则27] {location} 是节化章，output.shape 必须为 object"
                f"（信封形，agents-spec §2.3.1），实际 {output.get('shape')!r}"
            )
        hits = sorted({
            str(item).partition(":")[0]
            for item in output.get("validators", [])
            if str(item).partition(":")[0] in _ARRAY_VALIDATORS
        })
        if hits:
            messages.append(
                f"[规则27] {location} 是节化章，validators 不得含数组类验证器："
                f"{'、'.join(hits)}；节化 json 产物按信封形使用 "
                "sectioned_document_valid（agents-spec §2.3.1）"
            )
    return messages


def _identifier_signals(values: Iterable[Any]) -> set[str]:
    """只提取字段标识符，不识别或放行任何自然语言措辞。"""

    signals: set[str] = set()
    for value in values:
        signals.update(re.findall(r"[A-Za-z][A-Za-z0-9_]*", str(value)))
    return signals


def _validator_signals(specifications: Iterable[Any]) -> set[str]:
    signals: set[str] = set()
    for specification in specifications:
        name, arguments = _parse_validator_spec(str(specification))
        signals.update(_VALIDATOR_FIELDS.get(name, ()))
        if name == "each_item_has":
            signals.update(arguments)
    return signals


def _parse_validator_spec(specification: str) -> tuple[str, list[str]]:
    name, separator, raw = specification.partition(":")
    return name, raw.split(",") if separator else []


def _rule_28(goals: list[dict[str, Any]]) -> list[str]:
    """goal 验收字段族与 agent validators 的产物对象必须一致。"""

    messages: list[str] = []
    for goal, agent in _agents(goals):
        if _rated_chapter(goal, agent):
            continue
        deliverable = goal.get("deliverable", {})
        expected = _identifier_signals([
            deliverable.get("description", ""),
            *goal.get("acceptance", []),
        ])
        actual = _validator_signals(
            agent.get("output", {}).get("validators", [])
        )
        expected_findings = expected & _FINDINGS_FIELDS
        expected_ratings = expected & _RATINGS_FIELDS
        actual_findings = actual & _FINDINGS_FIELDS
        actual_ratings = actual & _RATINGS_FIELDS
        conflict = (
            len(expected_findings) >= 2 and len(actual_ratings) >= 2
            or len(expected_ratings) >= 2 and len(actual_findings) >= 2
        )
        if not conflict:
            continue
        expected_family = "findings" if expected_findings else "ratings"
        actual_family = "ratings" if actual_ratings else "findings"
        expected_fields = expected_findings or expected_ratings
        actual_fields = actual_ratings or actual_findings
        messages.append(
            f"[规则28] {goal.get('goal_id')}/{agent.get('agent_id')} 产物契约不自洽："
            f"goal deliverable/acceptance 描述 {expected_family} 字段 "
            f"{sorted(expected_fields)}，但 output.validators 校验 {actual_family} 字段 "
            f"{sorted(actual_fields)}。请让验收文本、产物结构与 validators 描述同一对象"
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
    platforms: dict[tuple[str, str], list[str]] = {}
    for _, agent in _agents(goals):
        entity = str(agent.get("entity") or "").strip()
        if not entity:
            continue
        for source in agent.get("capability", {}).get("sources", []):
            platforms.setdefault(
                (str(source), entity), []
            ).append(str(agent.get("agent_id")))
    for (source, entity), agent_ids in platforms.items():
        if len(agent_ids) > 1:
            messages.append(
                f"[警告5] 平台 {source} 的实体 {entity} 被多个 agent 重复采集："
                f"{agent_ids}"
            )
    for goal in goals:
        count = len(goal.get("agents", []))
        if count > 5:
            messages.append(f"[警告6] {goal.get('goal_id')} 下有 {count} 个 agent，超过 5 个")
    preset = re.compile(r"必须包含\s*([^，。；\n]{1,40}?)\s*条目")
    for goal in goals:
        for index, acceptance in enumerate(goal.get("acceptance", [])):
            matched = preset.search(str(acceptance))
            if matched:
                messages.append(
                    f"[警告7] {goal.get('goal_id')}.acceptance[{index}] 预设了"
                    f"具体实体条目“{matched.group(1).strip()}”，真实数据不足时不应阻断"
                )
    return messages


def lint(
    plan: Plan | Mapping[str, Any], *, for_approval: bool = False,
    max_chapters_per_goal: int | None = None,
) -> dict[str, list[str]]:
    """按 §10 返回问题；规则 12/29 是批准闸门，普通保存不阻断。"""
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
    if for_approval:
        errors.extend(_rule_12(raw))
        errors.extend(_rule_29(raw))
    errors.extend(_rule_13(goals))
    errors.extend(_rule_14(goals))
    errors.extend(_rule_15(goals))
    errors.extend(_rule_16(goals))
    errors.extend(_rule_17(goals))
    errors.extend(_rule_18(goals))
    errors.extend(_rule_19(goals))
    errors.extend(_rule_20(goals))
    errors.extend(_rule_21(goals))
    errors.extend(_rule_22(goals))
    errors.extend(_rule_23(raw, goals))
    errors.extend(_rule_24(goals, max_chapters_per_goal))
    errors.extend(_rule_25(raw, goals))
    errors.extend(_rule_26(goals))
    errors.extend(_rule_27(goals))
    errors.extend(_rule_28(goals))
    errors.extend(_rule_30(goals))
    return {"errors": errors, "warnings": _warnings(goals)}
