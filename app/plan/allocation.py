"""§PLAN-1 货 1：骨架之后、goal 段之前的确定性采集分配表。

规则 25 的分子（subjects）由骨架产、分母（采集 agent）由各 goal 段各自猜，
此前没有任何一步把「哪个实体归哪个 goal、走哪个源」定下来，全局约束靠三次
独立抽卡收敛。这里用纯函数把分配定死，goal 段只需照清单采。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence

from app.config import ResearchScaleProfile
from app.plan.entities import mentions
from app.plan.lint import (
    _SOURCE_LOCALES, _entity_names, applicable_sources, entity_locales,
)
from app.sources.registry import planning_catalog

# 每个 goal 至少给交叉验证 + 撰写留两章，剩下的才是采集位。
RESERVED_NON_COLLECTION_CHAPTERS = 2

# 源优先序：按实体轮转，subjects 越多覆盖的源越多。cn 把 x 放末位（国内品牌
# 在 X 上基本无声量）；global 把 HN 放首位（既有夹具全部以 HN 覆盖主体）。
_SOURCE_PRIORITY: Mapping[str, tuple[str, ...]] = {
    "cn_product": ("xhs", "weibo", "web_search", "douyin", "wechat_mp", "x"),
    "global_product": (
        "hacker_news", "product_hunt", "reddit", "web_search", "x", "xhs", "douyin",
    ),
}


# 跨语域补位的偏好序（§ENT-2）。**故意不复用 `_SOURCE_PRIORITY`**：那两条序列排的是
# 「这个市场属性的主场怎么采」，global 把 HN 放首位是因为既有夹具以 HN 覆盖主体；
# 而跨语域补位问的是另一个问题——「这个产品的外文名，普通用户在哪儿聊」。Reddit /
# 小红书是各自语言里最泛的用户讨论面，HN 与 Product Hunt 只覆盖发布与技术圈，
# 拿来当唯一的对面语域补位会大概率空手（ENT-1 真机即 reddit 命中、HN 无声量）。
_CROSS_LOCALE_PRIORITY: Mapping[str, tuple[str, ...]] = {
    "en": ("reddit", "x", "hacker_news", "product_hunt"),
    "zh": ("xhs", "weibo", "douyin", "wechat_mp"),
}


@dataclass(frozen=True)
class CollectionSlot:
    entity: str
    source_id: str
    collector_name: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def collection_capacity(
    goal_count: int, profile: ResearchScaleProfile,
) -> int | None:
    """全计划采集位总数；章数无上限的档位返回 None。"""

    per_goal = per_goal_capacity(profile)
    return None if per_goal is None else per_goal * goal_count


def per_goal_capacity(profile: ResearchScaleProfile) -> int | None:
    if profile.max_chapters_per_goal is None:
        return None
    return max(profile.max_chapters_per_goal - RESERVED_NON_COLLECTION_CHAPTERS, 0)


def subjects_budget(
    goal_count: int, profile: ResearchScaleProfile, *, scale: str | None = None,
) -> int | None:
    """骨架能挑几个研究实体：fast 的 6 个采集位固定留 3 个给跨语域。

    §ENT-3 把 ENT-2 的「只给主角留一张对面语域卡」改成分档留位：fast 留 3，
    standard 因章数无上限不压 subjects，但分配阶段仍给主角排 4 个跨语域源。

    为什么 fast 是 3：总位数仍为 3 goal × 2 采集位 = 6；一半给主角跨语域，
    一半保留主体与竞品覆盖。扩位不靠增加 goal、章数或每 goal 源数。

    装不下的跨语域候选由 `allocate_collections` 尽力跳过，不反向挤掉实体主位。
    """

    capacity = collection_capacity(goal_count, profile)
    return None if capacity is None else max(
        capacity - cross_locale_slots_budget(profile, scale=scale), 1,
    )


def cross_locale_slots_budget(
    profile: ResearchScaleProfile, *, scale: str | None = None,
) -> int:
    """主角跨语域位；生产链显式传档位，旧调用才按默认配置形状兼容。"""

    if scale is not None:
        if scale not in {"fast", "standard"}:
            raise ValueError(f"scale 只能取 fast 或 standard，实际为 {scale!r}")
        return 3 if scale == "fast" else 4
    return 3 if profile.max_chapters_per_goal is not None else 4


#: §ALLOC-1（用户 2026-09-11 拍甲）：fast 档主角在**本市场语域的主源**里先占几位。
#: 3 = 小红书 + 微博 + 抖音（按 `_SOURCE_PRIORITY["cn_product"]` 的序取本语域前三个）；
#: 公众号是第四个，自然落在外面——那一位在真机整跑里 yielded 0，谁占都是白占。
#: standard 章数无上限，主角占**每个**本语域主源。⛔ 不靠加 goal / 章 / 每 goal 源数扩容：
#: fast 仍是 6 位，主角多占 ⇒ 竞品少占（6 = 主角 3 + 跨语域 1 + 竞品 2）。
PROTAGONIST_HOME_SLOTS: Mapping[str, int | None] = {"fast": 3, "standard": None}

#: 竞品排位之前给主角跨语域留的位（fast）。留 1 不留 3：跨语域预算 3 在 fast 下从来
#: 只装得下 1（见 `test_ent2_只有单侧叫法…` 的「补位只补得下 1 个」），留 3 会把竞品挤光。
PROTAGONIST_CROSS_LOCALE_RESERVE = 1

#: §ALLOC-2（调度 2026-09-13 代拍甲-2）：分配表按 goal 语义归位时，主角 goal 位数不够
#: （fast 每 goal 2 位、≤2 源——`app/config.py` 数值行，禁区 ⛔ 不动），主角 goal **先留**
#: 这两个源，其余主角卡溢到竞品 goal 作「对照基线」。为什么是小红书+抖音而不是优先序
#: 里的小红书+微博：微博走预采集池、封顶 25 行，留它主角 goal 的语料仍是八成小红书
#: （SECQ-1 三表实测 xhs 24 / weibo 2）；抖音实测 191 行、102 条点名豆包，是 fast 下
#: 唯一能把单平台占比压到六成上下的摆法。⛔ 这是「留在主角 goal 里的序」，不改
#: `_SOURCE_PRIORITY`（那条序定的是主角占哪几个源，ALLOC-1 甲的读数一个不变）。
PROTAGONIST_GOAL_KEEP: tuple[str, ...] = ("xhs", "douyin")

#: 溢出到竞品 goal 的主角卡在 `baseline` 账上的 reason；D-062 的验收条闸与读账的人
#: 据此知道这张卡「不是本 goal 的实体」，只是对照基线。
PROTAGONIST_BASELINE_REASON = "protagonist_baseline"


def protagonist_home_slots(
    profile: ResearchScaleProfile, *, scale: str | None = None,
) -> int | None:
    """主角在本语域主源里先占几位；None = 每个主源各一位（章数无上限的档位）。"""
    if scale is not None:
        if scale not in PROTAGONIST_HOME_SLOTS:
            raise ValueError(f"scale 只能取 fast 或 standard，实际为 {scale!r}")
        return PROTAGONIST_HOME_SLOTS[scale]
    return PROTAGONIST_HOME_SLOTS["fast" if profile.max_chapters_per_goal is not None else "standard"]


def ordered_sources(
    market_profile: str,
    entities: Sequence[Mapping[str, Any]] | None = None,
) -> list[str]:
    """可排的源，按优先序。**「有没有」看实体叫法，market_profile 只定顺序。**

    §ENT-2（用户 2026-09-03 傍晚）：题面写「国内大家对豆包的看法」判出
    cn_product，但豆包有英文名 Doubao，Reddit 上就有得搜——旧实现里海外源
    压根不进分配表，采集期按语域取词（`source_mcp.entity_queries`）再准也没用。
    现在实体任一有中文叫法就放国内源、任一有英文叫法就放海外源；本市场属性的
    优先序排在前（权重仍在），跨语域放宽进来的源按对方市场属性的优先序缀在后。
    `entities` 为空（抽不出实体 / 旧调用）时逐字退回旧行为。
    """

    applicable = applicable_sources(market_profile, entities)
    catalog = {spec.source_id for spec in planning_catalog()}
    other = "global_product" if market_profile == "cn_product" else "cn_product"
    ordered: list[str] = []
    for source in _SOURCE_PRIORITY[market_profile] + _SOURCE_PRIORITY[other]:
        if source in applicable and source in catalog and source not in ordered:
            ordered.append(source)
    return ordered


def _place(
    source: str,
    plan: dict[str, list[CollectionSlot]],
    goal_sources: dict[str, set[str]],
    goal_ids: Sequence[str],
    per_goal: int | None,
    profile: ResearchScaleProfile,
    pointer: int,
) -> tuple[str | None, int]:
    """从 pointer 起轮转找一个装得下这个源的 goal；找不到返回 (None, pointer)。"""

    for offset in range(len(goal_ids)):
        goal_id = goal_ids[(pointer + offset) % len(goal_ids)]
        if per_goal is not None and len(plan[goal_id]) >= per_goal:
            continue
        distinct = goal_sources[goal_id] | {source}
        if (
            profile.max_sources_per_goal is not None
            and len(distinct) > profile.max_sources_per_goal
        ):
            continue
        return goal_id, (pointer + offset + 1) % len(goal_ids)
    return None, pointer


def allocate_collections(
    subjects: Sequence[str],
    market_profile: str,
    scaffolds: Sequence[Mapping[str, Any]],
    profile: ResearchScaleProfile,
    entities: Sequence[Mapping[str, Any]] | None = None,
    *,
    scale: str | None = None,
    entity_slot_target: int | None = None,
    skipped: list[dict[str, str]] | None = None,
    protagonists: Sequence[str] | None = None,
    baseline: list[dict[str, str]] | None = None,
) -> dict[str, list[CollectionSlot]]:
    """每个 subject 至少一个采集位；无 depends_on 的 goal 先分，再按序号轮转。

    §ALLOC-2（调度 2026-09-13 代拍甲）：主角优先模式下，若骨架 goal 的 title / objective
    点名了任一研究实体，**卡按 goal 语义归位**（见 `goal_affinity` 与 `_allocate_affine`）：
    主角卡先进标题点名主角的 goal、竞品卡进标题点名该竞品的 goal；主角 goal 装不下的
    主角卡溢到竞品 goal 作对照基线，记入 `baseline`（reason=protagonist_baseline）。
    六对 `(source, entity)` 与归位前**完全相同**，只换落到哪个 goal。骨架一个实体都
    不点名（旧夹具 / 「官方·口碑·竞品」这类按性质分段且不写产品名的骨架）⇒ 逐字退回
    ALLOC-1 的位置轮转。

    §ALLOC-1：传了 `protagonists`（主角 canonical，由题面推出——⛔ 不是 `subjects[0]`，
    见 `app/report/polish/tables.subject_canonicals`）且恰好一个主角在 subjects 里时，
    走「主角先占各主源，竞品再排」：主角先在本市场语域的主源里占 `protagonist_home_slots`
    位，竞品再按主源优先序**同源**排位（每竞品一位；先填满优先序最前的源，两竞品都落
    小红书就是设计——同源才可比，且不落只装预采集词的微博池），最后跨语域那套原样跑。
    装不下的竞品记入 `skipped`（reason=protagonist_first）不抛错。
    其余情形（没传主角 / 题面点不出 / 点出两个以上 / 主角在本语域没叫法 / 没有实体卡）
    **逐字退回旧行为**——旧行为是「实体按源优先序轮转」，主角只因为叫法多才多占源。

    §ENT-3 第二轮：中外都有叫法的主角按偏好序补对面语域，fast 3 位、standard
    4 位。第二轮尽力而为——章预算或每 goal 源数装不下就记入 `skipped` 后跳过，
    绝不因此让整份计划抛错；`entities` 为空时行为与 §PLAN-1 逐字相同。

    `entity_slot_target` 只给最终兜底合并使用：重复 subject 被折叠后，用同语域的
    额外来源补回原实体位数，避免合并让分配表缩水。

    同 goal 内不同源数不超过 max_sources_per_goal，(source, entity) 对全计划唯一。
    第一轮放不下时抛 ValueError——这条错在骨架层就该拦住（见 _skeleton_scaffolds）。
    """

    sources = ordered_sources(market_profile, entities)
    if not sources:
        raise ValueError(f"market_profile={market_profile!r} 下没有可用采集源")
    collectors = {spec.source_id: spec.collector_name for spec in planning_catalog()}
    per_goal = per_goal_capacity(profile)
    order = sorted(
        range(len(scaffolds)),
        key=lambda index: (len(scaffolds[index].get("depends_on", [])), index),
    )
    goal_ids = [f"goal-{index + 1}" for index in order]
    plan: dict[str, list[CollectionSlot]] = {f"goal-{i + 1}": [] for i in range(len(scaffolds))}
    goal_sources: dict[str, set[str]] = {goal_id: set() for goal_id in plan}
    pointer = 0
    taken: set[tuple[str, str]] = set()
    protagonist_plan = _protagonist_first(
        subjects, market_profile, sources, entities, protagonists,
        profile, scale=scale,
    )
    if protagonist_plan is not None:
        lead, home_sources, competitors = protagonist_plan
        affinity = goal_affinity(scaffolds, entities, subjects)
        if affinity:
            return _allocate_affine(
                lead, home_sources, competitors, subjects, market_profile, sources,
                entities, profile, plan, goal_sources, goal_ids, per_goal,
                collectors, affinity, scale=scale, skipped=skipped, baseline=baseline,
            )
        for source in home_sources:
            chosen, pointer = _place(
                source, plan, goal_sources, goal_ids, per_goal, profile, pointer,
            )
            if chosen is None:
                break
            plan[chosen].append(CollectionSlot(lead, source, collectors[source]))
            goal_sources[chosen].add(source)
            taken.add((source, lead))
        capacity = collection_capacity(len(scaffolds), profile)
        cross_candidates = _cross_locale_slots(
            [lead], sources, entities, taken,
            cross_locale_slots_budget(profile, scale=scale),
        )
        reserve = PROTAGONIST_CROSS_LOCALE_RESERVE if cross_candidates else 0
        competitor_budget = (
            None if capacity is None else max(capacity - len(taken) - reserve, 0)
        )
        main_sources = [
            source for source in _SOURCE_PRIORITY[market_profile] if source in sources
        ]
        pending = list(competitors)
        for source in main_sources:
            for entity in list(pending):
                if competitor_budget is not None and (
                    len(taken) - len(home_sources) >= competitor_budget
                ):
                    break
                chosen, pointer = _place(
                    source, plan, goal_sources, goal_ids, per_goal, profile, pointer,
                )
                if chosen is None:
                    continue
                plan[chosen].append(CollectionSlot(entity, source, collectors[source]))
                goal_sources[chosen].add(source)
                taken.add((source, entity))
                pending.remove(entity)
            if not pending:
                break
        if skipped is not None:
            skipped.extend(
                {"entity": entity, "source_id": "", "reason": "protagonist_first"}
                for entity in pending
            )
            # 旧快照里主角有两条 subject（`豆包`/`Doubao`）：位全记在第一条名下，
            # 其余几条不占位也不算丢，单独记一笔让规则 25 的红有处可查。
            skipped.extend(
                {"entity": entity, "source_id": "", "reason": "same_protagonist"}
                for entity in subjects
                if entity != lead and entity not in competitors
            )
        for entity, source in cross_candidates:
            chosen, pointer = _place(
                source, plan, goal_sources, goal_ids, per_goal, profile, pointer,
            )
            if chosen is None:
                if skipped is not None:
                    skipped.append({
                        "entity": entity, "source_id": source, "reason": "capacity",
                    })
                continue
            plan[chosen].append(CollectionSlot(entity, source, collectors[source]))
            goal_sources[chosen].add(source)
            taken.add((source, entity))
        return plan
    for position, entity in enumerate(subjects):
        source = sources[position % len(sources)]
        chosen, pointer = _place(
            source, plan, goal_sources, goal_ids, per_goal, profile, pointer,
        )
        if chosen is None:
            raise ValueError(
                f"研究实体 {len(subjects)} 个超出采集容量"
                f"（{len(scaffolds)} 个 goal × 每 goal {per_goal} 个采集位）：{entity}"
            )
        plan[chosen].append(CollectionSlot(entity, source, collectors[source]))
        goal_sources[chosen].add(source)
        taken.add((source, entity))
    target = max(len(subjects), int(entity_slot_target or 0))
    main_sources = [
        source for source in _SOURCE_PRIORITY[market_profile] if source in sources
    ]
    used_sources = {source for source, _entity in taken}
    unused = [source for source in main_sources if source not in used_sources]
    candidates = [*unused, *(source for source in main_sources if source not in unused)]
    for source in candidates:
        for entity in subjects:
            if len(taken) >= target:
                break
            if (source, entity) in taken:
                continue
            chosen, pointer = _place(
                source, plan, goal_sources, goal_ids, per_goal, profile, pointer,
            )
            if chosen is not None:
                plan[chosen].append(CollectionSlot(entity, source, collectors[source]))
                goal_sources[chosen].add(source)
                taken.add((source, entity))
        if len(taken) >= target:
            break
    for entity, source in _cross_locale_slots(
        subjects, sources, entities, taken,
        cross_locale_slots_budget(profile, scale=scale),
    ):
        chosen, pointer = _place(
            source, plan, goal_sources, goal_ids, per_goal, profile, pointer,
        )
        if chosen is None:
            if skipped is not None:
                skipped.append({
                    "entity": entity, "source_id": source, "reason": "capacity",
                })
            continue
        plan[chosen].append(CollectionSlot(entity, source, collectors[source]))
        goal_sources[chosen].add(source)
        taken.add((source, entity))
    return plan


def goal_affinity(
    scaffolds: Sequence[Mapping[str, Any]],
    entities: Sequence[Mapping[str, Any]] | None,
    subjects: Sequence[str],
) -> dict[str, dict[str, int]]:
    """每个 goal 对每个 subject 的亲和度：title 点名 = 2，只有 objective 点名 = 1，否则 0。

    点名判定沿用 `app.plan.entities.mentions` × `lint._entity_names`——与 D-059 主角判法、
    原声闸、D-062 验收条闸是**同一把尺子**（中文按包含、拉丁名要词边界、一个字的名字不算）；
    ⛔ 不另写匹配，两处各写一份的差异是静默的。

    没有任何 goal 点名任何 subject 时返回空 dict，调用方据此退回位置轮转。
    goal_id 按 scaffolds 的序号（goal-1 起），与 `allocate_collections` 的 plan 键一致。
    """

    by_id = {
        str(card.get("id") or card.get("canonical") or ""): card
        for card in entities or [] if isinstance(card, Mapping)
    }
    names: dict[str, list[str]] = {}
    for subject in subjects:
        card = by_id.get(subject)
        picked = _entity_names(card) if card is not None else []
        if len(subject.strip()) >= 2 and subject not in picked:
            picked = [subject.strip(), *picked]
        names[subject] = picked
    result: dict[str, dict[str, int]] = {}
    any_hit = False
    for index, scaffold in enumerate(scaffolds, start=1):
        title = str(scaffold.get("title") or "")
        objective = str(scaffold.get("objective") or "")
        scores: dict[str, int] = {}
        for subject in subjects:
            if any(mentions(title, name) for name in names[subject]):
                scores[subject] = 2
            elif any(mentions(objective, name) for name in names[subject]):
                scores[subject] = 1
            else:
                scores[subject] = 0
            any_hit = any_hit or scores[subject] > 0
        result[f"goal-{index}"] = scores
    return result if any_hit else {}


def _tiers(
    entity: str, goal_ids: Sequence[str], affinity: Mapping[str, Mapping[str, int]],
) -> list[list[str]]:
    """一张卡该按什么顺序找 goal：先 title 点名它的、再 objective 点名它的、最后其余。"""

    return [
        [goal_id for goal_id in goal_ids if affinity.get(goal_id, {}).get(entity, 0) == score]
        for score in (2, 1, 0)
    ]


def _place_affine(
    entity: str,
    source: str,
    tiers: Sequence[Sequence[str]],
    plan: dict[str, list[CollectionSlot]],
    goal_sources: dict[str, set[str]],
    per_goal: int | None,
    profile: ResearchScaleProfile,
    pointers: dict[int, int],
) -> tuple[str | None, int]:
    """逐层找一个装得下的 goal；返回 (goal_id, 命中的层号)，找不到 (None, -1)。

    同层多个 goal 时**按 goal 序填满一个再下一个**（无依赖的 goal 排前，见
    `allocate_collections` 的 `goal_ids`），⛔ 不轮转。小跑 `r-alloc2-0913-a` 实测：骨架
    「豆包基本盘 / 竞品对照 / 豆包归纳」两个 goal 标题都点名主角，层内轮转把小红书与
    抖音拆到两个 goal（goal-1 小红书+微博、goal-3 抖音+Reddit），甲-2「主角 goal 留
    小红书+抖音」就落空了；填满再下一个才让排在前面的主线 goal 拿到最厚的两源。
    `pointers` 只为兼容签名保留，不再参与决策。
    """

    del pointers
    for level, goal_ids in enumerate(tiers):
        if not goal_ids:
            continue
        chosen, _pointer = _place(
            source, plan, goal_sources, goal_ids, per_goal, profile, 0,
        )
        if chosen is not None:
            return chosen, level
    return None, -1


def _allocate_affine(
    lead: str,
    home_sources: Sequence[str],
    competitors: Sequence[str],
    subjects: Sequence[str],
    market_profile: str,
    sources: Sequence[str],
    entities: Sequence[Mapping[str, Any]] | None,
    profile: ResearchScaleProfile,
    plan: dict[str, list[CollectionSlot]],
    goal_sources: dict[str, set[str]],
    goal_ids: Sequence[str],
    per_goal: int | None,
    collectors: Mapping[str, str],
    affinity: Mapping[str, Mapping[str, int]],
    *,
    scale: str | None,
    skipped: list[dict[str, str]] | None,
    baseline: list[dict[str, str]] | None,
) -> dict[str, list[CollectionSlot]]:
    """§ALLOC-2 归位：同一张牌（ALLOC-1 甲的六对）按 goal 语义落位。

    三步，顺序是设计不是巧合：
    1. 主角卡按 `PROTAGONIST_GOAL_KEEP` 序**只**进 title 点名主角的 goal，装不下的记为溢出；
    2. 竞品卡各一张，进 title 点名该竞品的 goal（其次 objective 点名、再其次任意有位的）——
       竞品**先于**主角溢出卡落位，保证「每个竞品 goal 至少一张该实体卡」不被主角挤掉；
    3. 主角溢出卡 + 跨语域卡再落位：还进主角 goal（standard 章数无上限时全回主角 goal），
       否则进 objective 点名主角的竞品 goal 作对照基线，记入 `baseline`；哪儿都没位记 skipped。
    """

    taken: set[tuple[str, str]] = set()
    pointers: dict[int, int] = {}
    lead_tiers = _tiers(lead, goal_ids, affinity)

    def put(goal_id: str, entity: str, source: str) -> None:
        plan[goal_id].append(CollectionSlot(entity, source, collectors[source]))
        goal_sources[goal_id].add(source)
        taken.add((source, entity))

    # 1. 主角 goal 先留 KEEP 序里的源，其余按主源优先序。
    keep = [s for s in PROTAGONIST_GOAL_KEEP if s in home_sources]
    ordered_home = [*keep, *(s for s in home_sources if s not in keep)]
    overflow: list[tuple[str, str]] = []
    for source in ordered_home:
        chosen, _level = _place_affine(
            lead, source, [lead_tiers[0]], plan, goal_sources, per_goal, profile, pointers,
        )
        if chosen is None:
            overflow.append((lead, source))
            continue
        put(chosen, lead, source)

    # 2. 竞品：预算与 ALLOC-1 同口径（总位 − 主角本语域位 − 跨语域预留 1）。
    capacity = collection_capacity(len(goal_ids), profile)
    cross_candidates = _cross_locale_slots(
        [lead], sources, entities,
        # 溢出的主角卡稍后一定会落位，算作已覆盖该语域，跨语域候选才不会重复排本语域。
        taken | {(source, entity) for entity, source in overflow},
        cross_locale_slots_budget(profile, scale=scale),
    )
    reserve = PROTAGONIST_CROSS_LOCALE_RESERVE if cross_candidates else 0
    competitor_budget = (
        None if capacity is None else max(capacity - len(home_sources) - reserve, 0)
    )
    main_sources = [
        source for source in _SOURCE_PRIORITY[market_profile] if source in sources
    ]
    pending = list(competitors)
    placed_competitors = 0
    for entity in list(pending):
        if competitor_budget is not None and placed_competitors >= competitor_budget:
            break
        tiers = _tiers(entity, goal_ids, affinity)
        for source in main_sources:
            chosen, _level = _place_affine(
                entity, source, tiers, plan, goal_sources, per_goal, profile, pointers,
            )
            if chosen is None:
                continue
            put(chosen, entity, source)
            pending.remove(entity)
            placed_competitors += 1
            break
    if skipped is not None:
        skipped.extend(
            {"entity": entity, "source_id": "", "reason": "protagonist_first"}
            for entity in pending
        )
        skipped.extend(
            {"entity": entity, "source_id": "", "reason": "same_protagonist"}
            for entity in subjects
            if entity != lead and entity not in competitors
        )

    # 3. 主角溢出卡 + 跨语域卡：主角 goal 有位就回去，否则进对照 goal 记基线。
    for entity, source in [*overflow, *cross_candidates]:
        chosen, level = _place_affine(
            entity, source, lead_tiers, plan, goal_sources, per_goal, profile, pointers,
        )
        if chosen is None:
            if skipped is not None:
                skipped.append({
                    "entity": entity, "source_id": source, "reason": "capacity",
                })
            continue
        put(chosen, entity, source)
        # 只有落进「标题点名了别的实体」的 goal（竞品 goal）才是对照基线；落进
        # 「官方资料 / 用户口碑」这类不点名的 goal 时，主角就是它讲的东西，不记账。
        others_goal = any(
            affinity.get(chosen, {}).get(other, 0) == 2
            for other in subjects if other != lead
        )
        if level > 0 and others_goal and baseline is not None:
            baseline.append({
                "goal_id": chosen, "entity": entity, "source_id": source,
                "reason": PROTAGONIST_BASELINE_REASON,
            })
    return plan


def _protagonist_first(
    subjects: Sequence[str],
    market_profile: str,
    sources: Sequence[str],
    entities: Sequence[Mapping[str, Any]] | None,
    protagonists: Sequence[str] | None,
    profile: ResearchScaleProfile,
    *,
    scale: str | None,
) -> tuple[str, list[str], list[str]] | None:
    """主角优先模式的三元组 (主角 subject, 主角本语域主源, 竞品 subjects)；不适用返回 None。

    「不适用」的每一条都退回旧行为，理由写在 `allocate_collections` 的 docstring。
    主角 subject 用 subjects 里 canonical 命中的**第一个**——§ENT-3 之后同一实体只剩
    一个 subject，这里的「第一个」只为兼容旧快照里 `豆包`/`Doubao` 并存的形态。
    """
    if not protagonists or not entities:
        return None
    by_id = {
        str(card.get("id") or card.get("canonical") or ""): card
        for card in entities if isinstance(card, Mapping)
    }
    wanted = {str(name).strip() for name in protagonists if str(name).strip()}
    lead_subjects = [
        subject for subject in subjects
        if str(by_id.get(subject, {}).get("canonical") or subject).strip() in wanted
    ]
    if len({
        str(by_id.get(subject, {}).get("canonical") or subject).strip()
        for subject in lead_subjects
    }) != 1:
        return None
    lead = lead_subjects[0]
    home_locale = "zh" if market_profile == "cn_product" else "en"
    card = by_id.get(lead)
    if card is None or home_locale not in entity_locales([card]):
        return None
    home_sources = [
        source for source in _SOURCE_PRIORITY[market_profile]
        if source in sources and _SOURCE_LOCALES.get(source) == home_locale
    ]
    cap = protagonist_home_slots(profile, scale=scale)
    if cap is not None:
        home_sources = home_sources[:cap]
    if not home_sources:
        return None
    competitors = [subject for subject in subjects if subject not in lead_subjects]
    return lead, home_sources, competitors


def _cross_locale_slots(
    subjects: Sequence[str],
    sources: Sequence[str],
    entities: Sequence[Mapping[str, Any]] | None,
    taken: set[tuple[str, str]],
    limit: int,
) -> list[tuple[str, str]]:
    """双语主角按对面语域优先序取至多 `limit` 个不同源。"""

    by_id = {
        str(card.get("id") or card.get("canonical") or ""): card
        for card in entities or [] if isinstance(card, Mapping)
    }
    if not subjects:
        return []
    entity = str(subjects[0])
    card = by_id.get(entity)
    if card is None or entity_locales([card]) != {"zh", "en"}:
        return []
    covered = {
        _SOURCE_LOCALES.get(source) for source, name in taken if name == entity
    }
    missing = next((locale for locale in ("zh", "en") if locale not in covered), None)
    if missing is None:
        return []
    return [
        (entity, source) for source in _CROSS_LOCALE_PRIORITY[missing]
        if source in sources and (source, entity) not in taken
    ][:max(0, limit)]


def collection_plan_dict(
    plan: Mapping[str, Sequence[CollectionSlot]],
) -> dict[str, list[dict[str, str]]]:
    return {goal_id: [slot.to_dict() for slot in slots] for goal_id, slots in plan.items()}
