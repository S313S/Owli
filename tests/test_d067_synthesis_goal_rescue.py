"""§D-067：删卡闸把综合 goal 整个掏空——综合章不跟着表外卡连删，改接上游 goal 产物。

病根（WX-1 standard 复验小跑 `r-wx1-0914-1239`）：骨架 goal-4「综合分析国内对豆包看法与竞品比较」
depends_on goal-1/2/3，引擎却在它的草稿里自带两张分配表外的「微博·通义千问 / 文心一言」卡，
审计 / 交叉 / 一致性 / 撰写四章的依赖链只通到那两张卡。删卡闸删卡 → `_drop_orphans` 判四章孤儿连删
→ D-065 把 0 章的 goal-4 移出：最终计划 3 goal / 38 章，没有跨 goal 综合层。上一轮综合 goal
没自带卡就留住了——结果取决于引擎当次怎么起草。

夹具是真的：`tests/fixtures/d067/wx1-run-0914-1239` 是那一轮 plan-segments 的 skeleton / allocation /
entity-1..5 / assembled 原样拷贝（源目录只读）；`alloc3-run-b` 是 ALLOC-3 第二次小跑的同形拷贝。
`normalize_golden_2282058.json` 是**改前代码**（main 2282058）在三格上的修正说明 + 去 prompt 计划 sha：
- `wx1-run-0914-1239/standard`：红的现场记录（goal-4 被移出）；
- `wx1-run-0914-1239-no-upstream/standard`：同一份草稿把 goal-4.depends_on 置空的构造——无上游 goal
  时必须与改前逐字相同（仍删、仍移出）；
- `alloc3-run-b/standard`：不退化。fast 四份真骨架 + d065 的逐字锁在 test_alloc3_duplicate_owner_gate。
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from app.plan.generate import _build_plan
from app.plan.lint import lint
from app.plan.model import Plan
from app.plan.normalize import empty_goal_removal, normalize_plan

FIXTURES = Path(__file__).parent / "fixtures" / "d067"
WX1 = "wx1-run-0914-1239"
GOLDEN = json.loads((FIXTURES / "normalize_golden_2282058.json").read_text(encoding="utf-8"))
RESCUED = ["reliability-audit-28", "cross-validation-4", "consistency-check-3", "report-writing-4"]


def _assembled(name: str = WX1) -> dict:
    return json.loads((FIXTURES / name / "assembled.json").read_text(encoding="utf-8"))


def _allocation(name: str = WX1) -> dict:
    return json.loads((FIXTURES / name / "allocation.json").read_text(encoding="utf-8"))


def _build(name: str = WX1, assembled: dict | None = None) -> Plan:
    folder = FIXTURES / name
    skeleton = json.loads((folder / "skeleton.json").read_text(encoding="utf-8"))
    cards = []
    for path in sorted(folder.glob("entity-*.json"), key=lambda p: int(p.stem.split("-")[1])):
        card = json.loads(path.read_text(encoding="utf-8"))
        card.setdefault("id", card["canonical"])  # 生产里 resolve_entities 填的就是 canonical
        cards.append(card)
    return _build_plan(
        assembled or _assembled(name),
        query="国内大家对豆包的看法",
        research_id=f"r-{name}",
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


def _outputs(plan: Plan) -> set[str]:
    paths = {str(goal.deliverable.get("path", "")) for goal in plan.goals}
    paths |= {str(agent.output.get("path", "")) for goal in plan.goals for agent in goal.agents}
    return paths - {""}


def test_造红_改前代码_goal4被移出_计划只剩3goal38章() -> None:
    red = GOLDEN[f"{WX1}/standard"]
    assert red["goals"] == ["goal-1", "goal-2", "goal-3"] and red["chapters"] == 38
    removed = [empty_goal_removal(note) for note in red["notes"]]
    assert {"goal_id": "goal-4", "title": "综合分析国内对豆包看法与竞品比较"} in removed
    for agent_id in RESCUED:
        assert any(f"goal-4/{agent_id} 的上游采集卡已删" in note for note in red["notes"])
    # 前提：goal-4 在骨架里挂着全部三个上游 goal，改接有依据
    assert _assembled()["goals"][3]["depends_on"] == ["goal-1", "goal-2", "goal-3"]


def test_绿_goal4保留_综合章改接上游goal产物_表外微博卡照删() -> None:
    plan = _build()
    notes = normalize_plan(plan, collection_plan=_allocation())
    assert [goal.goal_id for goal in plan.goals] == ["goal-1", "goal-2", "goal-3", "goal-4"]
    assert not [note for note in notes if empty_goal_removal(note)]
    goal_4 = _goal(plan, "goal-4")
    assert [agent.agent_id for agent in goal_4.agents] == RESCUED
    # 表外卡与只评它们的评级章照删
    for card in ("weibo·通义千问", "weibo·文心一言"):
        assert any(f"goal-4/" in n and f"采集卡「{card}」不在分配表里，已删除" in n for n in notes), card
    for agent_id in ("data-collection-27", "data-collection-28", "reliability-audit-29", "reliability-audit-30"):
        assert any(f"goal-4/{agent_id} " in n for n in notes)
    rewire = [n for n in notes if n.startswith("[修正31] goal-4「") and "改接上游 goal 产物" in n]
    assert len(rewire) == 1, notes

    outputs = _outputs(plan)
    ancestors = {"goal-1", "goal-2", "goal-3"}
    deliverables = [
        {"from_goal": g, "artifact": _goal(plan, g).deliverable["path"]} for g in ("goal-1", "goal-2", "goal-3")
    ]
    head = goal_4.agents[0]
    assert head.depends_on == []
    assert head.inputs[:3] == deliverables
    # 上游采集产物只剩删卡后还在的表内卡：goal-1 5 + goal-2 4 + goal-3 4
    assert len(head.inputs) == 3 + 13
    writer = goal_4.agents[-1]
    assert writer.output["path"] == goal_4.deliverable["path"]
    assert writer.depends_on == ["consistency-check-3"]
    assert writer.inputs == deliverables
    for agent in goal_4.agents:
        for item in agent.inputs:
            assert item["from_goal"] in ancestors and item["artifact"] in outputs, item
    ids = {agent.agent_id for goal in plan.goals for agent in goal.agents}
    assert all(dep in ids for goal in plan.goals for agent in goal.agents for dep in agent.depends_on)
    assert lint(plan, collection_plan=_allocation())["errors"] == []


def test_D062验收条闸_别的goal摘条逐字不变_goal4只多摘两条过时条() -> None:
    """货 1 时这条锁「摘条数不增」；货 2（用户拍甲）起 goal-4 点名已删补采卡的两条要摘，其余逐字不变。"""
    plan = _build()
    notes = normalize_plan(plan, collection_plan=_allocation())
    before = [n for n in GOLDEN[f"{WX1}/standard"]["notes"] if n.startswith("[修正4]")]
    assert [n for n in notes if n.startswith("[修正4]") and not n.startswith("[修正4] goal-4")] == before
    assert [n.split(" 已摘")[0] for n in notes if n.startswith("[修正4] goal-4")] == [
        "[修正4] goal-4.acceptance[1]", "[修正4] goal-4.acceptance[2]",
    ]


def test_无上游goal的构造_与改前代码逐字相同_仍删仍移出() -> None:
    assembled = _assembled()
    assembled["goals"][3]["depends_on"] = []
    plan = _build(assembled=assembled)
    notes = normalize_plan(plan, collection_plan=_allocation())
    golden = GOLDEN[f"{WX1}-no-upstream/standard"]
    assert notes == golden["notes"]
    assert _sha(plan) == golden["plan_sha256"]
    assert "goal-4" not in {goal.goal_id for goal in plan.goals}


def test_不退化_alloc3小跑b计划与改前代码逐字相同() -> None:
    plan = _build("alloc3-run-b")
    notes = normalize_plan(plan, collection_plan=_allocation("alloc3-run-b"))
    golden = GOLDEN["alloc3-run-b/standard"]
    assert notes == golden["notes"]
    assert _sha(plan) == golden["plan_sha256"]


def test_综合链没断光_还有表内卡撑着_不救也不移出() -> None:
    """goal-4 首章换成一张表里归上游 goal-2 的卡：闸不删它（交规则 21），评级链还通，与旧行为同。"""
    assembled = _assembled()
    goal_2_card = copy.deepcopy(next(
        a for a in assembled["goals"][1]["agents"] if a["name"].startswith("小红书数据抓取")
    ))
    assembled["goals"][3]["agents"].insert(0, goal_2_card)
    plan = _build(assembled=assembled)
    notes = normalize_plan(plan, collection_plan=_allocation())
    assert not [n for n in notes if "改接上游 goal 产物" in n]
    goal_4 = _goal(plan, "goal-4")
    assert "report-writing-4" in {agent.agent_id for agent in goal_4.agents}


def test_章规格已生成时_opening_inputs同步补路径() -> None:
    plan = _build()
    writer = next(a for a in _goal(plan, "goal-4").agents if a.agent_id == "report-writing-4")
    writer.chapter = {"opening": {"inputs": [{"path": "goals/goal-4/consistency-check-3.json"}]}}
    normalize_plan(plan, collection_plan=_allocation())
    assert writer in _goal(plan, "goal-4").agents
    paths = [item["path"] for item in writer.chapter["opening"]["inputs"]]
    assert paths[0] == "goals/goal-4/consistency-check-3.json"
    assert len(paths) == 4 and paths[1:] == [item["artifact"] for item in writer.inputs]


def test_幂等_再跑一遍不再删也不再改接() -> None:
    plan = _build()
    normalize_plan(plan, collection_plan=_allocation())
    snapshot = _sha(plan)
    assert [n for n in normalize_plan(plan, collection_plan=_allocation()) if n.startswith("[修正31]")] == []
    assert _sha(plan) == snapshot



# ---- 货 2：综合 goal 验收条点名已删卡的产物 ----

STALE = [1, 2]  # 「补采的「微博数据抓取·通义千问」「微博数据抓取·文心一言」两章 JSON…」「…加本 goal 2 项补采共 28 项」


def test_货2造红_草稿里goal4有两条要已删补采卡产物的验收条() -> None:
    acceptance = _assembled()["goals"][3]["acceptance"]
    assert "「微博数据抓取·通义千问」「微博数据抓取·文心一言」两章 JSON" in acceptance[1]
    assert "本 goal 2 项补采共 28 项" in acceptance[2]


def test_货2绿_两条过时验收条被摘并留痕_其余验收条一字不动() -> None:
    original = _assembled()["goals"][3]["acceptance"]
    plan = _build()
    notes = normalize_plan(plan, collection_plan=_allocation())
    goal_4 = _goal(plan, "goal-4")
    assert goal_4.acceptance == [text for index, text in enumerate(original) if index not in STALE]
    named = next(n for n in notes if n.startswith("[修正4] goal-4.acceptance[1] 已摘："))
    assert "「微博数据抓取·通义千问」「微博数据抓取·文心一言」已被删卡闸删除" in named
    assert named.endswith(f"——原文「{original[1]}」")
    backfill = next(n for n in notes if n.startswith("[修正4] goal-4.acceptance[2] 已摘："))
    assert "补采产物" in backfill and backfill.endswith(f"——原文「{original[2]}」")
    assert lint(plan, collection_plan=_allocation())["errors"] == []


def _with_goal_4_acceptance(extra: list[str]) -> Plan:
    assembled = _assembled()
    assembled["goals"][3]["acceptance"] = assembled["goals"][3]["acceptance"] + extra
    return _build(assembled=assembled)


def test_货2豁免_缺口语境与否定语境的补采_活着的卡名_不摘() -> None:
    extra = [
        "对单源结论在缺口说明节注明所需补采的来源与实体",
        "证据台账不引用任何补采产物，只引用上游 goal 产物",
        "交叉验证章逐条引用「小红书数据抓取·DeepSeek」的采集结果",
    ]
    plan = _with_goal_4_acceptance(extra)
    normalize_plan(plan, collection_plan=_allocation())
    assert _goal(plan, "goal-4").acceptance[-3:] == extra


def test_货2不看还有卡的goal_点名已删卡照旧不动() -> None:
    """goal-1 删了三张卡但还剩表内卡：点名已删卡「网页搜索数据抓取·豆包」的验收条不归第四类管。"""
    assembled = _assembled()
    clause = "「网页搜索数据抓取·豆包」产物顶层为数组"
    assembled["goals"][0]["acceptance"] = assembled["goals"][0]["acceptance"] + [clause]
    plan = _build(assembled=assembled)
    normalize_plan(plan, collection_plan=_allocation())
    assert _goal(plan, "goal-1").acceptance[-1] == clause
