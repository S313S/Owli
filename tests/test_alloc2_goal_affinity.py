"""§ALLOC-2：分配表按 goal 语义归位——Kimi 卡回 Kimi goal，主角溢出卡作对照基线。

病根（2026-09-12 SECQ-1 三表实测 `r-e9760470f3e0`）：ALLOC-1 甲的六张卡没错，但 `_place()`
只按「无依赖的 goal 先、再按序号轮转」找位，scaffolds 的 title 一个字没读——于是
「Kimi 对照视角」goal 里零 Kimi 卡、「DeepSeek 对照视角」goal 里零 DeepSeek 卡、
「豆包舆论主线」goal 拿了小红书·豆包 + 小红书·Kimi。

夹具是真的：`r-e9760470f3e0` 的 plan-segments 原样拷进 tests/fixtures/alloc2（与 d061 同一份，
另拷一份是让两包夹具互不牵连）。调度 2026-09-13 代拍：A 甲（归位，只改 allocation.py）、
B 甲-2（主角 goal 留小红书+抖音，溢出的微博/Reddit·豆包进竞品 goal 作对照基线并留账）。
"""

from __future__ import annotations

import json
from pathlib import Path

from app.config import load_research_scale_config
from app.plan.allocation import (
    PROTAGONIST_BASELINE_REASON, allocate_collections, collection_plan_dict, goal_affinity,
)
from app.plan.generate import _goal_prompt

FIXTURES = Path(__file__).parent / "fixtures" / "alloc2"
FAST = load_research_scale_config().profile("fast")
STANDARD = load_research_scale_config().profile("standard")
QUERY = "国内大家对豆包的看法"


def _skeleton() -> dict:
    return json.loads((FIXTURES / "skeleton.json").read_text(encoding="utf-8"))


def _cards() -> list[dict]:
    cards = []
    for index in (1, 2, 3):
        card = json.loads((FIXTURES / f"entity-{index}.json").read_text(encoding="utf-8"))
        card.setdefault("id", card["canonical"])  # 生产里 resolve_entities 填的就是 canonical
        cards.append(card)
    return cards


def _scaffolds(skeleton: dict) -> list[dict]:
    return [
        {"title": g["title"], "objective": g["objective"], "depends_on": g["depends_on"],
         "subjects": list(skeleton["subjects"])}
        for g in skeleton["goals"]
    ]


def _pairs(plan) -> set[tuple[str, str]]:
    return {(s.source_id, s.entity) for slots in plan.values() for s in slots}


def _entities_of(plan, goal_id: str) -> set[str]:
    return {s.entity for s in plan[goal_id]}


def _run(profile, scale: str, **kwargs):
    skeleton = _skeleton()
    return allocate_collections(
        skeleton["subjects"], skeleton["market_profile"], _scaffolds(skeleton), profile,
        _cards(), scale=scale, entity_slot_target=3, protagonists=["豆包"], **kwargs,
    )


def test_亲和度_title点名记2_只有objective点名记1_骨架不点名返回空() -> None:
    skeleton = _skeleton()
    affinity = goal_affinity(_scaffolds(skeleton), _cards(), skeleton["subjects"])
    assert affinity["goal-1"] == {"豆包": 2, "Kimi": 0, "DeepSeek": 0}
    # goal-2/goal-3 的 objective 写「以豆包评价基线为参照」——objective 点名主角记 1
    assert affinity["goal-2"] == {"豆包": 1, "Kimi": 2, "DeepSeek": 0}
    assert affinity["goal-3"] == {"豆包": 1, "Kimi": 0, "DeepSeek": 2}
    blank = [{"title": "一", "objective": "采", "depends_on": []}] * 3
    assert goal_affinity(blank, _cards(), skeleton["subjects"]) == {}


def test_造红_旧表goal2零Kimi卡_新表每竞品goal至少一张该实体卡_主角goal两源() -> None:
    old = json.loads((FIXTURES / "allocation.json").read_text(encoding="utf-8"))
    assert "Kimi" not in {s["entity"] for s in old["goal-2"]}, "红：旧表 Kimi 对照 goal 零 Kimi 卡"
    assert "DeepSeek" not in {s["entity"] for s in old["goal-3"]}

    plan = _run(FAST, "fast")
    assert "Kimi" in _entities_of(plan, "goal-2")
    assert "DeepSeek" in _entities_of(plan, "goal-3")
    assert len({s.source_id for s in plan["goal-1"]}) >= 2
    assert _entities_of(plan, "goal-1") == {"豆包"}, "主角 goal 只有主角卡"
    # 六对 (source, entity) 与旧表完全相同——归位只换 goal，不换牌
    assert _pairs(plan) == {(s["source_id"], s["entity"]) for v in old.values() for s in v}
    assert all(len(slots) <= 2 and len({s.source_id for s in slots}) <= 2 for slots in plan.values())


def test_甲2_主角goal留小红书加抖音_溢出的微博与Reddit进竞品goal记基线账() -> None:
    baseline: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    plan = collection_plan_dict(_run(FAST, "fast", baseline=baseline, skipped=skipped))
    got = {g: [f"{s['source_id']}·{s['entity']}" for s in v] for g, v in plan.items()}
    assert got == {
        "goal-1": ["xhs·豆包", "douyin·豆包"],
        "goal-2": ["xhs·Kimi", "weibo·豆包"],
        "goal-3": ["xhs·DeepSeek", "reddit·豆包"],
    }
    assert [(b["goal_id"], b["source_id"], b["entity"], b["reason"]) for b in baseline] == [
        ("goal-2", "weibo", "豆包", PROTAGONIST_BASELINE_REASON),
        ("goal-3", "reddit", "豆包", PROTAGONIST_BASELINE_REASON),
    ]
    assert not [item for item in skipped if item["reason"] == "protagonist_first"]


def test_standard_章数无上限_主角卡全回主角goal_竞品各归各goal_无基线() -> None:
    baseline: list[dict[str, str]] = []
    plan = _run(STANDARD, "standard", baseline=baseline)
    assert _entities_of(plan, "goal-1") == {"豆包"}
    assert {s.source_id for s in plan["goal-1"]} >= {"xhs", "weibo", "douyin", "wechat_mp", "reddit"}
    assert _entities_of(plan, "goal-2") == {"Kimi"} and _entities_of(plan, "goal-3") == {"DeepSeek"}
    assert baseline == []


def test_退回旧行为_骨架不点名任何实体时与ALLOC1输出逐字相同() -> None:
    skeleton = _skeleton()
    blank = [{"title": "一", "objective": "采", "depends_on": g["depends_on"]} for g in skeleton["goals"]]
    no_title = [{"depends_on": g["depends_on"]} for g in skeleton["goals"]]
    kwargs = dict(scale="fast", entity_slot_target=3, protagonists=["豆包"])
    legacy = collection_plan_dict(allocate_collections(
        skeleton["subjects"], "cn_product", no_title, FAST, _cards(), **kwargs))
    assert collection_plan_dict(allocate_collections(
        skeleton["subjects"], "cn_product", blank, FAST, _cards(), **kwargs)) == legacy
    # 旧行为的形状：位置轮转把 Kimi 卡放进 goal-1（这正是本包要修的现象，只在退回时保留）
    assert "Kimi" in {s["entity"] for s in legacy["goal-1"]}


def test_按性质分段的骨架_只有objective点名主角_主角卡分摊三goal_竞品照归位() -> None:
    """旧库 r-3e04f808dffd 那种「官方 / 口碑 / 竞品」分段：title 不写产品名。"""
    skeleton = _skeleton()
    scaffolds = [
        {"title": "官方产品定位与功能资料采集", "objective": "采集豆包官方资料。", "depends_on": []},
        {"title": "国内社媒用户口碑采集", "objective": "采集国内用户对豆包的口碑。", "depends_on": []},
        {"title": "同类 AI 助手竞品对照素材采集", "objective": "采集 Kimi 与 DeepSeek 的对照素材。", "depends_on": ["goal-1"]},
    ]
    baseline: list[dict[str, str]] = []
    plan = allocate_collections(
        skeleton["subjects"], "cn_product", scaffolds, FAST, _cards(),
        scale="fast", entity_slot_target=3, protagonists=["豆包"], baseline=baseline,
    )
    assert len(_pairs(plan)) == 6
    # 竞品 goal 的 title 不点名、objective 点名两竞品 ⇒ 两张竞品卡都归 goal-3
    assert _entities_of(plan, "goal-3") == {"Kimi", "DeepSeek"}
    # 主角卡按 objective 分摊到官方 / 口碑两个 goal——它们讲的就是豆包，⛔ 不是对照基线
    assert _entities_of(plan, "goal-1") == {"豆包"} and _entities_of(plan, "goal-2") == {"豆包"}
    assert baseline == []


def test_goal提示词_溢出的基线卡有一句非本goal实体说明_主角goal没有() -> None:
    baseline: list[dict[str, str]] = []
    skeleton = _skeleton()
    plan = collection_plan_dict(_run(FAST, "fast", baseline=baseline))
    scaffolds = _scaffolds(skeleton)
    prompt_2 = _goal_prompt(
        QUERY, "goal-2", scaffolds[1], [], subjects=skeleton["subjects"], entities=_cards(),
        market_profile="cn_product", scale="fast", collection_slots=plan["goal-2"],
        baseline_slots=[b for b in baseline if b["goal_id"] == "goal-2"],
    )
    assert "主角对照基线卡" in prompt_2 and "微博数据抓取·豆包" in prompt_2
    prompt_1 = _goal_prompt(
        QUERY, "goal-1", scaffolds[0], [], subjects=skeleton["subjects"], entities=_cards(),
        market_profile="cn_product", scale="fast", collection_slots=plan["goal-1"],
        baseline_slots=[],
    )
    assert "主角对照基线卡" not in prompt_1
