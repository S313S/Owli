"""§PLAN-1 货 2：lint 之前的确定性修正。

原则：lint 报的每条规则先问「修法是不是确定性的」，是就代码修并留痕，
不是才回传给模型。这里只放 Plan 对象层面的修正（规则 26）；原始段层面的
修正（规则 17 shape 对齐）在 generate._align_deliverable_shape。
每条修正说明以 [修正NN] 开头，调用方负责发事件，防止静默缩范围。
"""

from __future__ import annotations

from typing import Any

from app.plan.model import Plan


def normalize_plan(plan: Plan) -> list[str]:
    """原地修正，返回修正说明；没动任何东西时返回空列表。"""

    return _repair_rule_26(plan)


def _ancestors(plan: Plan) -> dict[str, set[str]]:
    deps = {goal.goal_id: list(goal.depends_on) for goal in plan.goals}
    result: dict[str, set[str]] = {}
    for goal_id in deps:
        seen: set[str] = set()
        pending = list(deps[goal_id])
        while pending:
            item = pending.pop()
            if item in seen:
                continue
            seen.add(item)
            pending.extend(deps.get(item, []))
        result[goal_id] = seen
    return result


def _reachable_entities(chapter: dict[str, Any], by_output: dict[str, dict[str, Any]]) -> set[str]:
    """与 lint._rule_26 同口径：沿 opening.inputs 回溯到采集章的 closing.entities。"""

    reachable: set[str] = set()
    pending = [
        str(item.get("path", "")) for item in chapter.get("opening", {}).get("inputs", [])
        if isinstance(item, dict)
    ]
    visited: set[str] = set()
    while pending:
        path = pending.pop()
        if not path or path in visited:
            continue
        visited.add(path)
        upstream = by_output.get(path)
        if upstream is None:
            continue
        if upstream.get("chapter_type") == "collection":
            reachable.update(
                str(e).strip() for e in upstream.get("closing", {}).get("entities", [])
            )
        else:
            pending.extend(
                str(item.get("path", ""))
                for item in upstream.get("opening", {}).get("inputs", [])
                if isinstance(item, dict)
            )
    return reachable


def _repair_rule_26(plan: Plan) -> list[str]:
    """非采集章 closing.entities 里够不着的实体：能补 inputs 就补，否则删。

    补 inputs 只认「同 goal 或本 goal 传递依赖的上游 goal」里的采集章——
    执行顺序保证那份文件到时候一定在；别的 goal 的产物不敢引。
    删到只剩空列表时不删，留给 lint 让章级重试去改。
    """

    notes: list[str] = []
    by_output: dict[str, dict[str, Any]] = {}
    collectors: dict[str, list[tuple[str, str]]] = {}
    for goal in plan.goals:
        for agent in goal.agents:
            chapter = agent.chapter
            if isinstance(chapter, dict):
                path = str(chapter.get("closing", {}).get("output", {}).get("path", ""))
                if path:
                    by_output[path] = chapter
            if agent.capability.get("profile") == "web-collector" and agent.entity:
                collectors.setdefault(agent.entity.strip(), []).append(
                    (goal.goal_id, str(agent.output.get("path", "")))
                )
    ancestors = _ancestors(plan)
    for goal in plan.goals:
        for agent in goal.agents:
            chapter = agent.chapter
            if not isinstance(chapter, dict) or chapter.get("chapter_type") == "collection":
                continue
            closing = chapter.setdefault("closing", {})
            entities = [str(e).strip() for e in closing.get("entities", []) if str(e).strip()]
            if not entities:
                continue
            reachable = _reachable_entities(chapter, by_output)
            location = f"{goal.goal_id}/{chapter.get('chapter_id')} ({agent.agent_id})"
            kept = list(entities)
            for entity in entities:
                if entity in reachable:
                    continue
                candidates = [
                    path for owner, path in collectors.get(entity, [])
                    if path and (owner == goal.goal_id or owner in ancestors[goal.goal_id])
                ]
                if candidates:
                    inputs = chapter.setdefault("opening", {}).setdefault("inputs", [])
                    inputs.append({"path": candidates[0]})
                    by_output_entities = by_output.get(candidates[0], {}).get("closing", {}).get("entities", [])
                    reachable.update(str(e).strip() for e in by_output_entities)
                    notes.append(f"[修正26] {location} 实体 {entity} 不可达，补 inputs：{candidates[0]}")
                elif len(kept) > 1:
                    kept.remove(entity)
                    notes.append(f"[修正26] {location} 实体 {entity} 全计划无可达采集章，已从 closing.entities 删除")
            if kept != entities:
                closing["entities"] = kept
    return notes
