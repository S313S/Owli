"""§PLAN-1 货 2：lint 之前的确定性修正。

原则：lint 报的每条规则先问「修法是不是确定性的」，是就代码修并留痕，
不是才回传给模型。这里只放 Plan 对象层面的修正（规则 26、规则 31 反向）；
原始段层面的修正（规则 17 shape 对齐）在 generate._align_deliverable_shape。
每条修正说明以 [修正NN] 开头，调用方负责发事件，防止静默缩范围。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from app.plan.model import Agent, Goal, Plan


def normalize_plan(
    plan: Plan,
    *,
    collection_plan: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    per_goal_capacity: int | None = None,
) -> list[str]:
    """原地修正，返回修正说明；没动任何东西时返回空列表。

    `collection_plan` 是 §PLAN-1 的采集分配表（goal_id → [{entity, source_id,
    collector_name}]），**只在生成期传入**；不传就退回老行为，一张卡都不删
    （批准闸门等别的调用方走的正是这条路）。`per_goal_capacity` 是该档位每 goal
    的采集位上限，`None` = 不压（standard 档章数无上限）。

    ⚠️ **顺序是硬要求：删卡必须排在规则 26 之前。** 反过来的话，规则 26 会先给
    报告章补一条指向「马上要被删掉那张卡」的 inputs，删完就成了悬空引用。
    2026-09-12 那一轮的现场正是如此：三条规则 26 的修正事件里，DeepSeek 补的
    就是表外那张卡的产物——表外卡不但没被删，下游还把它当合法卡接纳了。
    """

    return _repair_rule_31_reverse(plan, collection_plan, per_goal_capacity) \
        + _repair_rule_26(plan)


def _is_collector(agent: Agent) -> bool:
    return agent.capability.get("profile") == "web-collector"


def _slot_key(agent: Agent) -> tuple[str, str]:
    """一张采集卡的身份：(source_id, entity)。

    `capability.sources` 在采集卡上恒一个元素（`generate._capability` 写死
    `"sources": [source_id]`），所以取第 0 个就是全部，不会漏掉第二个源。
    """
    sources = agent.capability.get("sources") or [""]
    return str(sources[0]), str(agent.entity or "").strip()


def _repair_rule_31_reverse(
    plan: Plan,
    collection_plan: Mapping[str, Sequence[Mapping[str, Any]]] | None,
    capacity: int | None,
) -> list[str]:
    """分配表变闸：表外的采集卡删掉，每 goal 采集位不超档位容量。

    **这是 lint 规则 31 的反向那一半。** 规则 31 查的是「表⊆计划」——表里每一对
    都必须落成卡（允许挪 goal，不许丢）。它**没查反过来**（计划⊆表），所以引擎
    多起草的卡一条规则都不违反，静默通过、真去采数。微博这类读池薄源命中口径宽，
    采回来的一多半是别家旧批次的语料，却被当成这个实体的证据入库。

    口径（用户 09-12 经调度代拍）：
    - **删卡按全局 `(source_id, entity)` 匹配**，不按 goal 内比对——规则 31 明写
      允许挪 goal，按 goal 比会把合法挪过去的卡误删。
    - **容量按每 goal 的采集卡数**，⛔ 不去收紧 `max_chapters_per_goal`：章数闸
      连非采集章一起数，收紧它会误伤报告章。实测 fast 那一格 4 章（3 采集 + 1 撰写）
      章数闸 4≤4 刚好过，采集位 3>2 却没人守——病不在章数上。

    空表（`None` 或 `{}`）一律当「没给」处理，**不是**「表里一张都没有」——
    按后者理解会把整个计划的采集卡删光。
    """

    if not collection_plan:
        return []
    table = {
        (str(slot.get("source_id", "")), str(slot.get("entity", "")).strip())
        for slots in collection_plan.values() for slot in slots
    }
    notes: list[str] = []
    dropped: set[str] = set()
    for goal in plan.goals:
        notes.extend(_gate_goal(goal, table, capacity, dropped))
    if dropped:
        notes.extend(_drop_orphans(plan, dropped))
    return notes


def _drop_orphans(plan: Plan, dropped: set[str]) -> list[str]:
    """删卡的连带收口：没了上游的章跟着走，剩下的把悬空的 depends_on 摘掉。

    **为什么非有这一步不可**：§RATE-1 货 2 给每张采集卡各挂一个只评它的评级章
    （`depends_on` 就那一张卡）。删掉卡而不管它，评级章就成了孤儿，lint 规则 2
    当场报「depends_on 引用了不存在的 id」，计划连生都生不出来——接生产链路时
    27 条既有用例就是这么红的。

    判「孤儿」不看 kind 看依赖：`depends_on` 非空、且**每一个**上游都已被删，
    就是真的没有输入了。报告章依赖的是全部评级章，删掉其中一个还剩别的，
    不会被这一条误伤。级联到不动为止（卡 → 评级章 → 再往下）。
    """

    notes: list[str] = []
    changed = True
    while changed:
        changed = False
        for goal in plan.goals:
            kept: list[Agent] = []
            for agent in goal.agents:
                deps = list(agent.depends_on)
                if deps and all(dep in dropped for dep in deps):
                    dropped.add(agent.agent_id)
                    notes.append(
                        f"[修正31] {goal.goal_id}/{agent.agent_id} 的上游采集卡已删、"
                        "这一章再没有输入，一并删除"
                    )
                    changed = True
                    continue
                kept.append(agent)
            goal.agents = kept
    # 剩下的章里还指着被删 id 的，把那几项摘掉。合并成一条说明——一章一条会把
    # 事件刷屏，而这一步本身是上面那几条删除的必然后果，不是独立决定。
    rewired: list[str] = []
    for goal in plan.goals:
        for agent in goal.agents:
            remaining = [dep for dep in agent.depends_on if dep not in dropped]
            if remaining != list(agent.depends_on):
                agent.depends_on = remaining
                rewired.append(f"{goal.goal_id}/{agent.agent_id}")
    if rewired:
        notes.append(
            f"[修正31] 以下章的 depends_on 摘掉了已删卡：{'、'.join(rewired)}"
        )
    return notes


def _gate_goal(goal: Goal, table: set[tuple[str, str]], capacity: int | None,
               dropped: set[str]) -> list[str]:
    """一个 goal 的三道口：表外删、重复去、超容量截。非采集卡一律原样留着。

    被删卡的 agent_id 记进 `dropped`，交给 `_drop_orphans` 做连带收口。
    """

    notes: list[str] = []
    kept: list[Agent] = []
    seen: set[tuple[str, str]] = set()
    collected = 0
    for agent in goal.agents:
        if not _is_collector(agent):
            kept.append(agent)
            continue
        source, entity = key = _slot_key(agent)
        where = f"{goal.goal_id}/{agent.agent_id}"
        if not entity:
            # 不带实体的通用采集卡（「API 数据抓取」这类职能名，`_classify` 判不出
            # 实体所以 entity 是空串）**不归这道闸管**：分配表的键是「哪个实体用哪个源」，
            # 一张没有实体的卡根本不构成这样的键，谈不上在不在表里。
            # ⛔ 也不计进容量——否则它会把表里真排了位的卡挤掉，规则 31 当场报
            # 「分配的采集对未落实」，计划连生都生不出来。
            kept.append(agent)
            continue
        if key not in table:
            notes.append(
                f"[修正31] {where} 采集卡「{source}·{entity}」不在分配表里，已删除"
                "（分配表是闸不是建议；表外源采回来的多是别家批次的语料）"
            )
            dropped.add(agent.agent_id)
            continue
        if key in seen:
            notes.append(
                f"[修正31] {where} 采集卡「{source}·{entity}」与本 goal 前一张重复，已删除"
            )
            dropped.add(agent.agent_id)
            continue
        if capacity is not None and collected >= capacity:
            notes.append(
                f"[修正31] {where} 采集卡「{source}·{entity}」超出本档位每 goal "
                f"{capacity} 个采集位的容量，已删除"
            )
            dropped.add(agent.agent_id)
            continue
        seen.add(key)
        collected += 1
        kept.append(agent)
    goal.agents = kept
    return notes


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
