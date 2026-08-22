from __future__ import annotations

import asyncio
from collections import Counter
from copy import deepcopy

import pytest

from tests.plan_factory import make_agent, make_plan_dict
from tests.test_plan_generate import (
    FakeEngine,
    FakeStore,
    ForbiddenEngine,
    _agent,
    _goal,
    _valid_skeleton,
)


def _collector(agent_id: str, goal_id: str, source_id: str) -> dict:
    agent = make_agent(agent_id, goal_id)
    agent["capability"].update({
        "profile": "web-collector",
        "tools": [f"source.{source_id}", "fs.write", "db.write"],
        "sources": [source_id],
        "network": "sources_only",
    })
    agent["output"] = {
        "format": "json",
        "shape": "array",
        "path": f"goals/{goal_id}/{agent_id}.json",
        "validators": [
            "file_exists",
            "json_array_min_items:1",
            "each_item_has:permalink,fetched_at",
        ],
    }
    return agent


def test_lint_拦截跨_goal_重复完整采集并自带复用解法() -> None:
    from app.plan.lint import duplicate_collection_goal_ids, lint

    plan = make_plan_dict()
    first = _collector("data-collection", "goal-1", "hacker_news")
    repeated = _collector("data-collection-2", "goal-3", "hacker_news")
    plan["goals"][0]["agents"] = [first]
    plan["goals"][2]["agents"] = [repeated]

    result = lint(plan)
    rule_errors = [item for item in result["errors"] if item.startswith("[规则21]")]

    assert duplicate_collection_goal_ids(plan) == {"goal-3"}
    assert len(rule_errors) == 1
    assert "source_id=hacker_news" in rule_errors[0]
    assert "goal-1/data-collection" in rule_errors[0]
    assert "goal-3/data-collection-2" in rule_errors[0]
    assert "改为 inputs 引用上游产物" in rule_errors[0]
    assert first["output"]["path"] in rule_errors[0]


def test_lint_不误伤不同源与合法_inputs_复用() -> None:
    from app.plan.lint import lint

    different = make_plan_dict()
    different["goals"][0]["agents"] = [
        _collector("data-collection", "goal-1", "hacker_news")
    ]
    different["goals"][1]["agents"] = [
        _collector("data-collection-2", "goal-2", "web_search")
    ]
    assert not [item for item in lint(different)["errors"] if item.startswith("[规则21]")]

    reused = make_plan_dict()
    upstream = _collector("data-collection", "goal-1", "hacker_news")
    reused["goals"][0]["agents"] = [upstream]
    downstream = reused["goals"][1]["agents"][0]
    downstream["inputs"] = [{
        "from_goal": "goal-1",
        "artifact": upstream["output"]["path"],
    }]
    assert not [item for item in lint(reused)["errors"] if item.startswith("[规则21]")]


def test_goal段提示词注入上游源与产物路径且明确_inputs_复用() -> None:
    from app.config import load_research_scale_config
    from app.plan.generate import _goal_prompt

    prompt = _goal_prompt(
        "飞书竞品优缺点",
        "goal-2",
        {"title": "审计", "objective": "审计", "depends_on": ["goal-1"]},
        [],
        upstream_collections=[{
            "goal_id": "goal-1",
            "agent_id": "data-collection",
            "source_id": "hacker_news",
            "output_path": "goals/goal-1/data-collection.json",
        }],
        scale="standard",
        scale_config=load_research_scale_config(),
    )

    assert "source_id=hacker_news" in prompt
    assert "goals/goal-1/data-collection.json" in prompt
    assert "通过 inputs 消费" in prompt
    assert "禁止重采同源" in prompt


def test_中文市场选源覆盖表进入规划提示且_lint_拦_HN_PH() -> None:
    from app.config import load_research_scale_config
    from app.plan.generate import _goal_prompt
    from app.plan.lint import lint

    prompt = _goal_prompt(
        "豆包语音输入法的竞品分析",
        "goal-1",
        {"title": "采集", "objective": "采集", "depends_on": []},
        [],
        market_profile="cn_product",
        scale="fast",
        scale_config=load_research_scale_config(),
    )

    assert '"market_profile":"cn_product"' in prompt
    assert '"applicable_sources":["web_search","x"]' in prompt

    plan = make_plan_dict()
    plan["market_profile"] = "cn_product"
    plan["market_profile_justification"] = "产品主要面向中国大陆用户。"
    bad = _collector("hn-collector", "goal-1", "hacker_news")
    bad["chapter"] = {
        "chapter_id": "ch-1",
        "chapter_type": "collection",
        "plan_path": "goals/goal-1/ch-1.md",
        "opening": {"inputs": [], "task": bad["task"], "acceptance": ["完成"]},
        "closing": {
            "output": {"path": bad["output"]["path"]},
            "entities": ["豆包"],
            "expected_count": 1,
            "notes": {},
        },
    }
    plan["goals"][0]["agents"] = [bad]

    errors = lint(plan)["errors"]
    assert any("规则23" in item and "hacker_news" in item and "web_search,x" in item
               for item in errors)

    ok = _collector("web-collector", "goal-1", "web_search")
    ok["chapter"] = {**bad["chapter"], "closing": {**bad["chapter"]["closing"],
                     "output": {"path": ok["output"]["path"]}}}
    plan["goals"][0]["agents"] = [ok]
    assert not any("规则23" in item for item in lint(plan)["errors"])


def test_规则23_只读骨架_market_profile_不从题目措辞猜测() -> None:
    from app.plan.lint import lint

    plan = make_plan_dict()
    plan["research_question"] = "Notion competitor landscape"
    plan["market_profile"] = "cn_product"
    plan["market_profile_justification"] = "主要分发和用户社区在中国大陆。"
    bad = _collector("hn-collector", "goal-1", "hacker_news")
    bad["chapter"] = {
        "chapter_id": "ch-1", "chapter_type": "collection",
        "plan_path": "goals/goal-1/ch-1.md",
        "opening": {"inputs": [], "task": bad["task"], "acceptance": ["完成"]},
        "closing": {
            "output": {"path": bad["output"]["path"]}, "entities": ["Notion"],
            "expected_count": 1, "notes": {},
        },
    }
    plan["goals"][0]["agents"] = [bad]

    assert any("规则23" in item and "hacker_news" in item for item in lint(plan)["errors"])

    plan["market_profile"] = "global_product"
    assert not any("规则23" in item for item in lint(plan)["errors"])


def test_规则21重试只回灌后出现的违规_goal段(tmp_path) -> None:
    from app.adapters.routing import RoutedAdapter
    from app.plan.generate import generate_plan

    invalid = _valid_skeleton()
    invalid["goals"][2] = _goal(
        3,
        [_agent("HN 数据抓取", "重复采集 Hacker News")],
    )
    corrected = deepcopy(invalid)
    corrected["goals"][2] = _goal(
        3,
        [_agent("报告撰写", "引用上游产物撰写报告")],
    )
    engine = FakeEngine([invalid, corrected])
    store = FakeStore(tmp_path)
    adapter = RoutedAdapter(
        adapters={"claude": engine, "codex": ForbiddenEngine()},
    )

    plan = asyncio.run(generate_plan("飞书竞品优缺点", store, adapter))

    assert plan.status == "awaiting_review"
    assert [task.output_path.stem for task in engine.tasks] == [
        "skeleton", "goal-1", "goal-2", "goal-3", "goal-3",
    ]
    assert "[规则21]" in engine.tasks[-1].body
    assert "改为 inputs 引用上游产物" in engine.tasks[-1].body


def test_fast_骨架_goal与单_goal采集源上限均为配置硬约束() -> None:
    from app.config import load_research_scale_config
    from app.plan.generate import _build_plan, _skeleton_scaffolds

    config = load_research_scale_config()
    assert config.fast.max_chapters_per_goal == 4
    four_goals = {"goals": [
        {"title": str(index), "objective": "产出", "depends_on": []}
        for index in range(4)
    ], "market_profile": "global_product",
        "market_profile_justification": "面向全球市场。"}
    with pytest.raises(ValueError, match="goal 数必须在 3–3"):
        _skeleton_scaffolds(four_goals, scale="fast", scale_config=config)

    plan = _valid_skeleton()
    plan["goals"][0]["agents"] = [
        _agent("HN 数据抓取", "采集 HN"),
        _agent("网页搜索数据抓取", "采集网页"),
        _agent("Product Hunt 数据抓取", "采集 Product Hunt"),
    ]
    with pytest.raises(ValueError, match="采集源最多 2 个"):
        _build_plan(
            plan,
            query="查询",
            research_id="r-fast",
            timestamp="2026-08-21T00:00:00Z",
            scale="fast",
            scale_config=config,
        )


def test_fast_单_goal_超过四章在段级_lint_拦截() -> None:
    from app.config import load_research_scale_config
    from app.plan.lint import lint

    plan = make_plan_dict()
    goal = plan["goals"][0]
    goal["agents"] = [
        {**make_agent(f"agent-fast-{index}", "goal-1"), "chapter": None}
        for index in range(5)
    ]
    profile = load_research_scale_config().fast

    errors = lint(
        plan, max_chapters_per_goal=profile.max_chapters_per_goal,
    )["errors"]

    assert any(
        item.startswith("[规则24] goal-1") and "5" in item and "4" in item
        for item in errors
    )


def test_fast_prompt使用配置条数且默认与显式standard逐字一致() -> None:
    from app.config import load_research_scale_config
    from app.plan.generate import _goal_prompt, _skeleton_prompt

    config = load_research_scale_config({
        "fast": {"source_item_limits": {"hacker_news": 42}}
    })
    scaffold = {"title": "采集", "objective": "采集", "depends_on": []}

    assert _skeleton_prompt("查询", []) == _skeleton_prompt(
        "查询", [], scale="standard", scale_config=config
    )
    assert _goal_prompt("查询", "goal-1", scaffold, []) == _goal_prompt(
        "查询", "goal-1", scaffold, [], scale="standard", scale_config=config
    )
    fast_skeleton = _skeleton_prompt(
        "查询", [], scale="fast", scale_config=config
    )
    fast_goal = _goal_prompt(
        "查询", "goal-1", scaffold, [], scale="fast", scale_config=config
    )
    assert "3–3 个 goal" in fast_skeleton
    assert "每个 goal 采集源最多 2 个" in fast_goal
    assert "hitsPerPage=42" in fast_goal
    assert "hitsPerPage=1000" not in fast_goal


def test_build_plan把档位与跨层采集输入写入快照() -> None:
    from app.config import load_research_scale_config
    from app.plan.generate import _build_plan

    skeleton = _valid_skeleton()
    plan = _build_plan(
        skeleton,
        query="查询",
        research_id="r-fast",
        timestamp="2026-08-21T00:00:00Z",
        scale="fast",
        scale_config=load_research_scale_config(),
    )
    raw = plan.to_dict()

    assert raw["scale"] == "fast"
    collection_path = raw["goals"][0]["agents"][0]["output"]["path"]
    goal_2_inputs = raw["goals"][1]["agents"][0]["inputs"]
    goal_3_inputs = raw["goals"][2]["agents"][0]["inputs"]
    assert {item["artifact"] for item in goal_2_inputs} >= {collection_path}
    assert {item["artifact"] for item in goal_3_inputs} >= {collection_path}


def test_API_scale只接受两档且缺省为standard() -> None:
    from pydantic import ValidationError

    from app.api.main import ResearchRequest

    assert ResearchRequest(query="查询").scale == "standard"
    assert ResearchRequest(query="查询", scale="fast").scale == "fast"
    with pytest.raises(ValidationError):
        ResearchRequest(query="查询", scale="tiny")


def test_MCP把结构化条数映射到源适配器参数() -> None:
    from app.adapters.capability import Capability
    from app.adapters.source_mcp import SourceToolAdapter, stdio_server_config

    received: list[int] = []

    def fake_hn(query: str, window: str, *, limit: int) -> list[dict]:
        del query, window
        received.append(limit)
        return []

    adapter = SourceToolAdapter({"source.hacker_news": fake_hn})
    result = asyncio.run(adapter.call(
        "source.hacker_news",
        "查询",
        "90d",
        research_id="r-fast",
        goal_id="goal-1",
        agent_id="data-collection",
        capability=Capability(
            tools=("source.hacker_news",),
            sources=("hacker_news",),
            network="sources_only",
        ),
        item_limit=42,
    ))

    assert result == []
    assert received == [42]
    config = stdio_server_config(("hacker_news",), item_limit=42, environ={})
    assert config["args"][-2:] == ["--item-limit", "42"]


def test_API_fast计划快照与执行任务都携带快速档条数(tmp_path) -> None:
    from tests.test_m2_wiring import api_client, wait_for_status

    async def scenario() -> None:
        async with api_client(tmp_path, auto_confirm=True) as (_, client, engine):
            created = await client.post(
                "/api/researches",
                json={"query": "飞书竞品优缺点", "scale": "fast"},
                headers={"X-Request-ID": "create-m3g-fast"},
            )
            assert created.status_code == 200, created.text
            research_id = created.json()["data"]["research_id"]
            await wait_for_status(client, research_id, "completed")
            plan = (await client.get(
                f"/api/researches/{research_id}/plan"
            )).json()["data"]

        collection = next(
            task for task in engine.tasks if task.agent_kind == "data_collection"
        )
        assert plan["scale"] == "fast"
        assert len(plan["goals"]) <= 3
        assert collection.source_item_limit == 100
        assert "hitsPerPage=100" in collection.body

    asyncio.run(scenario())


def test_codex_MCP配置显式放行工具审批() -> None:
    """codex-cli ≥0.149 的 MCP 逐次审批在非交互 exec 下默认全拒；
    配置必须携带 default_tools_approval_mode=approve（验收第三轮实证）。"""
    from app.adapters.source_mcp import codex_mcp_args

    args = codex_mcp_args(("hacker_news",))
    assert (
        'mcp_servers.owli_sources.default_tools_approval_mode="approve"' in args
    ), "codex MCP 配置缺少工具审批放行键"
