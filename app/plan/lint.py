"""计划树保存与批准前的 20 条阻断校验和 7 类质量提示。"""

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


def _rule_14(goals: list[dict[str, Any]]) -> list[str]:
    """数组校验器不得与同一 agent 的对象型任务/goal 验收互相打架。"""
    messages: list[str] = []
    object_contract = re.compile(
        r"JSON\s*object|顶层[^。；\n]{0,40}(?:含|包含)[^。；\n]{0,40}(?:键|字段)",
        re.IGNORECASE,
    )
    # 显式数组声明短路：「顶层为数组…每条（对象）含 permalink…字段」描述的是
    # 数组元素结构，与数组校验器一致，不是 object 契约。措辞彩票实锤：
    # 6b 实跑（2026-08-21 r-9eb208e803ee）该误报模型无解，goal-2 段预算被
    # 钉死耗尽——与规则 17/18 同构（M3 回填开口 #6）。
    array_contract = re.compile(
        r"顶层[^。；\n]{0,20}数组|JSON\s*arrays?", re.IGNORECASE
    )
    for goal, agent in _agents(goals):
        validators = agent.get("output", {}).get("validators", [])
        if not any(
            str(item).partition(":")[0] == "json_array_min_items"
            for item in validators
        ):
            continue
        # 契约点名了具体文件时只约束产出该文件的 agent：goal 验收描述
        # 最终交付对象契约，不该套在同 goal 的数组中间产物 agent 头上
        # （6b 实跑 2026-08-21 r-49a84c8c299e：验收点名 deliverable 文件，
        # 四个采集 agent 全被误拦，模型无解耗尽预算）。agent 自身 task
        # 文本不做文件归属豁免——它描述的就是本 agent 的产物。
        agent_basename = PurePosixPath(
            str(agent.get("output", {}).get("path", "")).replace("\\", "/")
        ).name

        def _applies(text: str, *, own_text: bool) -> bool:
            if not object_contract.search(text) or array_contract.search(text):
                return False
            if own_text:
                return True
            named = re.findall(r"[\w\-.]+\.json", text, re.IGNORECASE)
            return not named or agent_basename in named

        texts = [
            (str(item), False) for item in goal.get("acceptance", [])
        ] + [(str(agent.get("task", "")), True)]
        conflict = next(
            (
                text
                for text, own_text in texts
                if _applies(text, own_text=own_text)
            ),
            None,
        )
        if conflict:
            messages.append(
                f"[规则14] {goal.get('goal_id')}/{agent.get('agent_id')} 使用 "
                f"json_array_min_items，但任务或验收要求 JSON object：{conflict}"
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
    """goal 级 JSON 文件契约不得与章节校验器共存而不点名文件。

    真实样本 r-4878be30ff8c：验收写「文件为合法 JSON，顶层恰含 … 三个字段」
    却未指明是哪个文件，同 goal 的 data-cleaning 输出 format=markdown +
    sections_exist:结论 —— agent 按验收写纯 JSON，章节校验器必失败。

    点名与否按 goal 级判定，不逐行索要文件名。真实样本 r-29586a489b34：
    回喂后模型已在首行点名「文件 pros-cons.json 存在且顶层为 JSON object」，
    次行「顶层 object 含 competitors 字段」描述同一文件的结构却因行内无
    文件名被逐行误拒，三次重试全灭——契约有归属即视为已点名。
    """
    messages: list[str] = []
    json_contract = re.compile(
        r"(?:文件|产物)[^。；\n]{0,10}(?:为|是)[^。；\n]{0,10}合法\s*JSON"
        r"|JSON\s*object"
        r"|顶层[^。；\n]{0,40}(?:含|包含)[^。；\n]{0,40}(?:键|字段)",
        re.IGNORECASE,
    )
    names_json_file = re.compile(r"[\w./-]+\.json\b", re.IGNORECASE)
    for goal in goals:
        section_agents = [
            agent for agent in goal.get("agents", [])
            if any(
                str(item).partition(":")[0] == "sections_exist"
                for item in agent.get("output", {}).get("validators", [])
            )
        ]
        if not section_agents:
            continue
        goal_names_json = any(
            names_json_file.search(str(item))
            for item in goal.get("acceptance", [])
        )
        if goal_names_json:
            continue
        for acceptance in goal.get("acceptance", []):
            text = str(acceptance)
            if json_contract.search(text):
                agent_ids = [str(agent.get("agent_id")) for agent in section_agents]
                messages.append(
                    f"[规则17] {goal.get('goal_id')} 验收要求 JSON 文件契约但未点名"
                    f"文件，而 agent={agent_ids} 的校验器含 sections_exist（章节契约）"
                    f"：两者必有一方无法满足。请在验收里写明 .json 文件名，或改"
                    f"该 agent 的产物格式与校验器：{text}"
                )
                break
    return messages


def _rule_18(goals: list[dict[str, Any]]) -> list[str]:
    """无采集能力的下游 goal，验收不得按实体写死最小条数。

    真实样本 r-b1b75c7000ab goal-3：验收要求「报告为每个竞品至少列出
    2 条来自不同 author 的独立证据」，上游 goal-2 契约只保证 distinct
    competitor_name ≥3、对每竞品条数零承诺；实际 3 个竞品各只剩 1 条，
    分析 agent 被禁止新抓取，诚实返回 partial 也无济于事，重试与换引擎
    耗尽后整条调研 failed。数据规模断言只能落在采集 goal 或写成条件式。
    """
    messages: list[str] = []
    per_entity_minimum = re.compile(
        r"(?:每一?[个条组项名位家款]?|各)[^。；\n]{0,20}?"
        r"(?:至少|不少于|不得少于|≥|>=)[^。；\n]{0,8}?\d+\s*[条个组篇项]"
    )
    # 条件式识别不做字面单选：真实样本 r-f14050856779 三轮回灌后模型写
    # 「若上游数据不足以支撑则标注孤证…即算达标」，完全符合本规则给的
    # 出路，却因白名单只认「不足时」被拒到重试耗尽。
    negation = re.compile(
        r"禁止|不得|不要求|无需|不足|缺口|孤证|即算达标|即视为|视为达标"
    )
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
        for index, acceptance in enumerate(goal.get("acceptance", [])):
            text = str(acceptance)
            matched = per_entity_minimum.search(text)
            if matched and not negation.search(text):
                messages.append(
                    f"[规则18] {goal.get('goal_id')}.acceptance[{index}] 按实体"
                    f"写死最小条数「{matched.group(0).strip()}」，但该 goal 无"
                    f"采集能力且依赖上游数据，上游契约不保证每实体条数，数据"
                    f"不足时永不可满足。请改为条件式（数据不足时在产物中明确"
                    f"标注孤证或缺口即算达标）：{text}"
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
        for index, agent in enumerate(agents):
            path = str(agent.get("output", {}).get("path", "")).strip()
            if not path:
                continue
            normalized = str(PurePosixPath(path.replace("\\", "/")))
            role = (
                "final-agent"
                if index == len(agents) - 1
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
    plan: Plan | Mapping[str, Any], *, for_approval: bool = False
) -> dict[str, list[str]]:
    """按 §10 返回问题；规则 12 是批准闸门，普通保存不阻断。"""
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
    errors.extend(_rule_13(goals))
    errors.extend(_rule_14(goals))
    errors.extend(_rule_15(goals))
    errors.extend(_rule_16(goals))
    errors.extend(_rule_17(goals))
    errors.extend(_rule_18(goals))
    errors.extend(_rule_19(goals))
    errors.extend(_rule_20(goals))
    return {"errors": errors, "warnings": _warnings(goals)}
