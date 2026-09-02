"""§PLAN-1 货 1：骨架之后、goal 段之前的确定性采集分配表。

规则 25 的分子（subjects）由骨架产、分母（采集 agent）由各 goal 段各自猜，
此前没有任何一步把「哪个实体归哪个 goal、走哪个源」定下来，全局约束靠三次
独立抽卡收敛。这里用纯函数把分配定死，goal 段只需照清单采。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence

from app.config import ResearchScaleProfile
from app.plan.lint import _SOURCE_MARKET_PROFILES
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


def ordered_sources(market_profile: str) -> list[str]:
    """市场属性下可用且已注册的源，按优先序。"""

    applicable = _SOURCE_MARKET_PROFILES[market_profile]
    catalog = {spec.source_id for spec in planning_catalog()}
    return [
        source for source in _SOURCE_PRIORITY[market_profile]
        if source in applicable and source in catalog
    ]


def allocate_collections(
    subjects: Sequence[str],
    market_profile: str,
    scaffolds: Sequence[Mapping[str, Any]],
    profile: ResearchScaleProfile,
) -> dict[str, list[CollectionSlot]]:
    """每个 subject 恰好一个采集位；无 depends_on 的 goal 先分，再按序号轮转。

    同 goal 内不同源数不超过 max_sources_per_goal，(source, entity) 对全计划唯一。
    放不下时抛 ValueError——这条错在骨架层就该拦住（见 _skeleton_scaffolds）。
    """

    sources = ordered_sources(market_profile)
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
    for position, entity in enumerate(subjects):
        source = sources[position % len(sources)]
        chosen: str | None = None
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
            chosen = goal_id
            pointer = (pointer + offset + 1) % len(goal_ids)
            break
        if chosen is None:
            raise ValueError(
                f"研究实体 {len(subjects)} 个超出采集容量"
                f"（{len(scaffolds)} 个 goal × 每 goal {per_goal} 个采集位）：{entity}"
            )
        plan[chosen].append(CollectionSlot(entity, source, collectors[source]))
        goal_sources[chosen].add(source)
    return plan


def collection_plan_dict(
    plan: Mapping[str, Sequence[CollectionSlot]],
) -> dict[str, list[dict[str, str]]]:
    return {goal_id: [slot.to_dict() for slot in slots] for goal_id, slots in plan.items()}
