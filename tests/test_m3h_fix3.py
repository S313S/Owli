from __future__ import annotations

from copy import deepcopy
import asyncio
import json

import pytest

from tests.plan_factory import make_agent, make_plan_dict


def _collector(
    agent_id: str,
    goal_id: str,
    source_id: str,
    entity: str,
) -> dict:
    agent = make_agent(agent_id, goal_id)
    agent["display_name"] = f"网页搜索数据抓取·{entity}"
    agent["entity"] = entity
    agent["chapter"] = None
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


def _chapter(agent: dict, chapter_type: str, inputs: list[str], entities: list[str]) -> dict:
    return {
        "chapter_id": "ch-1",
        "chapter_type": chapter_type,
        "plan_path": f"goals/goal-1/{agent['agent_id']}.md",
        "opening": {
            "inputs": [{"path": path} for path in inputs],
            "task": agent["task"],
            "acceptance": ["产物通过结构校验"],
        },
        "closing": {
            "output": {"path": agent["output"]["path"]},
            "entities": entities,
            "expected_count": 1,
            "notes": {},
        },
    }


def test_规则21_同源不同实体跨goal合法() -> None:
    from app.plan.lint import lint

    plan = make_plan_dict()
    plan["subjects"] = ["豆包", "讯飞"]
    plan["subjects_justification"] = "主体与主要竞品。"
    plan["goals"][0]["agents"] = [
        _collector("data-collection", "goal-1", "web_search", "豆包")
    ]
    plan["goals"][1]["agents"] = [
        _collector("data-collection-2", "goal-2", "web_search", "讯飞")
    ]

    result = lint(plan)
    assert not [
        item for item in result["errors"] if item.startswith("[规则21]")
    ]
    assert not [
        item for item in result["warnings"] if item.startswith("[警告5]")
    ]


def test_规则21_同源同实体重复必须拦截并点名实体() -> None:
    from app.plan.lint import duplicate_collection_goal_ids, lint

    plan = make_plan_dict()
    plan["subjects"] = ["豆包"]
    plan["subjects_justification"] = "研究主体。"
    plan["goals"][0]["agents"] = [
        _collector("data-collection", "goal-1", "web_search", "豆包")
    ]
    plan["goals"][2]["agents"] = [
        _collector("data-collection-2", "goal-3", "web_search", "豆包")
    ]

    errors = [
        item for item in lint(plan)["errors"] if item.startswith("[规则21]")
    ]
    assert duplicate_collection_goal_ids(plan) == {"goal-3"}
    assert len(errors) == 1
    assert "source_id=web_search" in errors[0]
    assert "entity=豆包" in errors[0]


def test_fast_goal提示词写明章数预算与必采清单() -> None:
    from app.config import load_research_scale_config
    from app.plan.generate import _goal_prompt

    prompt = _goal_prompt(
        "竞品分析",
        "goal-2",
        {"title": "竞品采集", "objective": "采集竞品", "depends_on": []},
        [],
        subjects=["主体", "竞品甲", "竞品乙"],
        scale="fast",
        scale_config=load_research_scale_config(),
    )

    assert "本 goal 章数上限为 4" in prompt
    # §PLAN-1：「多源合并为一章」在代码里本就不存在（一 agent 一源），
    # 出路改为系统预分配的必采清单；同源不同实体跨 goal 仍允许。
    assert "采集清单已由系统按预算分配" in prompt
    assert "本 goal 必采清单" in prompt
    assert "同源不同实体允许跨 goal 采集" in prompt
    assert "在章数预算内，优先一实体一源" in prompt
    assert "必须把 agents 拆到一项只负责一个竞品与一个信息源" not in prompt


def test_骨架subjects与justification被校验并进入scaffold() -> None:
    from app.plan.generate import _skeleton_scaffolds, _skeleton_prompt

    value = {
        "market_profile": "cn_product",
        "market_profile_justification": "主要市场在中国大陆。",
        "subjects": ["主体", "竞品甲"],
        "subjects_justification": "包含主体自身与直接竞品。",
        "goals": [
            {"title": f"阶段{index}", "objective": "形成产物", "depends_on": []}
            for index in range(1, 4)
        ],
    }

    scaffolds = _skeleton_scaffolds(value)
    prompt = _skeleton_prompt("竞品分析", [])

    assert scaffolds[0]["subjects"] == ["主体", "竞品甲"]
    assert scaffolds[0]["subjects_justification"] == "包含主体自身与直接竞品。"
    assert "subjects" in prompt and "subjects_justification" in prompt


def test_采集agent实体由名称分隔符确定性提取_非采集为null() -> None:
    from collections import Counter

    from app.plan.generate import _build_agent

    collector = _build_agent(
        {"name": "网页搜索数据抓取·竞品甲", "task": "采集", "output": {"shape": "array"}},
        "goal-1",
        [],
        "竞品分析",
        Counter(),
        previous_agent_id=None,
        upstream_artifacts={},
    )
    analyst = _build_agent(
        {"name": "数据清洗", "task": "清洗", "output": {"shape": "object"}},
        "goal-1",
        [],
        "竞品分析",
        Counter(),
        previous_agent_id=None,
        upstream_artifacts={},
    )

    assert collector["entity"] == "竞品甲"
    assert analyst["entity"] is None


def test_采集章closing实体必须与agent实体一致() -> None:
    from app.plan.chapters import validate_chapter_value
    from app.plan.model import Agent

    agent = Agent.from_dict(
        _collector("data-collection", "goal-1", "web_search", "竞品甲")
    )
    value = {
        "chapter_type": "collection",
        "opening": {
            "inputs": [],
            "task": "采集竞品甲",
            "acceptance": ["至少返回 1 条记录"],
        },
        "closing": {
            "output": {"path": agent.output["path"]},
            "entities": ["其他实体"],
            "expected_count": 1,
            "notes": {},
        },
    }

    with pytest.raises(ValueError, match="agent.entity"):
        validate_chapter_value(value, agent)


def test_完整计划保存subjects且采集章实体覆盖它们(tmp_path) -> None:
    from tests.test_plan_generate import _generate, _valid_skeleton

    plan, _, _ = _generate(tmp_path, [_valid_skeleton()])
    collected = {
        entity
        for goal in plan.goals
        for agent in goal.agents
        if agent.chapter and agent.chapter["chapter_type"] == "collection"
        for entity in agent.chapter["closing"]["entities"]
    }

    assert plan.subjects == ["飞书"]
    assert collected >= set(plan.subjects)


def test_规则25_段级缺实体必须报error且消息头锚定goal() -> None:
    from app.plan.lint import lint

    plan = make_plan_dict()
    plan["subjects"] = ["豆包", "讯飞"]
    plan["subjects_justification"] = "主体与直接竞品。"
    plan["goals"][0]["agents"] = [
        _collector("data-collection", "goal-1", "web_search", "豆包")
    ]

    errors = [
        item for item in lint(plan)["errors"] if item.startswith("[规则25]")
    ]
    assert len(errors) == 1
    assert errors[0].startswith("[规则25] goal-3/")
    assert "讯飞" in errors[0]


def test_规则26_非采集章entities不得超出inputs可达采集实体() -> None:
    from app.plan.lint import lint

    plan = make_plan_dict()
    plan["subjects"] = ["豆包", "讯飞"]
    plan["subjects_justification"] = "主体与直接竞品。"
    collector = _collector("data-collection", "goal-1", "web_search", "豆包")
    collector["chapter"] = _chapter(collector, "collection", [], ["豆包"])
    consumer = deepcopy(plan["goals"][1]["agents"][0])
    consumer["entity"] = None
    consumer["chapter"] = _chapter(
        consumer,
        "comparison",
        [collector["output"]["path"]],
        ["豆包", "讯飞"],
    )
    plan["goals"][0]["agents"] = [collector]
    plan["goals"][1]["agents"] = [consumer]

    all_errors = lint(plan)["errors"]
    rule_25 = [item for item in all_errors if item.startswith("[规则25]")]
    rule_26 = [item for item in all_errors if item.startswith("[规则26]")]
    assert len(rule_25) == 1 and "讯飞" in rule_25[0]
    assert len(rule_26) == 1 and "讯飞" in rule_26[0]
    assert "请为该实体补采集章，或从本章 entities 中删除" in rule_26[0]


def test_规则23_无章data_collection_agent也能段级拦不适用源() -> None:
    from app.plan.lint import lint

    plan = make_plan_dict()
    plan["market_profile"] = "cn_product"
    plan["market_profile_justification"] = "主要市场在中国大陆。"
    bad = _collector("hn-collector", "goal-1", "hacker_news", "主体")
    bad["agent_kind"] = "data_collection"
    bad["capability"]["profile"] = "readonly-analyst"
    plan["goals"][0]["agents"] = [bad]

    errors = lint(plan)["errors"]
    assert any(
        item.startswith("[规则23] goal-1/hn-collector")
        and "hacker_news" in item
        for item in errors
    )


def _planning_sdk(messages_by_call: list[list[object]]):
    class TextBlock:
        def __init__(self, text: str):
            self.text = text

    class AssistantMessage:
        def __init__(self, text: str):
            self.content = [TextBlock(text)]

    class ResultMessage:
        def __init__(
            self,
            *,
            is_error: bool,
            structured_output=None,
            api_error_status=None,
            subtype="success",
            stop_reason="stop_sequence",
            cause=None,
            marker="完整原文",
        ):
            self.is_error = is_error
            self.structured_output = structured_output
            self.api_error_status = api_error_status
            self.subtype = subtype
            self.stop_reason = stop_reason
            self.cause = cause
            self.marker = marker

    class Options:
        def __init__(self, **values):
            self.values = values

    class Client:
        calls = 0

        def __init__(self, options):
            self.options = options
            self.index = Client.calls
            Client.calls += 1

        async def connect(self, prompt):
            async for _ in prompt:
                pass

        async def receive_response(self):
            for message in messages_by_call[self.index]:
                yield message

        async def disconnect(self):
            pass

    class Sdk:
        ClaudeAgentOptions = Options
        ClaudeSDKClient = Client

    Sdk.TextBlock = TextBlock
    Sdk.AssistantMessage = AssistantMessage
    Sdk.ResultMessage = ResultMessage
    return Sdk, AssistantMessage, ResultMessage


def test_规划短流_is_error_true但产物合法仍成功且完整ResultMessage落盘(tmp_path) -> None:
    from app.adapters.claude import ClaudeAdapter
    from app.adapters.contracts import PlanningSegmentRequest

    messages = [[]]
    sdk, _, result_type = _planning_sdk(messages)
    message = result_type(
        is_error=True,
        structured_output={"ok": True},
        marker="X" * 1200,
    )
    messages[0].append(message)
    adapter = ClaudeAdapter(sdk=sdk, log_root=tmp_path / "engine-errors")

    result = asyncio.run(adapter.generate_plan_segment(
        PlanningSegmentRequest(
            "r-fix3",
            "goal-1-ch-1",
            "只输出 JSON",
            output_schema={
                "type": "object",
                "required": ["ok"],
                "properties": {"ok": {"const": True}},
            },
        )
    ))

    assert result.completed is True
    assert json.loads(result.text) == {"ok": True}
    lines = list((tmp_path / "engine-errors").glob("claude-*.jsonl"))
    assert len(lines) == 1
    raw = lines[0].read_text(encoding="utf-8")
    assert "X" * 1200 in raw


def test_规划短流产物不可用时按api状态归因() -> None:
    from app.adapters.claude import ClaudeAdapter
    from app.adapters.contracts import PlanningSegmentRequest

    messages = [[]]
    sdk, assistant_type, result_type = _planning_sdk(messages)
    messages[0].extend([
        assistant_type("not-json"),
        result_type(is_error=True, api_error_status=529),
    ])

    result = asyncio.run(ClaudeAdapter(sdk=sdk).generate_plan_segment(
        PlanningSegmentRequest("r-fix3", "goal-1", "只输出 JSON")
    ))

    assert result.completed is False
    assert result.cause == "service"
    assert "产物" in str(result.error)


def test_规划短流产物不可用时按ResultMessage_cause归因() -> None:
    from app.adapters.claude import ClaudeAdapter
    from app.adapters.contracts import PlanningSegmentRequest

    messages = [[]]
    sdk, assistant_type, result_type = _planning_sdk(messages)
    messages[0].extend([
        assistant_type("not-json"),
        result_type(
            is_error=True,
            subtype="error",
            stop_reason=None,
            cause="rate_limit",
        ),
    ])

    result = asyncio.run(ClaudeAdapter(sdk=sdk).generate_plan_segment(
        PlanningSegmentRequest("r-fix3", "goal-1", "只输出 JSON")
    ))

    assert result.completed is False
    assert result.cause == "rate_limit"


def test_stop_sequence正常组合不归engine_error且不消耗段预算(tmp_path) -> None:
    from app.adapters.claude import ClaudeAdapter
    from app.config import ResilienceConfig
    from app.plan.segments import PlanSegmentWorkspace

    messages = [[], []]
    sdk, assistant_type, result_type = _planning_sdk(messages)
    messages[0].extend([
        assistant_type("not-json"),
        result_type(is_error=True, cause="engine_error"),
    ])
    messages[1].extend([
            result_type(is_error=False, structured_output={"ok": True}),
        ])
    claude = ClaudeAdapter(sdk=sdk, log_root=tmp_path / "engine-errors")

    class Adapter:
        async def run_planning_segment(self, request, on_text=None):
            return await claude.generate_plan_segment(request, on_text=on_text)

    workspace = PlanSegmentWorkspace(
        tmp_path / "runs" / "r-stop-sequence",
        ResilienceConfig(1, 1, 1),
        retry_sleep=lambda _: asyncio.sleep(0),
    )
    value = asyncio.run(workspace.generate("goal-1", "生成 JSON", Adapter()))

    assert value == {"ok": True}
    assert workspace._attempts["goal-1"] == 1
