"""§RATE-1 货 2：计划必排评级章（采集即评级，评级在写作之前）。

判据：每个采集章后面跟一个只连它的评级章；首个汇总章改等全部评级章；评级章走
`_output` 的评级五件验证器；lint 规则 30 在缺评级章时报红。
"""

from __future__ import annotations

from copy import deepcopy

from tests.test_plan_generate import NOW, RESEARCH_ID, _agent, _valid_skeleton


def _three_collector_plan():
    from app.plan.generate import _build_plan

    skeleton = _valid_skeleton()
    skeleton["goals"][0]["agents"] = [
        _agent("网页搜索数据抓取·豆包", "采集豆包口碑"),
        _agent("网页搜索数据抓取·讯飞", "采集讯飞口碑"),
        _agent("小红书数据抓取·通义", "采集通义口碑"),
        _agent("数据清洗", "汇总三份采集结果"),
    ]
    return _build_plan(
        skeleton, query="三家口碑", research_id=RESEARCH_ID, timestamp=NOW,
        scale="fast",
    )


def test_每个采集章各配一个评级章_汇总章等齐全部评级章() -> None:
    agents = _three_collector_plan().goals[0].agents
    collectors = [a for a in agents if a.capability["profile"] == "web-collector"]
    ratings = [a for a in agents if a.agent_id.startswith("reliability-audit")]

    assert len(collectors) == 3 and len(ratings) == 3
    # 评级章紧跟它评的那一章，且只依赖它一个——采集章一 done 就起跑，其余仍在跑。
    for index, collector in enumerate(collectors):
        assert agents[index * 2] is collector
        assert agents[index * 2 + 1] is ratings[index]
        assert ratings[index].depends_on == [collector.agent_id]
    assert agents[-1].agent_id == "data-cleaning"
    assert agents[-1].depends_on == [item.agent_id for item in ratings]


def test_评级章沿用评级五件验证器且不新增校验器名() -> None:
    ratings = [
        agent for agent in _three_collector_plan().goals[0].agents
        if agent.agent_id.startswith("reliability-audit")
    ]
    for agent in ratings:
        assert agent.output["format"] == "json"
        assert agent.output["shape"] == "array"
        assert agent.output["validators"] == [
            "file_exists",
            "no_item_missing_rating",
            "field_domain_whitelist:reliability_closed_set",
            "rating_notes_matches_regex",
            "rating_notes_scores_match_columns",
        ]
        assert agent.engine == "claude"
        assert "七个字段" in agent.prompt["body"]


def test_规则30_采集章没有评级章时报红_有则绿() -> None:
    from app.plan.lint import lint

    plan = _three_collector_plan().to_dict()
    assert [item for item in lint(plan)["errors"] if "[规则30]" in item] == []

    stripped = deepcopy(plan)
    stripped["goals"][0]["agents"] = [
        agent for agent in stripped["goals"][0]["agents"]
        if not agent["agent_id"].startswith("reliability-audit")
    ]
    stripped["goals"][0]["agents"][-1]["depends_on"] = [
        agent["agent_id"] for agent in stripped["goals"][0]["agents"][:-1]
    ]
    errors = [item for item in lint(stripped)["errors"] if "[规则30]" in item]
    assert len(errors) == 3
    assert all("没有对应的评级章" in item for item in errors)


def test_规则30_评级章多连一章也报红() -> None:
    from app.plan.lint import lint

    plan = _three_collector_plan().to_dict()
    rating = next(
        agent for agent in plan["goals"][0]["agents"]
        if agent["agent_id"].startswith("reliability-audit")
    )
    rating["depends_on"] = ["data-collection", "data-collection-2"]
    errors = [item for item in lint(plan)["errors"] if "[规则30]" in item]
    # 依赖两章后它不再被认成「那一章的评级章」，退化成「采集章没配评级章」。
    assert errors and any("data-collection" in item for item in errors)


def test_评级章的章规格确定性生成_不吃一次章扩写引擎调用(tmp_path) -> None:
    from tests.test_plan_generate import _generate

    skeleton = _valid_skeleton()
    skeleton["goals"][0]["agents"] = [
        _agent("HN 数据抓取·飞书", "通过 API 抓取 Hacker News 证据"),
        _agent("网页搜索数据抓取·飞书", "补齐网页搜索证据"),
    ]
    plan, _, engine = _generate(tmp_path, [skeleton])

    agents = [agent for goal in plan.goals for agent in goal.agents]
    ratings = [
        agent for agent in agents
        if (agent.chapter or {}).get("closing", {}).get("notes", {}).get("rates_chapter")
    ]
    assert [agent.agent_id for agent in ratings] == [
        "reliability-audit", "reliability-audit-2",
    ]
    # 章扩写只对模型写的章发起；评级章的章规格由系统写死，不多花一次调用。
    assert len(engine.chapter_tasks) == len(agents) - len(ratings)

    collector = plan.goals[0].agents[0]
    chapter = ratings[0].chapter
    assert chapter["chapter_type"] == "audit"
    # §RATE-2 货 1 起，评级章的输入改指**物化行文件**（采集产物只有模型顺手写下的
    # 一小撮，库里那一章的行才是全量）；这里跟着改口径，别把绿的验成红的。
    assert chapter["opening"]["inputs"] == [
        {"path": "goals/goal-1/data-collection.rows.json"}
    ]
    assert chapter["closing"]["notes"]["rates_output"] == collector.output["path"]
    assert chapter["closing"]["output"] == {"path": ratings[0].output["path"]}
    assert chapter["closing"]["notes"]["rates_chapter"] == collector.agent_id
    # §RATE-2：章规格与 **task 文案**必须一起改口——货 1 只改了章规格、
    # 文案仍写着「逐条评级 <采集产物>」，模型照文案走就还是只读那 10 条。
    assert "data-collection.rows.json" in ratings[0].task
    assert collector.output["path"] not in ratings[0].task
