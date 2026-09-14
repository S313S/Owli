"""§D-068：采集卡被删光的 goal，验收条仍要「本 goal 采集产物」——D-067 货 2 的「补采」词判漏网，改结构判。

病根（WX-1 第三轮 standard 小跑 `r-wx1-0914-1425`）：goal-5 自带「reddit·字节跳动」「x·字节跳动」、goal-6 自带
「hacker_news·字节跳动」，全是表外卡被删光；goal-5 验收条却写「本 goal 新增 2 条采集 output.path，共计 16 条」
「新增采集章的组合仅为「reddit·字节跳动」…」「每份新增采集 JSON 顶层为数组」，goal-6 写「本 goal 新增采集章的
组合与上游及必采清单均不重复」。一个「补采」都没有，D-067 货 2 放行；WX-1 判据⑦拿生产 `_asks_backfill` 当尺子，同盲报绿。

夹具是真的：`tests/fixtures/d068/wx1-run-0914-1425` 是那一轮 plan-segments 原样拷贝（源目录只读）。
`normalize_golden_9801396.json` 是**改前代码**（main 9801396）在这格上的修正说明、去 prompt 计划 sha 与各 goal 最终验收条。
独立尺子（不调 normalize 判定函数）在 `scripts/acceptance/d068/d068_hollow_ruler.py`，旧码读数 `ruler_reading_9801396.json`。
fast 四份 / d065 / alloc3 小跑 b / D-067 两格的不退化由既有 golden 用例逐字锁着。
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from app.plan.generate import _build_plan
from app.plan.lint import lint
from app.plan.model import Plan
from app.plan.normalize import _asks_own_collection, normalize_plan

FIXTURES = Path(__file__).parent / "fixtures" / "d068"
RUN = FIXTURES / "wx1-run-0914-1425"
GOLDEN = json.loads((FIXTURES / "normalize_golden_9801396.json").read_text(encoding="utf-8"))["wx1-run-0914-1425/standard"]
RULER = json.loads((FIXTURES / "ruler_reading_9801396.json").read_text(encoding="utf-8"))
STALE = {"goal-5": [2, 3, 4, 5, 6], "goal-6": [6]}


def _assembled() -> dict:
    return json.loads((RUN / "assembled.json").read_text(encoding="utf-8"))


def _allocation() -> dict:
    return json.loads((RUN / "allocation.json").read_text(encoding="utf-8"))


def _build(assembled: dict | None = None) -> Plan:
    skeleton = json.loads((RUN / "skeleton.json").read_text(encoding="utf-8"))
    cards = []
    for path in sorted(RUN.glob("entity-*.json"), key=lambda p: int(p.stem.split("-")[1])):
        card = json.loads(path.read_text(encoding="utf-8"))
        card.setdefault("id", card["canonical"])  # 生产里 resolve_entities 填的就是 canonical
        cards.append(card)
    return _build_plan(
        assembled or _assembled(),
        query="国内大家对豆包的看法",
        research_id="r-wx1-run-0914-1425",
        timestamp="2026-09-14T00:00:00+00:00",
        scale="standard",
        market_profile=skeleton["market_profile"],
        market_profile_justification=skeleton["market_profile_justification"],
        subjects=skeleton["subjects"],
        subjects_justification=skeleton["subjects_justification"],
        entities=cards,
        repairs=[],
    )


def _without_prompts(value):
    if isinstance(value, dict):
        return {k: _without_prompts(v) for k, v in value.items() if k != "prompt"}
    if isinstance(value, list):
        return [_without_prompts(v) for v in value]
    return value


def _sha(plan: Plan) -> str:
    return hashlib.sha256(
        json.dumps(_without_prompts(plan.to_dict()), ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def _goal(plan: Plan, goal_id: str):
    return next(goal for goal in plan.goals if goal.goal_id == goal_id)


def _draft_acceptance() -> dict[str, list[str]]:
    return {f"goal-{i + 1}": goal["acceptance"] for i, goal in enumerate(_assembled()["goals"])}


def test_造红_改前代码_空心goal5与goal6仍留着要本goal采集产物的验收条() -> None:
    draft = _draft_acceptance()
    for goal_id, indexes in STALE.items():
        for index in indexes:
            assert draft[goal_id][index] in GOLDEN["acceptance"][goal_id], (goal_id, index)
        assert not [n for n in GOLDEN["notes"] if n.startswith(f"[修正4] {goal_id}.")]
    assert "本 goal 新增 2 条采集 output.path，共计 16 条" in draft["goal-5"][2]
    assert "「reddit·字节跳动」「x·字节跳动」" in draft["goal-5"][3]
    assert "新增采集章" in draft["goal-6"][6]
    # 独立尺子在旧码上量出非零
    assert RULER["target"]["hollow_goals"] == ["goal-5", "goal-6"]
    assert RULER["target"]["hollow_remaining_hit_items"] == 6


def test_绿_空心goal的过时验收条整条摘_逐条留痕带原文() -> None:
    draft = _draft_acceptance()
    plan = _build()
    notes = normalize_plan(plan, collection_plan=_allocation())
    for goal_id, indexes in STALE.items():
        assert _goal(plan, goal_id).acceptance == [
            text for index, text in enumerate(draft[goal_id]) if index not in indexes
        ]
        for index in indexes:
            note = next(n for n in notes if n.startswith(f"[修正4] {goal_id}.acceptance[{index}] 已摘："))
            assert note.endswith(f"——原文「{draft[goal_id][index]}」")
    slot = next(n for n in notes if n.startswith("[修正4] goal-5.acceptance[3] 已摘："))
    assert "点名的采集组合「reddit·字节跳动」「x·字节跳动」只在本 goal 已删的采集卡上" in slot
    count = next(n for n in notes if n.startswith("[修正4] goal-5.acceptance[2] 已摘："))
    assert "本 goal 自己的采集章（「新增 2 条采集」）" in count
    assert lint(plan, collection_plan=_allocation())["errors"] == []


def test_豁免与还有卡的goal_验收条与改前代码逐字相同() -> None:
    plan = _build()
    notes = normalize_plan(plan, collection_plan=_allocation())
    for goal_id in ("goal-1", "goal-2", "goal-3", "goal-4"):
        assert _goal(plan, goal_id).acceptance == GOLDEN["acceptance"][goal_id], goal_id
    draft = _draft_acceptance()
    assert draft["goal-6"][3] in _goal(plan, "goal-6").acceptance  # 「…或本 goal 采集数据的 permalink」
    assert draft["goal-1"][4] in _goal(plan, "goal-1").acceptance  # 「…后续补采建议」
    # 除空心 goal 的新摘条外，修正说明与改前逐字同；验收条放回改前的样子，计划 sha 与改前同
    ours = [n for n in notes if n.startswith(("[修正4] goal-5.", "[修正4] goal-6."))]
    assert [n for n in notes if n not in ours] == GOLDEN["notes"]
    for goal_id in STALE:
        _goal(plan, goal_id).acceptance = GOLDEN["acceptance"][goal_id]
    assert _sha(plan) == GOLDEN["plan_sha256"]


def test_主语判_只看前面的否定选择词_上游与缺口语境不算() -> None:
    hits = {
        "本 goal 新增采集章的 (source_id, entity) 组合与上游及必采清单均不重复": "本 goal 新增采集",
        "采集章 source_id 全部来自 applicable_sources 闭集": "采集章",
        "每份新增采集 JSON 顶层为数组": "新增采集",
        "覆盖上游 26 项产物加本 goal 2 项补采共 28 项": "补采",
    }
    for text, word in hits.items():
        assert _asks_own_collection(text) == word, text
    for text in (
        "所有事实性陈述均可回溯至上游产物 output.path 或本 goal 采集数据的 permalink",
        "证据台账不引用任何补采产物，只引用上游 goal 产物",
        "对单源结论在缺口说明节注明所需补采的来源与实体",
        "报告引用上游采集章的产物",
        "报告引用 goal-1 的采集章产物",
        "任何维度若源侧样本不足，均以结构化缺口口径注明缺口原因与后续补采建议，不得留空",
    ):
        assert _asks_own_collection(text) is None, text


def test_还有卡的goal_写新增采集章也不动() -> None:
    assembled = _assembled()
    clause = "本 goal 新增采集章的 (source_id, entity) 组合与上游均不重复"
    assembled["goals"][0]["acceptance"] = assembled["goals"][0]["acceptance"] + [clause]
    plan = _build(assembled)
    normalize_plan(plan, collection_plan=_allocation())
    assert _goal(plan, "goal-1").acceptance[-1] == clause


def test_组合在别处还活着_空心goal点名它不算() -> None:
    """「reddit·豆包」在 goal-1 还有卡：空心 goal-6 引它的产物是合法的上游引用。"""
    assembled = _assembled()
    clause = "竞争位置章引用 reddit·豆包 的上游结论"
    assembled["goals"][5]["acceptance"] = assembled["goals"][5]["acceptance"] + [clause]
    plan = _build(assembled)
    normalize_plan(plan, collection_plan=_allocation())
    assert _goal(plan, "goal-6").acceptance[-1] == clause


def test_幂等_再跑一遍不再摘() -> None:
    plan = _build()
    normalize_plan(plan, collection_plan=_allocation())
    snapshot = copy.deepcopy(_without_prompts(plan.to_dict()))
    assert [n for n in normalize_plan(plan, collection_plan=_allocation()) if n.startswith("[修正4]")] == []
    assert _without_prompts(plan.to_dict()) == snapshot
