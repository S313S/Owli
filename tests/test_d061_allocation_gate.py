"""§D-061：分配表在起草层是「声明」不是「闸」——表外采集卡要在机械修正里删掉。

病根：lint 规则 31 只查「表⊆计划」（表里每一对都必须落成卡），**没查反过来**
（计划⊆表）。所以引擎多起草的那张卡一条规则都不违反，静默通过，最后真去采了数
——微博是读池薄源，命中口径宽，采回来的一多半是别家旧批次的语料，却被当成
这个实体的证据入库。

夹具是真的：`r-e9760470f3e0`（fast）那一轮的 plan-segments 原样拷进 tests/fixtures/d061。
⚠️ 必须走 `assembled.json → _build_plan → normalize_plan` 整条——`goal-N.json` 里每张卡
只有 name/task/output，entity 与 sources 是 `_build_plan` 之后才从卡名派生的，
拿 json 直接比对量不出这个缺陷。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.plan.generate import _build_plan
from app.plan.model import Plan
from app.plan.normalize import normalize_plan
from tests.plan_factory import attach_rating_agents, make_agent, make_plan_dict

FIXTURES = Path(__file__).parent / "fixtures" / "d061"


def _entities() -> list[dict]:
    cards = []
    for index in (1, 2, 3):
        card = json.loads((FIXTURES / f"entity-{index}.json").read_text(encoding="utf-8"))
        # 生产里 `resolve_entities` 填的就是这个：id 取实体的规范名。
        card.setdefault("id", card["canonical"])
        cards.append(card)
    return cards


def _fixture_plan() -> Plan:
    skeleton = json.loads((FIXTURES / "skeleton.json").read_text(encoding="utf-8"))
    assembled = json.loads((FIXTURES / "assembled.json").read_text(encoding="utf-8"))
    return _build_plan(
        assembled,
        query="国内大家对豆包的看法",
        research_id="r-e9760470f3e0",
        timestamp="2026-09-12T04:51:00+00:00",
        scale="fast",
        market_profile=skeleton["market_profile"],
        market_profile_justification=skeleton["market_profile_justification"],
        subjects=skeleton["subjects"],
        subjects_justification=skeleton["subjects_justification"],
        entities=_entities(),
        repairs=[],
    )


def _allocation() -> dict:
    return json.loads((FIXTURES / "allocation.json").read_text(encoding="utf-8"))


def _cards(plan: Plan) -> list[tuple[str, str, str]]:
    """计划里的采集卡：(goal_id, source_id, entity)。"""
    return [
        (goal.goal_id, str((agent.capability.get("sources") or [""])[0]),
         str(agent.entity or "").strip())
        for goal in plan.goals for agent in goal.agents
        if agent.capability.get("profile") == "web-collector"
    ]


def _table(allocation: dict) -> set[tuple[str, str]]:
    return {(str(slot["source_id"]), str(slot["entity"]).strip())
            for slots in allocation.values() for slot in slots}


# —— 判据 1：造红 / 转绿 ————————————————————————————————


def test_红_不给分配表时还是七张卡():
    """老行为的锚：不传表就是从前那样，7 张卡照过——这正是 09-12 实跑的现场。"""
    plan = _fixture_plan()
    normalize_plan(plan)
    cards = _cards(plan)
    assert len(cards) == 7
    assert ("goal-1", "weibo", "DeepSeek") in cards, "表外那张就是它"


def test_绿_给了分配表就删成六张且与表逐对相等():
    """判据 1。注意比的是**集合相等**不是逐 goal 比对——规则 31 明写允许挪 goal。"""
    plan = _fixture_plan()
    allocation = _allocation()
    normalize_plan(plan, collection_plan=allocation)
    cards = _cards(plan)
    assert len(cards) == 6
    assert {(source, entity) for _, source, entity in cards} == _table(allocation)


def test_删了哪张为什么_写进修正说明():
    """判据 2 的库内读数靠它：说明进 events，得说得出卡、源、实体。"""
    plan = _fixture_plan()
    notes = normalize_plan(plan, collection_plan=_allocation())
    dropped = [n for n in notes if "不在分配表里" in n]
    assert len(dropped) == 1, notes
    assert "weibo" in dropped[0] and "DeepSeek" in dropped[0] and "goal-1" in dropped[0]
    # 连带收口也要留痕——删一张卡会带走它的评级章、改掉报告章的依赖，
    # 这两件事都是范围变化，不许静默（本模块开篇那条「防止静默缩范围」）。
    assert any("再没有输入" in n and "reliability-audit-3" in n for n in notes), notes
    assert any("depends_on 摘掉了已删卡" in n for n in notes), notes


def test_每goal采集位不超档位容量():
    """fast 每 goal 容量 2。删完表外卡，goal-1 正好落回 2。"""
    plan = _fixture_plan()
    normalize_plan(plan, collection_plan=_allocation(), per_goal_capacity=2)
    counts: dict[str, int] = {}
    for goal_id, _, _ in _cards(plan):
        counts[goal_id] = counts.get(goal_id, 0) + 1
    assert counts == {"goal-1": 2, "goal-2": 2, "goal-3": 2}


def test_幂等_再跑一遍不再删也不再留痕():
    plan = _fixture_plan()
    allocation = _allocation()
    normalize_plan(plan, collection_plan=allocation)
    assert [n for n in normalize_plan(plan, collection_plan=allocation)
            if n.startswith("[修正31]")] == []


# —— 顺序：删卡必须排在规则 26 之前（调度 09-12 点名写成用例）——————————


def test_删卡排在规则26之前_不留悬空inputs():
    """反过来的话，规则 26 会先给报告章补一条指向**被删卡产物**的 inputs。

    实测现场就是这么发生的：那一轮的三条修正事件里，DeepSeek 补的正是
    `goals/goal-1/data-collection-3.json`——表外那张卡的产物。表外卡不但没被删，
    下游还把它当合法卡接纳了。所以这条不是假想，是复现。
    """
    plan = _fixture_plan()
    deleted_paths = {
        str(agent.output.get("path", ""))
        for goal in plan.goals for agent in goal.agents
        if agent.capability.get("profile") == "web-collector"
        and (str((agent.capability.get("sources") or [""])[0]),
             str(agent.entity or "").strip()) not in _table(_allocation())
    }
    assert deleted_paths, "夹具里本该有一张要被删的卡"
    normalize_plan(plan, collection_plan=_allocation())
    for goal in plan.goals:
        for agent in goal.agents:
            chapter = agent.chapter
            if not isinstance(chapter, dict):
                continue
            inputs = (chapter.get("opening") or {}).get("inputs") or []
            for item in inputs:
                assert str(item.get("path", "")) not in deleted_paths, \
                    f"{goal.goal_id}/{agent.agent_id} 的 inputs 指向了被删卡的产物"


# —— 口径：全局匹配，允许挪 goal ————————————————————————


def _tiny_plan(cards: list[tuple[str, str, str]]) -> Plan:
    """最小计划：按 (goal_id, source_id, entity) 造采集卡。

    ⛔ 不自己拼 Plan 字典——`tests/plan_factory` 早就有一份合法骨架，自己拼会
    跟字段表悄悄漂移（造红时第一版就漏了 query 字段，报的是「字段表之外的字段」）。
    """
    raw = make_plan_dict()
    buckets: dict[str, list[dict]] = {"goal-1": [], "goal-2": [], "goal-3": []}
    for index, (goal_id, source, entity) in enumerate(cards, start=1):
        agent = make_agent(f"data-collection-{index}", goal_id)
        agent["display_name"] = f"{source}·{entity}" if entity else source
        # 模型层「没有实体」是 null 不是空串（`entity 必须是非空字符串或 null`）。
        agent["entity"] = entity or None
        agent["capability"] = {**agent["capability"], "profile": "web-collector",
                               "sources": [source]}
        agent["output"] = {**agent["output"], "format": "json", "shape": "array",
                           "path": f"goals/{goal_id}/data-collection-{index}.json"}
        agent["chapter"] = {
            **agent["chapter"], "chapter_type": "collection",
            "closing": {**agent["chapter"]["closing"],
                        "output": {"path": agent["output"]["path"]},
                        "entities": [entity] if entity else []},
        }
        buckets[goal_id].append(agent)
    for index, goal_id in enumerate(("goal-1", "goal-2", "goal-3")):
        raw["goals"][index]["agents"] = buckets[goal_id] or [make_agent(f"agent-{index+1}", goal_id)]
    attach_rating_agents(raw)
    return Plan.from_dict(raw)


def test_挪过goal的卡不许删():
    """规则 31 明写「允许挪 goal，不许丢」。删卡按 goal 内比对就会误伤它。"""
    allocation = {"goal-1": [{"entity": "豆包", "source_id": "xhs",
                              "collector_name": "小红书数据抓取"}], "goal-2": [], "goal-3": []}
    plan = _tiny_plan([("goal-2", "xhs", "豆包")])     # 表说 goal-1，引擎放在 goal-2
    notes = normalize_plan(plan, collection_plan=allocation)
    assert [n for n in notes if n.startswith("[修正31]")] == [], "挪 goal 是允许的"
    assert len(_cards(plan)) == 1


def test_同一对起草两张_容量闸去重():
    """两张都「在表里」，但位数会超容量——重复卡只留一张。"""
    allocation = {"goal-1": [{"entity": "豆包", "source_id": "xhs",
                              "collector_name": "小红书数据抓取"}], "goal-2": [], "goal-3": []}
    plan = _tiny_plan([("goal-1", "xhs", "豆包"), ("goal-1", "xhs", "豆包")])
    notes = normalize_plan(plan, collection_plan=allocation, per_goal_capacity=2)
    assert len(_cards(plan)) == 1
    assert any("重复" in n for n in notes), notes


def test_超容量按序截断并留痕():
    allocation = {"goal-1": [{"entity": e, "source_id": s, "collector_name": "c"}
                             for s, e in (("xhs", "豆包"), ("weibo", "豆包"),
                                          ("douyin", "豆包"))], "goal-2": [], "goal-3": []}
    plan = _tiny_plan([("goal-1", "xhs", "豆包"), ("goal-1", "weibo", "豆包"),
                       ("goal-1", "douyin", "豆包")])
    notes = normalize_plan(plan, collection_plan=allocation, per_goal_capacity=2)
    assert len(_cards(plan)) == 2
    assert any("容量" in n for n in notes), notes


# —— 保护既有调用方：不传表就是老行为 ————————————————————


def test_不传分配表时一张都不删():
    plan = _tiny_plan([("goal-1", "xhs", "豆包"), ("goal-2", "weibo", "Kimi")])
    assert normalize_plan(plan) == []
    assert len(_cards(plan)) == 2


@pytest.mark.parametrize("empty", [None, {}])
def test_空表当没给_不许把卡删光(empty):
    """空表是「没给」不是「表里一张都没有」——按后者理解会把整个计划删空。"""
    plan = _tiny_plan([("goal-1", "xhs", "豆包")])
    assert normalize_plan(plan, collection_plan=empty) == []
    assert len(_cards(plan)) == 1


def test_不带实体的通用采集卡不归这道闸管():
    """「API 数据抓取」这类职能名判不出实体，`entity` 是空串。

    分配表的键是「哪个实体用哪个源」，一张没有实体的卡根本不构成这样的键，
    谈不上在不在表里。照删的话，四预设档里的通用采集职能会被整个抹掉——
    接生产链路时 `test_路由表逐项与四预设档映射` 就是这么红的（goal-1 被删空、
    `plan.goals[0].agents[0]` 当场 IndexError）。
    """
    allocation = {"goal-1": [{"entity": "豆包", "source_id": "xhs",
                              "collector_name": "小红书数据抓取"}], "goal-2": [], "goal-3": []}
    plan = _tiny_plan([("goal-1", "xhs", "豆包"), ("goal-1", "web_search", "")])
    notes = normalize_plan(plan, collection_plan=allocation, per_goal_capacity=2)
    assert [n for n in notes if n.startswith("[修正31]")] == [], notes
    assert len(_cards(plan)) == 2


def test_通用采集卡不占容量():
    """⛔ 它也不能计进容量——否则会把表里真排了位的卡挤掉，

    规则 31 当场报「分配的采集对未落实」，计划连生都生不出来。
    """
    allocation = {"goal-1": [{"entity": e, "source_id": s, "collector_name": "c"}
                             for s, e in (("xhs", "豆包"), ("weibo", "豆包"))],
                  "goal-2": [], "goal-3": []}
    plan = _tiny_plan([("goal-1", "web_search", ""), ("goal-1", "xhs", "豆包"),
                       ("goal-1", "weibo", "豆包")])
    normalize_plan(plan, collection_plan=allocation, per_goal_capacity=2)
    assert {(s, e) for _, s, e in _cards(plan)} == {("web_search", ""), ("xhs", "豆包"),
                                                    ("weibo", "豆包")}
