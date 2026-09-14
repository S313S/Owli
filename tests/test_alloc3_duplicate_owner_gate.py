"""§ALLOC-3 货 3：D-061 删卡闸补一口——同一对建在两个 goal、表里那个 goal 也建了且不在另一个的上游 ⇒ 删另一个。

病根（standard 规划期小跑 `r-alloc3-0914-a`）：分配表按性质分对了（goal-2 口碑 = 小红书/抖音/微博·豆包，
goal-3 媒体 = 公众号·豆包），但 goal-1「产品与定位基础」先起草、看不到后面 goal 的表，自加了
网页搜索/公众号/微博·豆包三张清单外卡。旧闸按全局 (源, 实体) 匹配：网页搜索删了，微博/公众号那两对
在表里就留下；规则 21 再「保先出现者」让 goal-2/goal-3 删掉自己的卡——最终计划里两张卡全落在 goal-1。

夹具是真的：`r-alloc3-0914-a` 的 skeleton / allocation / entity-1 / assembled（assembled 是规则 21 打回
之后的最终组装，即「两张卡在 goal-1、goal-2/3 没有」的形态）。首稿形态（goal-2/3 各自照表建了卡）
按 events 里规则 21 那条原文补回两张卡构造。`normalize_golden_d6e390e.json` 是**改前代码**在 d061 / alloc2 /
d062fu / d062fu-run-a（fast）与 d065（standard）上的修正说明 + 计划 sha256，逐字锁住不退化。
用户 2026-09-14 拍甲（经调度）是「一律按 goal 删、表里那个 goal 没建交规则 31 打回」；施工实测一律按 goal 删
打红 19 条整链用例（挪了 goal 的卡被删、重生时上游清单仍叫它「禁止重复」，三轮收不敛整份计划作废），
本终端收窄为本文件首行口径并呈调度追认。
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from app.plan.generate import _build_plan
from app.plan.lint import lint
from app.plan.model import Plan
from app.plan.normalize import empty_goal_removal, normalize_plan

FIXTURES = Path(__file__).parent / "fixtures"
RUN = FIXTURES / "alloc3-run"
GOLDEN = json.loads((RUN / "normalize_golden_d6e390e.json").read_text(encoding="utf-8"))


def _build(name: str, scale: str, assembled: dict | None = None) -> Plan:
    folder = FIXTURES / name
    skeleton = json.loads((folder / "skeleton.json").read_text(encoding="utf-8"))
    cards = []
    for index in range(1, 5):
        path = folder / f"entity-{index}.json"
        if path.exists():
            card = json.loads(path.read_text(encoding="utf-8"))
            card.setdefault("id", card["canonical"])  # 生产里 resolve_entities 填的就是 canonical
            cards.append(card)
    return _build_plan(
        assembled or json.loads((folder / "assembled.json").read_text(encoding="utf-8")),
        query="国内大家对豆包的看法",
        research_id=f"r-{name}",
        timestamp="2026-09-14T00:00:00+00:00",
        scale=scale,
        market_profile=skeleton["market_profile"],
        market_profile_justification=skeleton["market_profile_justification"],
        subjects=skeleton["subjects"],
        subjects_justification=skeleton["subjects_justification"],
        entities=cards,
        repairs=[],
    )


def _allocation(name: str = "alloc3-run") -> dict:
    return json.loads((FIXTURES / name / "allocation.json").read_text(encoding="utf-8"))


def _cards(plan: Plan) -> list[tuple[str, str, str]]:
    return sorted(
        (goal.goal_id, str((agent.capability.get("sources") or [""])[0]), str(agent.entity or "").strip())
        for goal in plan.goals for agent in goal.agents
        if agent.capability.get("profile") == "web-collector" and agent.entity
    )


def _table_triples(allocation: dict) -> list[tuple[str, str, str]]:
    return sorted(
        (goal_id, str(slot["source_id"]), str(slot["entity"]).strip())
        for goal_id, slots in allocation.items() for slot in slots
    )


def _first_draft() -> dict:
    """首稿形态：goal-2 照表建了微博·豆包、goal-3 照表建了公众号·豆包（events 规则 21 原文为证）。"""
    assembled = json.loads((RUN / "assembled.json").read_text(encoding="utf-8"))
    goal_1 = assembled["goals"][0]["agents"]
    weibo = copy.deepcopy(next(a for a in goal_1 if a["name"] == "微博数据抓取·豆包"))
    wechat = copy.deepcopy(next(a for a in goal_1 if a["name"] == "微信公众号数据抓取·豆包"))
    weibo["task"] = "在微博采集普通用户对「豆包」的使用评价与吐槽原声，顶层输出数组，每条含 permalink、fetched_at。"
    wechat["task"] = "从微信公众号采集科技媒体与分析师关于「豆包」的评测长文，顶层输出数组，每条含 permalink、fetched_at。"
    assembled["goals"][1]["agents"].insert(2, weibo)
    assembled["goals"][2]["agents"].insert(0, wechat)
    return assembled


def test_造红_真计划里微博与公众号豆包卡落在goal1_表里归goal2与goal3() -> None:
    plan = _build("alloc3-run", "standard")
    cards = _cards(plan)
    assert ("goal-1", "weibo", "豆包") in cards and ("goal-1", "wechat_mp", "豆包") in cards
    owners = {(s, e): g for g, s, e in _table_triples(_allocation())}
    assert owners[("weibo", "豆包")] == "goal-2" and owners[("wechat_mp", "豆包")] == "goal-3"
    # 改前的删卡说明（golden 同口径量出）只删了表外的网页搜索，这两张一句没提
    assert cards != _table_triples(_allocation()), "红：最终计划卡与分配表逐 goal 不等"


def test_终稿形态_表里那个goal没建_不动_允许挪goal照旧() -> None:
    """规则 21 打回之后的形态：两张卡只在 goal-1。表里那个 goal 没建就不删——这一步救不回来，要靠首稿那一步。"""
    plan = _build("alloc3-run", "standard")
    notes = normalize_plan(plan, collection_plan=_allocation())
    assert not [n for n in notes if "留表里那个 goal 的卡" in n], notes
    assert ("goal-1", "weibo", "豆包") in _cards(plan) and ("goal-1", "wechat_mp", "豆包") in _cards(plan)


def test_绿_首稿形态_每张卡的goal与分配表逐goal相等_规则21与31都不报() -> None:
    plan = _build("alloc3-run", "standard", _first_draft())
    before = _cards(plan)
    assert ("goal-1", "weibo", "豆包") in before and ("goal-2", "weibo", "豆包") in before
    notes = normalize_plan(plan, collection_plan=_allocation())
    kept = [n for n in notes if "留表里那个 goal 的卡" in n]
    assert len(kept) == 2, notes
    assert any("goal-1/" in n and "weibo·豆包" in n and "归 goal-2" in n for n in kept)
    assert any("goal-1/" in n and "wechat_mp·豆包" in n and "归 goal-3" in n for n in kept)
    assert _cards(plan) == _table_triples(_allocation())
    errors = lint(plan, collection_plan=_allocation())["errors"]
    assert not [e for e in errors if e.startswith(("[规则21]", "[规则31]"))], errors


def test_删光一个goal时_空goal移出排在归错goal删卡之后() -> None:
    """顺序：删卡（含归错 goal）→ D-065 移出空 goal。一个 goal 只有归错的卡、没有别的章时整条移出。"""
    assembled = _first_draft()
    assembled["goals"][2]["agents"] = [copy.deepcopy(assembled["goals"][1]["agents"][0])]  # goal-3 只剩一张小红书·豆包
    plan = _build("alloc3-run", "standard", assembled)
    notes = normalize_plan(plan, collection_plan=_allocation())
    wrong = next(i for i, n in enumerate(notes) if "goal-3/" in n and "留表里那个 goal 的卡" in n)
    removed = next(i for i, n in enumerate(notes) if (empty_goal_removal(n) or {}).get("goal_id") == "goal-3")
    assert wrong < removed
    assert "goal-3" not in {goal.goal_id for goal in plan.goals}


def test_表里那个goal在上游时不动_交规则21让下游改引用上游产物() -> None:
    """test_m3g 那条路：下游 goal 重复了上游表内卡，规则 21 让下游改 inputs 引用——闸不抢这一步。"""
    assembled = _first_draft()
    goal_4 = assembled["goals"][3]["agents"]  # goal-4 depends_on goal-2、goal-3
    goal_4.insert(0, copy.deepcopy(assembled["goals"][1]["agents"][0]))  # 小红书·豆包，表里归 goal-2
    plan = _build("alloc3-run", "standard", assembled)
    normalize_plan(plan, collection_plan=_allocation())
    assert ("goal-4", "xhs", "豆包") in _cards(plan)
    errors = lint(plan, collection_plan=_allocation())["errors"]
    assert any(e.startswith("[规则21]") and "goal-4/" in e for e in errors), errors


def test_幂等_再跑一遍不再删也不再留痕() -> None:
    plan = _build("alloc3-run", "standard", _first_draft())
    normalize_plan(plan, collection_plan=_allocation())
    assert [n for n in normalize_plan(plan, collection_plan=_allocation()) if n.startswith("[修正31]")] == []


@pytest.mark.parametrize("key", sorted(GOLDEN))
def test_不退化_既有夹具修正说明与计划和改前代码逐字相同(key: str) -> None:
    name, scale = key.split("/")
    if name == "d065":
        plan = Plan.from_dict(json.loads((FIXTURES / "d065" / "plan.json").read_text(encoding="utf-8")))
        allocation = json.loads((FIXTURES / "d065" / "plan-segments" / "allocation.json").read_text(encoding="utf-8"))
        notes = normalize_plan(plan, collection_plan=allocation)
    else:
        plan = _build(name, scale)
        notes = normalize_plan(plan, collection_plan=_allocation(name), per_goal_capacity=2)
    assert notes == GOLDEN[key]["notes"]
    digest = hashlib.sha256(
        json.dumps(plan.to_dict(), ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    assert digest == GOLDEN[key]["plan_sha256"]
