"""§D-071：删卡连删把「还留着卡的 goal」的清洗 / 交叉 / 撰写链一并删掉，交付物无章产出且 lint 放行。

取证 WX-1 重建研究 r-d9c69fb6132a：goal-3（Kimi）规划段由「规划」章打头，`generate._build_plan`
「首个汇总章等齐全部采集章」要求此前全是采集章，条件不成立 → 清洗章只依赖最后一张 hn·Kimi →
评级章改接也不触发；hn·Kimi 是分配表外卡，`normalize._drop_orphans` 连删清洗 / 交叉 / 撰写，
goal-3 只剩 规划 / 小红书·Kimi / 其评级章，交付物 kimi-profile.json 无章产出，lint 0 条。
09-13 小跑 r-wx1-0913-1355 的 goal-2 是同一个病（d065 夹具的存盘计划里就少了那条链）。

三层各锁一道：
- 源头：打头的非采集章不破坏「等齐采集章」（`_leading_collector_block`）；
- 连删：goal 还有存活卡时综合链不删、链头改接存活卡的评级章（`_surviving_card_rescue`）；
- 兜底闸：规则 34「交付物必须恰有一章产出」，**只在生成期开**（调度 09-15 批复收窄），
  编辑 / 批准 / 已存旧计划不拦。

夹具 `tests/fixtures/d071/r-d9c69fb6132a` 是 `../Owli-wx1/var/runs/r-d9c69fb6132a/plan-segments/`
与 `var/wx1-form/r-d9c69fb6132a.plan.json`（→ plan.json）的只读拷贝。期望值读原文写死。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app.plan.generate import _build_plan, _leading_collector_block
from app.plan.lint import lint
from app.plan.model import Plan
from app.plan.normalize import empty_goal_removal, normalize_plan
from tests.plan_factory import attach_rating_agents, make_agent, make_goal, make_plan_dict

FIXTURE = Path(__file__).parent / "fixtures" / "d071" / "r-d9c69fb6132a"
DELIVERABLE = "goals/goal-3/kimi-profile.json"


def _read(name: str):
    return json.loads((FIXTURE / name).read_text(encoding="utf-8"))


def _build_wx1() -> tuple[Plan, dict]:
    meta, skeleton = _read("meta.json"), _read("skeleton.json")
    cards = []
    for path in sorted(FIXTURE.glob("entity-*.json"), key=lambda p: int(p.stem.split("-")[1])):
        card = json.loads(path.read_text(encoding="utf-8"))
        card.setdefault("id", card["canonical"])
        cards.append(card)
    plan = _build_plan(
        _read("assembled.json"),
        query=meta["query"], research_id=meta["research_id"], timestamp=meta["timestamp"],
        scale=meta["scale"],
        market_profile=skeleton["market_profile"],
        market_profile_justification=skeleton["market_profile_justification"],
        subjects=skeleton["subjects"], subjects_justification=skeleton["subjects_justification"],
        entities=cards, repairs=[],
    )
    return plan, _read("allocation.json")


def _goal(plan: Plan, goal_id: str):
    return next(goal for goal in plan.goals if goal.goal_id == goal_id)


# ---------------------------------------------------------------- 真夹具


def test_造红前提_存盘计划goal3交付物无章产出_旧口径lint放行_规则34报一条() -> None:
    saved = Plan.from_dict(_read("plan.json"))
    goal_3 = _goal(saved, "goal-3")
    assert [agent.agent_id for agent in goal_3.agents] == [
        "goal-planning", "data-collection-16", "reliability-audit-16",
    ]
    assert goal_3.deliverable["path"] == DELIVERABLE
    readers = [
        agent.agent_id for agent in _goal(saved, "goal-6").agents
        if any(item["artifact"] == DELIVERABLE for item in agent.inputs)
    ]
    assert readers == ["data-cleaning-6", "report-writing-6"]

    allocation = _read("allocation.json")
    assert lint(saved, collection_plan=allocation)["errors"] == []
    errors = lint(saved, collection_plan=allocation, require_deliverable_producer=True)["errors"]
    assert len(errors) == 1 and errors[0].startswith(f"[规则34] goal-3 交付物 {DELIVERABLE} 没有任何章产出")
    assert "请在本 goal 末尾补一章报告撰写" in errors[0]


def test_绿_goal3保住清洗交叉撰写链_清洗章接存活卡评级章_生成期lint全过() -> None:
    plan, allocation = _build_wx1()
    raw_goal_3 = _goal(plan, "goal-3")
    cleaner = next(agent for agent in raw_goal_3.agents if agent.agent_id == "data-cleaning-3")
    # 源头：清洗章等齐全部评级章，不再只挂最后一张 hn·Kimi
    assert cleaner.depends_on == [f"reliability-audit-{n}" for n in range(16, 22)]

    normalize_plan(plan, collection_plan=allocation, per_goal_capacity=None)
    goal_3 = _goal(plan, "goal-3")
    assert [(agent.agent_id, agent.depends_on) for agent in goal_3.agents] == [
        ("goal-planning", []),
        ("data-collection-16", []),
        ("reliability-audit-16", ["data-collection-16"]),
        ("data-cleaning-3", ["reliability-audit-16"]),
        ("cross-validation-2", ["data-cleaning-3"]),
        ("report-writing-3", ["cross-validation-2"]),
    ]
    assert [a.agent_id for a in goal_3.agents if a.output["path"] == DELIVERABLE] == ["report-writing-3"]
    outputs = {agent.output["path"] for goal in plan.goals for agent in goal.agents}
    for agent in _goal(plan, "goal-6").agents:
        for item in agent.inputs:
            if item["from_goal"] == "goal-3":
                assert item["artifact"] in outputs, (agent.agent_id, item)
    assert lint(plan, collection_plan=allocation, require_deliverable_producer=True)["errors"] == []


def test_源头判定_打头非采集章被跳过_采集章之后夹了非采集章就不算() -> None:
    def agent(agent_id: str, profile: str) -> dict:
        return {"agent_id": agent_id, "capability": {"profile": profile}}

    collector, analyst = "web-collector", "readonly-analyst"
    assert _leading_collector_block([]) == []
    assert _leading_collector_block([agent("p", analyst)]) == []
    assert _leading_collector_block([agent("a", collector), agent("b", collector)]) == ["a", "b"]
    assert _leading_collector_block(
        [agent("p", analyst), agent("a", collector), agent("b", collector)]
    ) == ["a", "b"]
    # 首个汇总章已经排过（采集章后面跟过非采集章）：后面的章照旧只依赖上一章
    assert _leading_collector_block(
        [agent("a", collector), agent("c", analyst), agent("b", collector)]
    ) == []


# ---------------------------------------------------------------- 构造夹具


def _collector(agent_id: str, goal_id: str, source: str, entity: str) -> dict:
    data = make_agent(agent_id, goal_id)
    data["entity"] = entity
    data["capability"].update({"profile": "web-collector", "sources": [source]})
    data["output"] = {
        "format": "json", "shape": "array",
        "path": f"goals/{goal_id}/{agent_id}.json", "validators": ["file_exists"],
    }
    data["chapter"]["closing"]["output"]["path"] = data["output"]["path"]
    return data


def _chain(goal: dict, head_depends_on: list[str]) -> None:
    """清洗 → 撰写两章；撰写章产出交付物。"""
    goal_id = goal["goal_id"]
    cleaner = make_agent(f"data-cleaning-{goal_id[-1]}", goal_id)
    cleaner["depends_on"] = head_depends_on
    writer = make_agent(f"report-writing-{goal_id[-1]}", goal_id)
    writer["depends_on"] = [cleaner["agent_id"]]
    writer["output"]["path"] = goal["deliverable"]["path"]
    writer["chapter"]["closing"]["output"]["path"] = goal["deliverable"]["path"]
    goal["agents"].extend([cleaner, writer])


def _plan(goals: list[dict]) -> Plan:
    raw = make_plan_dict()
    raw["goals"] = goals
    raw["baseline"] = None
    return Plan.from_dict(raw)


def _rating_of(goal: dict, collector_id: str) -> str:
    return next(a["agent_id"] for a in goal["agents"] if a["depends_on"] == [collector_id])


def test_构造_仍有卡的goal_清洗链挂错在表外卡上_改接存活卡不删() -> None:
    goal = make_goal(1)
    goal["agents"] = [
        make_agent("goal-planning", "goal-1"),
        _collector("data-collection", "goal-1", "xhs", "Kimi"),
        _collector("data-collection-2", "goal-1", "weibo", "Kimi"),
    ]
    attach_rating_agents({"goals": [goal]})
    kept_rating = _rating_of(goal, "data-collection")
    stray_rating = _rating_of(goal, "data-collection-2")
    _chain(goal, [stray_rating])  # 挂错：只挂在表外卡的评级章上
    plan = _plan([goal])

    notes = normalize_plan(plan, collection_plan={"goal-1": [{"entity": "Kimi", "source_id": "xhs"}]})

    ids = [agent.agent_id for agent in plan.goals[0].agents]
    assert ids == ["goal-planning", "data-collection", kept_rating, "data-cleaning-1", "report-writing-1"]
    by_id = {agent.agent_id: agent for agent in plan.goals[0].agents}
    assert by_id["data-cleaning-1"].depends_on == [kept_rating]
    assert by_id["data-cleaning-1"].chapter["opening"]["inputs"] == [
        {"path": f"goals/goal-1/{kept_rating}.json"},
    ]
    assert by_id["report-writing-1"].depends_on == ["data-cleaning-1"]
    rescue = [note for note in notes if "还有存活的采集卡" in note]
    assert rescue == [
        "[修正31] goal-1「阶段 1 证据产物」还有存活的采集卡，综合章 data-cleaning-1、report-writing-1 "
        f"的依赖只通到已删卡：不删，链头 data-cleaning-1 改接本 goal 存活卡 {kept_rating}"
    ]
    assert any(f"goal-1/{stray_rating} 的上游采集卡已删" in note for note in notes)
    # 手写章规格不全（规则 22 与本包无关），只看悬空依赖与交付物产出
    errors = lint(plan, require_deliverable_producer=True)["errors"]
    assert not [e for e in errors if e.startswith(("[规则2]", "[规则34]"))], errors


def test_构造_goal卡全删且无上游_维持D065移出() -> None:
    goal = make_goal(1)
    goal["agents"] = [_collector("data-collection", "goal-1", "weibo", "Kimi")]
    attach_rating_agents({"goals": [goal]})
    _chain(goal, [_rating_of(goal, "data-collection")])
    keep = make_goal(2)
    keep["depends_on"] = []
    keep["agents"] = [_collector("data-collection-9", "goal-2", "xhs", "Kimi")]
    plan = _plan([goal, keep])

    notes = normalize_plan(plan, collection_plan={"goal-2": [{"entity": "Kimi", "source_id": "xhs"}]})

    assert [goal.goal_id for goal in plan.goals] == ["goal-2"]
    assert {"goal_id": "goal-1", "title": "阶段 1 证据产物"} in [empty_goal_removal(n) for n in notes]
    assert not [note for note in notes if "还有存活的采集卡" in note]


def test_构造_综合goal卡全删但有上游_维持D067改接上游goal() -> None:
    upstream = make_goal(1)
    upstream["agents"] = [_collector("data-collection", "goal-1", "xhs", "Kimi")]
    attach_rating_agents({"goals": [upstream]})
    _chain(upstream, [_rating_of(upstream, "data-collection")])
    synthesis = make_goal(2)
    synthesis["depends_on"] = ["goal-1"]
    synthesis["agents"] = [_collector("data-collection-2", "goal-2", "weibo", "Kimi")]
    attach_rating_agents({"goals": [synthesis]})
    synthesis["agents"][1]["agent_id"] = "reliability-audit-2"
    _chain(synthesis, ["reliability-audit-2"])
    plan = _plan([upstream, synthesis])

    notes = normalize_plan(plan, collection_plan={"goal-1": [{"entity": "Kimi", "source_id": "xhs"}]})

    goal_2 = _goal(plan, "goal-2")
    assert [agent.agent_id for agent in goal_2.agents] == ["data-cleaning-2", "report-writing-2"]
    assert goal_2.agents[0].depends_on == []
    assert {"from_goal": "goal-1", "artifact": "goals/goal-1/result.md"} in goal_2.agents[0].inputs
    assert any("改接上游 goal 产物" in note for note in notes)
    assert not [note for note in notes if "还有存活的采集卡" in note]


# ---------------------------------------------------------------- 规则 34 的开关边界


def test_规则34只在生成期开_编辑与批准路径不触发(monkeypatch: pytest.MonkeyPatch) -> None:
    """调度 09-15 批复：编辑 / 批准 / 已存旧计划不拦。日后若有人全局打开，这条先红。"""
    from app.plan import editing

    saved = Plan.from_dict(_read("plan.json"))  # goal-3 交付物无章产出的真存盘计划
    for decision in saved.decision_balance:
        decision["answer"] = decision.get("answer") or "默认"
    assert lint(saved)["errors"] == []
    assert not [e for e in lint(saved, for_approval=True)["errors"] if e.startswith("[规则34]")]

    reached: list[str] = []
    monkeypatch.setattr(editing, "save_plan", lambda store, plan, **_: reached.append("approve") or plan)
    monkeypatch.setattr(
        editing, "commit_changes", lambda store, plan, changes, **_: reached.append("edit") or plan,
    )
    approved = editing.approve(object(), copy.deepcopy(saved), at="2026-09-15T00:00:00Z")
    assert approved.status == "approved"
    submitted = saved.to_dict()
    submitted["title"] = f"{submitted['title']}（改）"
    editing.apply_edit(object(), saved, submitted, at="2026-09-15T00:00:00Z")
    assert reached == ["approve", "edit"]
