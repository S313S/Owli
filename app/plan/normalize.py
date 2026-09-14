"""§PLAN-1 货 2：lint 之前的确定性修正。

原则：lint 报的每条规则先问「修法是不是确定性的」，是就代码修并留痕，
不是才回传给模型。这里只放 Plan 对象层面的修正（规则 26、规则 31 反向）；
原始段层面的修正（规则 17 shape 对齐）在 generate._align_deliverable_shape。
每条修正说明以 [修正NN] 开头，调用方负责发事件，防止静默缩范围。
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
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
    空 goal 移除（§D-065）紧跟删卡、同样排在规则 26 之前，且不看 `collection_plan`：
    一个 0 章 goal 不论怎么来的，留在计划里执行期都会冻结。
    """

    return _repair_rule_31_reverse(plan, collection_plan, per_goal_capacity) \
        + _repair_empty_goals(plan) \
        + _repair_acceptance(plan) \
        + _repair_rule_26(plan)


_EMPTY_GOAL_NOTE = re.compile(r"^\[修正31\] (?P<goal_id>\S+)「(?P<title>.*)」一章不剩，已移出计划")


def empty_goal_removal(note: str) -> dict[str, str] | None:
    """从一条修正说明认出「空 goal 移出计划」，给事件 raw 与报告附注用；不是就 None。

    说明文案与这条正则同在本模块，改一边必须改另一边（用例锁着）。
    """

    match = _EMPTY_GOAL_NOTE.match(note)
    if match is None:
        return None
    return {"goal_id": match["goal_id"], "title": match["title"]}


def _repair_empty_goals(plan: Plan) -> list[str]:
    """0 章 goal 移出计划，下游改接它的上游、摘掉指向它的输入（§D-065）。

    **病根**：删卡闸（`_gate_goal` + `_drop_orphans`）能把一个 goal 的章删光，
    goal 本身却留着。执行器等它收尾等不到，依赖它的 goal 永不起跑、研究永不
    completed、报告永不组装，零报错。WX-1 小跑 r-wx1-0913-1355 的 goal-4「口碑」
    唯一一张卡是表外卡，删完 0 章，goal-6 综合研判还依赖它。

    - `goal.depends_on`：被删 goal 换成它自己的上游（递归穿过连续的空 goal），保序去重。
      下游的执行顺序约束不丢，只是跳过了一个不会产出任何东西的节点。
    - `agent.inputs`：摘掉 `from_goal` 是被删 goal 的项——否则 lint 规则 3
      （from_goal 必须在祖先链里）当场报错；
    - `chapter.opening.inputs`：摘掉指向那些产物、或落在 `goals/<被删 goal>/` 下的路径。
      被删 goal 没有章，那个目录下永远不会有文件。
    - goal_id 不重编号：删除留下的 id 空洞不复用是既有口径（`model.next_goal_id`）。
    """

    empty = {goal.goal_id: goal for goal in plan.goals if not goal.agents}
    if not empty:
        return []

    def upstream(goal_id: str) -> list[str]:
        result: list[str] = []
        for dep in empty[goal_id].depends_on:
            result.extend(upstream(dep) if dep in empty else [dep])
        return result

    plan.goals = [goal for goal in plan.goals if goal.goal_id not in empty]
    notes = [
        f"[修正31] {goal_id}「{goal.title}」一章不剩，已移出计划"
        "（它的采集卡都不在分配表里被删了，连带的章跟着删光；留着执行期会一直等它）"
        for goal_id, goal in empty.items()
    ]
    rewired: list[str] = []
    stripped: list[str] = []
    for goal in plan.goals:
        deps: list[str] = []
        for dep in goal.depends_on:
            for item in (upstream(dep) if dep in empty else [dep]):
                if item not in deps:
                    deps.append(item)
        if deps != list(goal.depends_on):
            rewired.append(
                f"{goal.goal_id}（{'、'.join(goal.depends_on)} → {'、'.join(deps) or '无'}）"
            )
            goal.depends_on = deps
        for agent in goal.agents:
            if _strip_empty_goal_inputs(agent, set(empty)):
                stripped.append(f"{goal.goal_id}/{agent.agent_id}")
    if rewired:
        notes.append(f"[修正31] 以下 goal 的 depends_on 改接被移出 goal 的上游：{'、'.join(rewired)}")
    if stripped:
        notes.append(f"[修正31] 以下章摘掉了指向被移出 goal 的输入：{'、'.join(stripped)}")
    return notes


def _strip_empty_goal_inputs(agent: Agent, removed: set[str]) -> bool:
    dead_paths = {
        str(item.get("artifact", ""))
        for item in agent.inputs
        if isinstance(item, dict) and item.get("from_goal") in removed
    }
    changed = False
    kept = [
        item for item in agent.inputs
        if not (isinstance(item, dict) and item.get("from_goal") in removed)
    ]
    if len(kept) != len(agent.inputs):
        agent.inputs = kept
        changed = True
    opening = (agent.chapter or {}).get("opening")
    inputs = opening.get("inputs") if isinstance(opening, dict) else None
    if isinstance(inputs, list):
        prefixes = tuple(f"goals/{goal_id}/" for goal_id in removed)
        kept_opening = [
            item for item in inputs
            if not (
                isinstance(item, dict)
                and (
                    str(item.get("path", "")) in dead_paths
                    or str(item.get("path", "")).startswith(prefixes)
                )
            )
        ]
        if len(kept_opening) != len(inputs):
            opening["inputs"] = kept_opening
            changed = True
    return changed


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


#: §D-062：验收条里的反向约束语境。无卡实体出现在这些词所在的**子句**里，说明这条
#: 是在禁止它（「未出现 Kimi 或 DeepSeek 的任何叫法」「仅使用闭集叫法：…」），那是防串号
#: 的闸，不能摘。误判方向不对称：豁免只在「本来要摘」时启用，误豁免 = 回到现状，不会更坏。
#: §D-062-fu 补：r-50600e09f7dd 的「仅出现闭集内的实体叫法…，未越界写入「DeepSeek」「Kimi」…」
#: 第二子句里一个旧词都没有，被当成「要求写 DeepSeek」摘了。补的是「限定 / 越界 / 混入 /
#: 排除 / 避免」几族写法；⛔ 不收裸「不」「无」——「不少于 Kimi 3 条」是正向要求。
_ACCEPTANCE_NEGATION_WORDS = (
    "未出现", "不出现", "不得", "禁止", "不写", "仅使用", "只使用", "仅限",
    "不含", "不包含", "不允许", "不许", "不引用", "未引用", "闭集", "白名单",
    "仅出现", "只出现", "越界", "混入", "混进", "未写入", "未提及", "不提及",
    "排除", "避免", "严禁", "杜绝", "勿", "不重复", "不与",
)
_ACCEPTANCE_CLAUSE_SEPARATORS = frozenset("，,；;。")
#: 括号里的逗号不切子句：引擎写 `(xhs, 豆包语音输入法)` 这种组合元组，按英文逗号切会把
#: 实体切到没有否定词的半截里（r-d062fu-0913-a 现形）。
_ACCEPTANCE_BRACKETS = {"(": ")", "（": "）", "[": "]", "【": "】", "「": "」"}
#: 产物路径：全形 `goals/goal-N/<file>`，或引擎常写的短形 `goal-N/<file>`（前面不接
#: 路径字符，免得把全形里的那段再抓一遍）。§D-062-fu 起短形也判，见 `_acceptance_paths`。
_ACCEPTANCE_PRODUCT_PATH = re.compile(
    r"(?<![A-Za-z0-9_./\-])(?:goals/)?goal-[1-9][0-9]*/[A-Za-z0-9_.\-]+")
_GOAL_ID = re.compile(r"goal-[1-9][0-9]*")
#: 一个 goal 的验收条被摘光时的兜底——规则 4 要求至少一条；这条不提任何实体。
_ACCEPTANCE_FALLBACK = (
    "交付物文件存在且通过 validators；对本 goal 没有采集卡的实体不作内容要求，"
    "数据不足以结构化缺口口径记录"
)


def _repair_acceptance(plan: Plan) -> list[str]:
    """§D-062：验收条要求写「没有采集卡的实体」或引用「已删卡的产物」——整条摘。

    **为什么要有这一步**：D-061 删掉表外卡后，规则 26 只把实体从报告章的
    `closing.entities` 摘掉，`goal.acceptance` 原样不动；而 `runtime.py` 把验收条逐字
    拼进写手提示词。goal-1 第 1 条仍写「含豆包、Kimi、DeepSeek 三个实体的证据分节」，
    写手手里没有 DeepSeek 任何数据却被要求写——要么编，要么章反复被打回。lint 规则 4
    只查「至少一条」「不含不可判定表述」，不查提到的实体有没有卡 ⇒ 规划期静默、执行期发作。

    口径（§一′，调度 09-12 代拍）：
    - **可达口径**：一条验收条「在说哪个实体」按实体全部叫法（id / canonical / zh / en /
      aliases）匹配；提到的实体 ⊄「本 goal 或其传递上游 goal 有卡的实体」⇒ **整条摘**，
      不做句内删改（句内删会产出病句）。只看本 goal 会误伤 goal-2 消费 goal-1 Kimi 语料的条。
    - **反向约束豁免**：无卡实体出现在否定/限定语境的子句里（词表见上）不摘。
    - **第三类按路径判**：验收条写死的 `goals/<goal>/<file>` 不在现存产物集合
      （所有 agent 的 output.path + 各 goal 的 deliverable.path）里 ⇒ 整条摘。实体可达
      ≠ 那份产物还在：goal-2 第 4 条三个实体都可达，引的却是被删卡的产物。
    - ⛔ 必须排在删卡闸之后（看的是删完还在的卡），规则 26 之前（与它无依赖，只是同一批留痕）。
      不动 lint 规则 4 的语义，不改引擎提示词求它自己判。

    留痕号用「[修正4]」：验收条可判定性是规则 4 的领地，一条要求写无卡实体的验收条在
    实践上就是不可判定的。摘到空列表时兜底一条不提实体的验收条（规则 4 要至少一条）。
    """

    matchers = _entity_matchers(plan)
    outputs: set[str] = set()
    cards: dict[str, set[str]] = {}
    for goal in plan.goals:
        path = str((goal.deliverable or {}).get("path", "")).strip()
        if path:
            outputs.add(path)
        cards[goal.goal_id] = set()
        for agent in goal.agents:
            path = str((agent.output or {}).get("path", "")).strip()
            if path:
                outputs.add(path)
            if _is_collector(agent) and str(agent.entity or "").strip():
                cards[goal.goal_id].add(str(agent.entity).strip())
    ancestors = _ancestors(plan)
    notes: list[str] = []
    for goal in plan.goals:
        reachable = set(cards[goal.goal_id])
        for upstream in ancestors.get(goal.goal_id, set()):
            reachable |= cards.get(upstream, set())
        kept: list[str] = []
        for index, item in enumerate(goal.acceptance):
            text = str(item)
            reasons: list[str] = []
            missing = _missing_paths(text, outputs)
            if missing:
                reasons.append(f"引用的产物 {'、'.join(missing)} 不存在（已删卡或从未起草）")
            unreachable = [
                entity_id for entity_id, patterns in matchers
                if entity_id not in reachable and _mentioned_outside_negation(text, patterns)
            ]
            if unreachable:
                reasons.append(
                    f"提到 {'、'.join(unreachable)}，但本 goal 及其上游没有这些实体的采集卡"
                )
            if reasons:
                notes.append(
                    f"[修正4] {goal.goal_id}.acceptance[{index}] 已摘：{'；'.join(reasons)}"
                    f"——原文「{text}」"
                )
                continue
            kept.append(item)
        if len(kept) == len(goal.acceptance):
            continue
        if not kept:
            kept = [_ACCEPTANCE_FALLBACK]
            notes.append(
                f"[修正4] {goal.goal_id}.acceptance 被摘光，兜底一条不提实体的验收条"
                "（规则 4 要求至少一条）"
            )
        goal.acceptance = kept
    return notes


def _acceptance_paths(text: str) -> list[str]:
    """验收条里引用的产物路径，一律归一成全形 `goals/goal-N/<file>`。

    - 短形 `goal-1/data-collection.json` 补 `goals/` 前缀（D-062 挂账：旧闸只认全形，
      引擎用短形引一份被删卡的产物就拦不到）。
    - 句末英文句点不算文件名（「…data-collection-1.json.」）。
    - ⛔ `goal-1/goal-2` 这种 goal 并列不是路径——叶子本身是 goal id 的整段跳过。
    """

    paths: list[str] = []
    for raw in _ACCEPTANCE_PRODUCT_PATH.findall(text):
        raw = raw.rstrip(".")
        leaf = raw.rsplit("/", 1)[-1]
        if not leaf or _GOAL_ID.fullmatch(leaf):
            continue
        paths.append(raw if raw.startswith("goals/") else f"goals/{raw}")
    return paths


def _missing_paths(text: str, outputs: set[str]) -> list[str]:
    """引用了但不在现存产物里的路径。没写扩展名的（`goal-2/data-collection-3`）按主干比。"""

    stems = {PurePosixPath(path).with_suffix("").as_posix() for path in outputs}
    return [
        path for path in _acceptance_paths(text)
        if path not in outputs
        and not (PurePosixPath(path).suffix == "" and path in stems)
    ]


def _entity_matchers(plan: Plan) -> list[tuple[str, list[re.Pattern[str]]]]:
    """每个实体卡 → 它全部叫法的匹配器。

    中文名按子串；ASCII 名按**整词**（前后不接字母数字）且忽略大小写——DeepSeek 的
    别名表里有「DS」，裸子串会把「HEADS」「IDS」都判成提到。没有实体卡的历史计划
    （`entities=[]`）返回空表，按实体那一半就不动。
    """

    result: list[tuple[str, list[re.Pattern[str]]]] = []
    for entity in plan.entities:
        names = {entity.id, entity.canonical, entity.names.get("zh"), entity.names.get("en"),
                 *entity.names.get("aliases", [])}
        patterns: list[re.Pattern[str]] = []
        for name in names:
            name = str(name or "").strip()
            if not name:
                continue
            if any("一" <= ch <= "鿿" for ch in name):
                patterns.append(re.compile(re.escape(name)))
            else:
                patterns.append(re.compile(
                    rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])", re.IGNORECASE))
        if patterns:
            result.append((entity.id.strip(), patterns))
    return result


def _acceptance_clauses(text: str) -> list[str]:
    """按「，,；;。」切子句，但括号 / 引号里的分隔符不切（不配对的括号按到句尾算）。"""

    clauses: list[str] = []
    buffer: list[str] = []
    closers: list[str] = []
    for ch in text:
        if ch in _ACCEPTANCE_BRACKETS:
            closers.append(_ACCEPTANCE_BRACKETS[ch])
        elif closers and ch == closers[-1]:
            closers.pop()
        elif not closers and ch in _ACCEPTANCE_CLAUSE_SEPARATORS:
            clauses.append("".join(buffer))
            buffer = []
            continue
        buffer.append(ch)
    clauses.append("".join(buffer))
    return clauses


def _mentioned_outside_negation(text: str, patterns: list[re.Pattern[str]]) -> bool:
    """这条验收条是不是在「要求写」这个实体：按子句看，提到它且子句里没有否定/限定词。

    全部提及都落在否定子句里（「未出现 Kimi 或 DeepSeek 的任何叫法」）⇒ False，不摘。
    """

    for clause in _acceptance_clauses(text):
        if not any(pattern.search(clause) for pattern in patterns):
            continue
        if not any(word in clause for word in _ACCEPTANCE_NEGATION_WORDS):
            return True
    return False


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
