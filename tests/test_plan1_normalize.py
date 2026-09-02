"""§PLAN-1 货 2：能机械修的规则不再叫模型返工，且每条修正都留痕。"""

from __future__ import annotations

from app.plan.lint import lint
from app.plan.model import Plan
from app.plan.normalize import normalize_plan
from tests.plan_factory import attach_rating_agents, make_agent, make_plan_dict
from tests.test_plan_generate import _agent, _generate, _goal, _valid_skeleton


def _collector(goal_id: str, agent_id: str, entity: str) -> dict:
    agent = make_agent(agent_id, goal_id)
    agent["display_name"] = f"小红书数据抓取·{entity}"
    agent["entity"] = entity
    agent["capability"] = {**agent["capability"], "profile": "web-collector", "sources": ["xhs"]}
    agent["output"] = {**agent["output"], "format": "json", "shape": "array", "path": f"goals/{goal_id}/{agent_id}.json"}
    agent["chapter"] = {
        **agent["chapter"], "chapter_type": "collection",
        "closing": {**agent["chapter"]["closing"], "output": {"path": agent["output"]["path"]}, "entities": [entity]},
    }
    return agent


def _writer(goal_id: str, agent_id: str, entities: list[str]) -> dict:
    agent = make_agent(agent_id, goal_id)
    agent["chapter"] = {**agent["chapter"], "chapter_type": "report", "closing": {**agent["chapter"]["closing"], "entities": entities}}
    return agent


def _plan(goal1_agents: list[dict], goal2_agents: list[dict]) -> Plan:
    raw = make_plan_dict()
    raw["goals"][0]["agents"] = goal1_agents
    raw["goals"][1]["agents"] = goal2_agents
    raw["goals"][2]["agents"] = [make_agent("agent-3", "goal-3")]
    attach_rating_agents(raw)
    return Plan.from_dict(raw)


def test_规则26_同goal与上游goal的采集章可达时补inputs_不删实体() -> None:
    plan = _plan(
        [_collector("goal-1", "data-collection", "小罐茶"), _writer("goal-1", "writer-1", ["小罐茶"])],
        [_writer("goal-2", "writer-2", ["小罐茶"])],
    )
    assert any(e.startswith("[规则26]") for e in lint(plan)["errors"])
    notes = normalize_plan(plan)
    assert notes == [
        "[修正26] goal-1/ch-1 (writer-1) 实体 小罐茶 不可达，补 inputs：goals/goal-1/data-collection.json",
        "[修正26] goal-2/ch-1 (writer-2) 实体 小罐茶 不可达，补 inputs：goals/goal-1/data-collection.json",
    ]
    assert not any(e.startswith("[规则26]") for e in lint(plan)["errors"])
    assert plan.goals[1].agents[0].chapter["closing"]["entities"] == ["小罐茶"]
    assert normalize_plan(plan) == []  # 幂等


def test_规则26_全计划无采集章的实体被删_删空不删留给lint() -> None:
    plan = _plan(
        [_collector("goal-1", "data-collection", "小罐茶"), _writer("goal-1", "writer-1", ["大益"])],
        [_writer("goal-2", "writer-2", ["小罐茶", "大益"])],
    )
    notes = normalize_plan(plan)
    assert notes == [
        "[修正26] goal-2/ch-1 (writer-2) 实体 小罐茶 不可达，补 inputs：goals/goal-1/data-collection.json",
        "[修正26] goal-2/ch-1 (writer-2) 实体 大益 全计划无可达采集章，已从 closing.entities 删除",
    ]
    assert plan.goals[1].agents[0].chapter["closing"]["entities"] == ["小罐茶"]
    # goal-1 的 writer 只剩「大益」一个实体：删空不删，规则 26 照报，交章级重试
    writer = next(a for a in plan.goals[0].agents if a.agent_id == "writer-1")
    assert writer.chapter["closing"]["entities"] == ["大益"]
    r26 = [e for e in lint(plan)["errors"] if e.startswith("[规则26]")]
    assert len(r26) == 1 and "goal-1/ch-1" in r26[0]


def test_规则26_不引非上游goal的采集章_只删() -> None:
    # goal-1 的 writer 引「大益」，采集章却在 goal-2（goal-1 不依赖它）：不敢补，删
    plan = _plan(
        [_writer("goal-1", "writer-1", ["小罐茶", "大益"]), _collector("goal-1", "data-collection", "小罐茶")],
        [_collector("goal-2", "data-collection-2", "大益")],
    )
    notes = normalize_plan(plan)
    assert [n for n in notes if "大益" in n] == [
        "[修正26] goal-1/ch-1 (writer-1) 实体 大益 全计划无可达采集章，已从 closing.entities 删除",
    ]


def test_规则17_末位agent与deliverable的shape对齐并发修正事件(tmp_path) -> None:
    skeleton = _valid_skeleton()
    # goal-1：deliverable array，采集 agent 却写 object → 以 deliverable 为准改 array
    skeleton["goals"][0]["agents"] = [_agent("HN 数据抓取·飞书", "采集", output={"shape": "object"})]
    # goal-3：deliverable 写成 array，报告撰写是节化章 → 两边都改 object
    skeleton["goals"][2]["deliverable"]["shape"] = "array"
    plan, store, _ = _generate(tmp_path, [skeleton])
    assert plan.goals[0].agents[0].output["shape"] == "array"
    assert plan.goals[2].deliverable["shape"] == "object"
    assert plan.goals[2].agents[-1].output["shape"] == "object"
    assert [e for e in store.events if e.outcome == "retrying"] == []
    repairs = [e.text for e in store.events if e.text.startswith("机械修正：")]
    assert repairs == [
        "机械修正：[修正17] goal-1/HN 数据抓取·飞书 output.shape object→array（归属 deliverable.shape=array）",
        "机械修正：[修正17] goal-3/报告撰写 output.shape object→object（归属 deliverable.shape=array→object）",
    ]
