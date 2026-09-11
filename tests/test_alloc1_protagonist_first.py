"""§ALLOC-1：采集分配表——主角先占各主源，竞品再同源排位。

背景（2026-09-11 实测）：`r-045acebc352b` 的抖音 107 条全是用 Kimi 的词搜的，因为旧分配
表按 `position % len(sources)` 轮转，Kimi 恰好排到抖音；主角只因为有两个叫法才拿到
两个源。用户拍甲：fast 下主角占 小红书 + 微博 + 抖音 3 位 + Reddit 1 位，竞品 2 位，
不扩容、不动跨语域那套。
"""

from __future__ import annotations

from app.config import load_research_scale_config
from app.plan.allocation import (
    allocate_collections, collection_plan_dict, protagonist_home_slots,
)

FAST = load_research_scale_config().profile("fast")
STANDARD = load_research_scale_config().profile("standard")
SCAFFOLDS = [{"depends_on": []}, {"depends_on": ["goal-1"]}, {"depends_on": ["goal-2"]}]


def _card(cid: str, zh: str | None, en: str | None, aliases: list[str] | None = None,
          canonical: str | None = None) -> dict:
    return {"id": cid, "canonical": canonical or cid,
            "names": {"zh": zh, "en": en, "aliases": aliases or []}, "same_product": True}


DOUBAO = _card("豆包", "豆包", "Doubao", ["豆包AI", "豆包大模型"])
KIMI = _card("Kimi", "Kimi", "Kimi", ["月之暗面"])
DEEPSEEK = _card("DeepSeek", "深度求索", "DeepSeek")
ERNIE = _card("文心一言", "文心一言", "ERNIE Bot")
MODERN = ["豆包", "Kimi", "DeepSeek"]
CARDS = [DOUBAO, KIMI, DEEPSEEK]


def _pairs(plan) -> set[tuple[str, str]]:
    return {(s.source_id, s.entity) for slots in plan.values() for s in slots}


def _by_entity(plan, entity: str) -> set[str]:
    return {s for s, e in _pairs(plan) if e == entity}


def test_造红_旧路径下抖音落在竞品名下_主角只因两个叫法才多占源() -> None:
    legacy = ["豆包", "Doubao", "DeepSeek", "Kimi", "文心一言"]
    cards = [DOUBAO, _card("Doubao", "豆包", "Doubao", ["字节豆包"], canonical="豆包"), DEEPSEEK, KIMI, ERNIE]
    plan = allocate_collections(legacy, "cn_product", SCAFFOLDS, FAST, cards,
                                scale="fast", entity_slot_target=5)
    assert ("douyin", "Kimi") in _pairs(plan), "红：抖音归 Kimi（2026-09-04 整跑实况）"
    assert _by_entity(plan, "豆包") == {"xhs", "reddit"} and _by_entity(plan, "Doubao") == {"weibo"}
    # 如今的 3 主体 fast 计划更糟：主角三跨语域位把抖音整个挤出分配表
    plan = allocate_collections(MODERN, "cn_product", SCAFFOLDS, FAST, CARDS,
                                scale="fast", entity_slot_target=3)
    assert "douyin" not in {s for s, _ in _pairs(plan)}


def test_甲_fast主角占小红书微博抖音加Reddit_竞品各一位同落小红书_容量不超() -> None:
    skipped: list[dict[str, str]] = []
    plan = allocate_collections(MODERN, "cn_product", SCAFFOLDS, FAST, CARDS,
                                scale="fast", entity_slot_target=3, skipped=skipped,
                                protagonists=["豆包"])
    assert _by_entity(plan, "豆包") == {"xhs", "weibo", "douyin", "reddit"}
    assert _by_entity(plan, "Kimi") == {"xhs"} and _by_entity(plan, "DeepSeek") == {"xhs"}
    assert len(_pairs(plan)) == 6 == sum(len(v) for v in plan.values())
    assert all(len(slots) <= 2 for slots in plan.values())
    assert all(len({s.source_id for s in slots}) <= 2 for slots in plan.values())
    assert "wechat_mp" not in {s for s, _ in _pairs(plan)}, "公众号是死位，谁都不占"
    assert not [item for item in skipped if item["reason"] == "protagonist_first"]


def test_甲_旧快照五主体_文心一言让位记账不抛错_别名subject不占位() -> None:
    legacy = ["豆包", "Doubao", "DeepSeek", "Kimi", "文心一言"]
    cards = [DOUBAO, _card("Doubao", "豆包", "Doubao", ["字节豆包"], canonical="豆包"), DEEPSEEK, KIMI, ERNIE]
    skipped: list[dict[str, str]] = []
    plan = allocate_collections(legacy, "cn_product", SCAFFOLDS, FAST, cards, scale="fast",
                                entity_slot_target=5, skipped=skipped, protagonists=["豆包"])
    assert _by_entity(plan, "豆包") == {"xhs", "weibo", "douyin", "reddit"}
    assert _by_entity(plan, "文心一言") == set() and _by_entity(plan, "Doubao") == set()
    reasons = {(item["entity"], item["reason"]) for item in skipped}
    assert ("文心一言", "protagonist_first") in reasons
    assert ("Doubao", "same_protagonist") in reasons


def test_判据四_跨语域读数不变_standard主角占全部本语域主源且对面四源照旧() -> None:
    plan = allocate_collections(MODERN, "cn_product", SCAFFOLDS, STANDARD, CARDS,
                                scale="standard", entity_slot_target=3, protagonists=["豆包"])
    got = _by_entity(plan, "豆包")
    assert got & {"reddit", "x", "hacker_news", "product_hunt"} == {"reddit", "x", "hacker_news", "product_hunt"}
    assert got & {"xhs", "weibo", "douyin", "wechat_mp"} == {"xhs", "weibo", "douyin", "wechat_mp"}
    assert protagonist_home_slots(STANDARD, scale="standard") is None
    assert protagonist_home_slots(FAST, scale="fast") == 3


def test_退回旧行为_没主角或两个主角或主角本语域无叫法或无实体卡() -> None:
    legacy = collection_plan_dict(allocate_collections(
        MODERN, "cn_product", SCAFFOLDS, FAST, CARDS, scale="fast", entity_slot_target=3))
    for protagonists in (None, [], ["文心一言"], ["豆包", "Kimi"]):
        got = collection_plan_dict(allocate_collections(
            MODERN, "cn_product", SCAFFOLDS, FAST, CARDS, scale="fast",
            entity_slot_target=3, protagonists=protagonists))
        assert got == legacy, protagonists
    en_only = [_card("Doubao", None, "Doubao"), KIMI, DEEPSEEK]
    assert collection_plan_dict(allocate_collections(
        ["Doubao", "Kimi", "DeepSeek"], "cn_product", SCAFFOLDS, FAST, en_only,
        scale="fast", entity_slot_target=3, protagonists=["Doubao"],
    )) == collection_plan_dict(allocate_collections(
        ["Doubao", "Kimi", "DeepSeek"], "cn_product", SCAFFOLDS, FAST, en_only,
        scale="fast", entity_slot_target=3))
    no_cards = collection_plan_dict(allocate_collections(
        MODERN, "cn_product", SCAFFOLDS, FAST, None, scale="fast", protagonists=["豆包"]))
    assert no_cards == collection_plan_dict(allocate_collections(
        MODERN, "cn_product", SCAFFOLDS, FAST, None, scale="fast"))


def test_主角只有中文名时不留跨语域位_竞品照样各一位() -> None:
    tea = ["小罐茶", "八马茶业", "大益"]
    cards = [_card(n, n, None) for n in tea]
    plan = allocate_collections(tea, "cn_product", SCAFFOLDS, FAST, cards, scale="fast",
                                entity_slot_target=3, protagonists=["小罐茶"])
    assert _by_entity(plan, "小罐茶") == {"xhs", "weibo", "douyin"}
    assert _by_entity(plan, "八马茶业") == {"xhs"} and _by_entity(plan, "大益") == {"xhs"}


def test_生成链从题面推主角_点不出时为空() -> None:
    from app.plan.generate import _protagonists

    assert _protagonists("国内大家对豆包的看法", MODERN, CARDS) == ["豆包"]
    assert _protagonists("大家对 Kimi 的看法", MODERN, CARDS) == ["Kimi"], "跟题面走，不跟列表顺序走"
    assert _protagonists("国产 AI 助手口碑如何", MODERN, CARDS) == []
