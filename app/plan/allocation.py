"""§PLAN-1 货 1：骨架之后、goal 段之前的确定性采集分配表。

规则 25 的分子（subjects）由骨架产、分母（采集 agent）由各 goal 段各自猜，
此前没有任何一步把「哪个实体归哪个 goal、走哪个源」定下来，全局约束靠三次
独立抽卡收敛。这里用纯函数把分配定死，goal 段只需照清单采。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence

from app.config import ResearchScaleProfile
from app.plan.lint import _SOURCE_LOCALES, applicable_sources, entity_locales
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


def subjects_budget(goal_count: int, profile: ResearchScaleProfile) -> int | None:
    """骨架能挑几个研究实体：采集位总数**留一位**给跨语域补位（§ENT-2，用户 09-03 拍板乙）。

    为什么留：`collection_capacity` 是采集位总数，骨架此前被允许挑满。挑满之后
    「中外都有叫法的实体再补一张对面语域的卡」（`allocate_collections` 第二轮）
    一个空位都不剩，快速档铺满型题面（一个主角 + 五个竞品）永远跨不了语域，
    而那正是用户要的效果。

    为什么是留一位而不是按实体数留：跨语域补位是尽力而为，留一位就够让**主角**
    （subjects 原序第一个）拿到对面那张卡；留更多会成比例地削竞品数。而且被让掉的
    那个名额本来就领不到实体卡——实体卡上限是 5 张（`entities.MAX_ENTITIES`），
    快速档第 6 个竞品既没有中外叫法也不会被跨语域路由。**两个上限就此对齐。**

    章数无上限的档位（standard）不受影响，返回 None。
    """

    capacity = collection_capacity(goal_count, profile)
    return None if capacity is None else max(capacity - 1, 1)


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
) -> dict[str, list[CollectionSlot]]:
    """每个 subject 至少一个采集位；无 depends_on 的 goal 先分，再按序号轮转。

    §ENT-2 第二轮：中外都有叫法的实体再补一个**对面语域**的采集位（豆包既进
    小红书也进 Reddit）。第二轮是尽力而为——章预算装不下就不补，绝不因此让
    整份计划抛错；`entities` 为空时整个第二轮不发生，行为与 §PLAN-1 逐字相同。

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
    for entity, source in _cross_locale_slots(subjects, sources, entities, taken):
        chosen, pointer = _place(
            source, plan, goal_sources, goal_ids, per_goal, profile, pointer,
        )
        if chosen is None:
            continue
        plan[chosen].append(CollectionSlot(entity, source, collectors[source]))
        goal_sources[chosen].add(source)
        taken.add((source, entity))
    return plan


def _cross_locale_slots(
    subjects: Sequence[str],
    sources: Sequence[str],
    entities: Sequence[Mapping[str, Any]] | None,
    taken: set[tuple[str, str]],
) -> list[tuple[str, str]]:
    """中外都有叫法的实体，各要一个对面语域的采集位（按 subjects 原序，先到先得）。"""

    by_id = {
        str(card.get("id") or card.get("canonical") or ""): card
        for card in entities or [] if isinstance(card, Mapping)
    }
    wanted: list[tuple[str, str]] = []
    for entity in subjects:
        card = by_id.get(entity)
        if card is None or entity_locales([card]) != {"zh", "en"}:
            continue
        covered = {
            _SOURCE_LOCALES.get(source)
            for source, name in taken if name == entity
        }
        for locale in ("zh", "en"):
            if locale in covered:
                continue
            for source in _CROSS_LOCALE_PRIORITY[locale]:
                if source in sources and (source, entity) not in taken:
                    wanted.append((entity, source))
                    break
            break
    return wanted


def collection_plan_dict(
    plan: Mapping[str, Sequence[CollectionSlot]],
) -> dict[str, list[dict[str, str]]]:
    return {goal_id: [slot.to_dict() for slot in slots] for goal_id, slots in plan.items()}
