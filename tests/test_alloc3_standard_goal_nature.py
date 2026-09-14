"""§ALLOC-3：standard 下主角卡按 goal 性质分卡——口碑 goal 拿小红书/抖音/微博，媒体 goal 拿公众号。

病根（WX-1 规划期小跑 `r-wx1-0913-1355`）：ALLOC-2 的「同层按 goal 序填满再下一个」只在 fast
（每 goal 2 位）验过；standard 每 goal 位数无上限，一填就把八张豆包卡全塞进第一个点名豆包的
goal-1「产品画像」，goal-4「社媒与用户口碑」、goal-5「媒体与行业观察者评论」各 0 卡。

夹具是真的：`../Owli-wx1/var/wx1-run/runs/r-wx1-0913-1355/plan-segments` 另存到 tests/fixtures/alloc3
（原目录未动）。`golden_4df32c7.json` 是**改前代码**在四份真骨架 × 两档上的输出，用来逐字锁住
fast 与「不该变的 standard」。用户 2026-09-14 拍：A 甲（性质匹配分严格更高才挪）、B1（海外社区
四源留原处）、C（汇总类标题不作候选）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import load_research_scale_config
from app.plan.allocation import (
    INDUSTRY_VIEW_CUES, SOURCE_NATURE, allocate_collections, collection_plan_dict, nature_score,
)

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN = json.loads((FIXTURES / "alloc3" / "golden_4df32c7.json").read_text(encoding="utf-8"))
PROTAGONIST = {
    "alloc2": "豆包", "d062fu": "豆包", "d062fu-run-a": "豆包语音输入法", "alloc3": "豆包",
    "alloc3-run-b": "豆包",
}


def _load(name: str) -> tuple[dict, list[dict], list[dict]]:
    skeleton = json.loads((FIXTURES / name / "skeleton.json").read_text(encoding="utf-8"))
    cards = []
    for index in range(1, 5):
        path = FIXTURES / name / f"entity-{index}.json"
        if path.exists():
            card = json.loads(path.read_text(encoding="utf-8"))
            card.setdefault("id", card["canonical"])  # 生产里 resolve_entities 填的就是 canonical
            cards.append(card)
    scaffolds = [
        {"title": g["title"], "objective": g["objective"], "depends_on": g["depends_on"],
         "subjects": list(skeleton["subjects"])}
        for g in skeleton["goals"]
    ]
    return skeleton, cards, scaffolds


def _run(name: str, scale: str, scaffolds: list[dict] | None = None) -> dict:
    skeleton, cards, default_scaffolds = _load(name)
    baseline: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    plan = collection_plan_dict(allocate_collections(
        skeleton["subjects"], skeleton["market_profile"], scaffolds or default_scaffolds,
        load_research_scale_config().profile(scale), cards, scale=scale,
        entity_slot_target=len(skeleton["subjects"]), protagonists=[PROTAGONIST[name]],
        baseline=baseline, skipped=skipped,
    ))
    return {"plan": plan, "baseline": baseline, "skipped": skipped}


def _short(plan: dict) -> dict[str, list[str]]:
    return {g: [f"{s['source_id']}·{s['entity']}" for s in slots] for g, slots in plan.items()}


def _pairs(plan: dict) -> set[tuple[str, str]]:
    return {(s["source_id"], s["entity"]) for slots in plan.values() for s in slots}


def test_造红_旧表口碑goal与媒体goal零卡_新表口碑goal拿三张国内UGC_媒体goal拿公众号() -> None:
    old = json.loads((FIXTURES / "alloc3" / "allocation.json").read_text(encoding="utf-8"))
    assert old["goal-4"] == [] and old["goal-5"] == [], "红：小跑分配表口碑/媒体 goal 各 0 卡"
    assert GOLDEN["alloc3/standard"]["plan"] == old, "改前代码重算与小跑分配表逐字相同"

    new = _run("alloc3", "standard")
    assert _short(new["plan"]) == {
        "goal-1": ["reddit·豆包", "x·豆包", "hacker_news·豆包", "product_hunt·豆包"],
        "goal-2": ["xhs·字节跳动"],
        "goal-3": ["xhs·DeepSeek", "xhs·Kimi"],
        "goal-4": ["xhs·豆包", "douyin·豆包", "weibo·豆包"],
        "goal-5": ["wechat_mp·豆包"],
        "goal-6": [],
    }
    # 只挪不增删：(source, entity) 对与改前完全相同，D-061 闸按全局对匹配不受影响
    assert _pairs(new["plan"]) == _pairs(old)
    assert new["baseline"] == [] and new["skipped"] == []


@pytest.mark.parametrize("key", ["alloc2/fast", "d062fu/fast", "d062fu-run-a/fast", "alloc3/fast"])
def test_fast不退化_四份真骨架输出与改前代码逐字相同(key: str) -> None:
    name, scale = key.split("/")
    assert _run(name, scale) == GOLDEN[key]


@pytest.mark.parametrize("key", ["alloc2/standard", "d062fu/standard", "d062fu-run-a/standard"])
def test_standard只有一个可投goal或同分时_输出与改前代码逐字相同(key: str) -> None:
    """alloc2/d062fu 只有 goal-1 点名主角；输入法题研判 goal「用户体验」与 goal-1「用户口碑」同分。"""
    name, scale = key.split("/")
    assert _run(name, scale) == GOLDEN[key]


def test_B1_海外社区四源不参与性质投递_留在第一个点名主角的goal() -> None:
    assert not {"reddit", "x", "hacker_news", "product_hunt", "web_search"} & set(SOURCE_NATURE)
    plan = _run("alloc3", "standard")["plan"]
    assert {s["source_id"] for s in plan["goal-1"]} == {"reddit", "x", "hacker_news", "product_hunt"}


def test_C_汇总类标题的goal不作候选_即使标题写了口碑() -> None:
    _skeleton, _cards, scaffolds = _load("alloc3")
    scaffolds[3] = {**scaffolds[3], "title": "国内社媒与用户口碑对豆包的综合研判"}
    plan = _run("alloc3", "standard", scaffolds)["plan"]
    assert plan["goal-4"] == [] and plan["goal-6"] == []
    # 口碑卡退到剩余候选里分最高的：goal-5 objective「公开评价」记 1 > goal-1 记 0
    assert {s["source_id"] for s in plan["goal-5"]} == {"xhs", "douyin", "weibo", "wechat_mp"}


def test_A甲_只有严格更高才挪_标题不点名别的实体_竞品goal不收主角卡() -> None:
    _skeleton, _cards, scaffolds = _load("alloc3")
    # goal-3 objective 写了「口碑标签」且 objective 点名豆包——但标题点名 DeepSeek/Kimi，不作候选
    assert nature_score(scaffolds[2], "xhs") == 1
    assert {s["entity"] for s in _run("alloc3", "standard")["plan"]["goal-3"]} == {"DeepSeek", "Kimi"}
    # 口碑 goal 标题改成不带性质词：objective 点名微博/小红书/抖音记 1 > goal-1 的 0，照样挪
    scaffolds[3] = {**scaffolds[3], "title": "国内普通人怎么看豆包"}
    plan = _run("alloc3", "standard", scaffolds)["plan"]
    assert {s["source_id"] for s in plan["goal-4"]} == {"xhs", "douyin", "weibo"}
    # goal-1 objective 也写上「口碑」，与 goal-4 同分 ⇒ 留原处
    scaffolds[0] = {**scaffolds[0], "objective": scaffolds[0]["objective"] + "兼顾口碑。"}
    plan = _run("alloc3", "standard", scaffolds)["plan"]
    assert plan["goal-4"] == []
    assert {"xhs", "douyin", "weibo"} <= {s["source_id"] for s in plan["goal-1"]}


def test_候选同分时取性质更纯的_媒体评价goal不抢口碑goal的卡() -> None:
    """小跑 r-alloc3-0914-b：goal-2「国内媒体与行业侧对豆包的评价」口碑词「评价」+ 媒体词都记 2，
    goal-3「国内普通用户在社交与内容平台的口碑」只有口碑 2。旧 tie-break 按 goal 序，口碑 goal 零卡。"""
    old = json.loads((FIXTURES / "alloc3-run-b" / "allocation.json").read_text(encoding="utf-8"))
    assert old["goal-3"] == [] and len(old["goal-2"]) == 4, "红：小跑分配表口碑 goal 零卡、媒体 goal 四张"
    new = _run("alloc3-run-b", "standard")
    assert _short(new["plan"]) == {
        "goal-1": ["reddit·豆包", "x·豆包", "hacker_news·豆包", "product_hunt·豆包"],
        "goal-2": ["wechat_mp·豆包"],
        "goal-3": ["xhs·豆包", "douyin·豆包", "weibo·豆包"],
        "goal-4": [],
        "goal-5": [],
    }
    assert _pairs(new["plan"]) == _pairs(old)


def test_打分_标题2_只有objective1_平台名也算_媒体词命中公众号() -> None:
    goal = {"title": "国内媒体与行业观察者对豆包的评论采集", "objective": "采集科技媒体长文。"}
    assert nature_score(goal, "wechat_mp") == 2
    assert nature_score(goal, "xhs") == 0
    assert nature_score({"title": "豆包", "objective": "在小红书上看看"}, "xhs") == 1
    assert nature_score(goal, "reddit") == 0
    assert "公众号" in INDUSTRY_VIEW_CUES


def test_源性质表与证据行写死的content_kind一致() -> None:
    """性质词沿用证据行的 content_kind，不另起名目；这里守两边别漂。"""
    from app.precollect import PLATFORM_PROFILES
    from app.store.dao import _CONTENT_KINDS

    assert set(SOURCE_NATURE.values()) <= set(_CONTENT_KINDS)
    for source in ("weibo", "wechat_mp"):
        assert PLATFORM_PROFILES[source].content_kind == SOURCE_NATURE[source]
    root = Path(__file__).resolve().parents[1] / "app" / "sources"
    for source in ("xhs", "douyin"):
        text = (root / f"{source}.py").read_text(encoding="utf-8")
        assert f'"content_kind": "{SOURCE_NATURE[source]}"' in text
