from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from pathlib import Path

import pytest

from tests.plan_factory import make_plan_dict


def _collector(agent: dict, goal_id: str, agent_id: str, entity: str) -> dict:
    agent = dict(agent)
    agent["agent_id"] = agent_id
    agent["display_name"] = f"网页搜索数据抓取·{entity}"
    agent["entity"] = entity
    agent["depends_on"] = []
    agent["inputs"] = []
    agent["capability"] = {
        **agent["capability"],
        "profile": "web-collector",
        "tools": ["source.web_search", "fs.write", "db.write"],
        "sources": ["web_search"],
        "network": "sources_only",
    }
    agent["output"] = {
        "format": "json",
        "shape": "array",
        "path": f"goals/{goal_id}/{agent_id}.json",
        "validators": ["file_exists", "json_array_min_items:1"],
    }
    agent["chapter"] = None
    return agent


def _analyst(agent: dict, goal_id: str, agent_id: str) -> dict:
    agent = dict(agent)
    agent["agent_id"] = agent_id
    agent["display_name"] = "交叉验证"
    agent["entity"] = None
    agent["depends_on"] = []
    agent["inputs"] = []
    agent["capability"] = {
        **agent["capability"],
        "profile": "readonly-analyst",
        "tools": ["fs.read", "fs.write", "db.read"],
        "sources": [],
    }
    agent["output"] = {
        "format": "json",
        "shape": "object",
        "path": f"goals/{goal_id}/{agent_id}.json",
        "validators": ["file_exists"],
    }
    agent["chapter"] = None
    return agent


def _chapter_value(agent, chapter_type: str, entities: list[str]) -> dict:
    return {
        "chapter_type": chapter_type,
        "opening": {
            "inputs": [],
            "task": agent.task,
            "acceptance": ["产物按声明路径落盘"],
        },
        "closing": {
            "output": {"path": agent.output["path"]},
            "entities": entities,
            "expected_count": 1,
            "notes": {},
        },
    }


def _cross_goal_plan():
    from app.plan.model import Plan

    source = make_plan_dict()
    source["goals"][0]["agents"] = [
        _collector(source["goals"][0]["agents"][0], "goal-1", "data-collection", "豆包")
    ]
    source["goals"][1]["agents"] = [
        _collector(source["goals"][1]["agents"][0], "goal-2", "data-collection-2", "讯飞")
    ]
    source["goals"][2]["agents"] = [
        _analyst(source["goals"][2]["agents"][0], "goal-3", "cross-validation")
    ]
    return Plan.from_dict(source)


def test_全卷采集清单由plan结构确定性派生() -> None:
    from app.plan.chapters import _collection_inventory

    plan = _cross_goal_plan()

    assert _collection_inventory(plan) == [
        {
            "goal_id": "goal-1",
            "agent_id": "data-collection",
            "output": {"path": "goals/goal-1/data-collection.json"},
            "entity": "豆包",
        },
        {
            "goal_id": "goal-2",
            "agent_id": "data-collection-2",
            "output": {"path": "goals/goal-2/data-collection-2.json"},
            "entity": "讯飞",
        },
    ]


def test_cross_validation系统补齐全卷采集路径后规则22通过(tmp_path) -> None:
    from app.config import ChapterEngineConfig, ResilienceConfig
    from app.plan.chapters import generate_chapter_specs
    from app.plan.lint import lint
    from app.plan.segments import PlanSegmentWorkspace

    plan = _cross_goal_plan()
    prompts: list[str] = []

    class Adapter:
        async def run_planning_segment(self, request, on_text=None):
            from app.adapters.contracts import PlanningSegmentResult

            prompts.append(request.prompt)
            goal_number = int(request.segment_name.split("-")[1]) - 1
            agent = plan.goals[goal_number].agents[0]
            is_collection = agent.entity is not None
            value = _chapter_value(
                agent,
                "collection" if is_collection else "cross_validation",
                [agent.entity] if is_collection else ["豆包", "讯飞"],
            )
            if not is_collection:
                value["opening"]["inputs"] = [
                    {"path": "goals/goal-1/data-collection-3.json"}
                ]
            text = json.dumps(value, ensure_ascii=False)
            await on_text(text)
            return PlanningSegmentResult(text, True)

    workspace = PlanSegmentWorkspace(
        tmp_path / "runs" / plan.research_id,
        ResilienceConfig(3, 60, 900),
    )
    asyncio.run(generate_chapter_specs(
        plan, workspace, Adapter(), ChapterEngineConfig(),
    ))

    expected = [
        {"path": "goals/goal-1/data-collection.json"},
        {"path": "goals/goal-2/data-collection-2.json"},
    ]
    cross = plan.goals[2].agents[0]
    assert cross.chapter["opening"]["inputs"] == expected
    assert lint(plan)["errors"] == []
    assert "全卷采集章清单" in prompts[-1]
    assert "不得自造路径" in prompts[-1]
    assert all(item["path"] in prompts[-1] for item in expected)


def test_全卷补全不破坏同goal章节全并发(tmp_path) -> None:
    from app.config import ChapterEngineConfig, ResilienceConfig
    from app.plan.chapters import generate_chapter_specs
    from app.plan.model import Plan
    from app.plan.segments import PlanSegmentWorkspace

    source = make_plan_dict()
    source["goals"] = source["goals"][:1]
    base = source["goals"][0]["agents"][0]
    source["goals"][0]["agents"] = [
        _collector(base, "goal-1", "data-collection", "豆包"),
        _collector(base, "goal-1", "data-collection-2", "讯飞"),
        _analyst(base, "goal-1", "cross-validation"),
    ]
    plan = Plan.from_dict(source)
    active = 0
    max_active = 0

    class Adapter:
        async def run_planning_segment(self, request, on_text=None):
            nonlocal active, max_active
            from app.adapters.contracts import PlanningSegmentResult

            index = int(request.segment_name.rsplit("-", 1)[1]) - 1
            agent = plan.goals[0].agents[index]
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            value = _chapter_value(
                agent,
                "collection" if agent.entity else "cross_validation",
                [agent.entity] if agent.entity else ["豆包", "讯飞"],
            )
            text = json.dumps(value, ensure_ascii=False)
            await on_text(text)
            active -= 1
            return PlanningSegmentResult(text, True)

    workspace = PlanSegmentWorkspace(
        tmp_path / "runs" / plan.research_id,
        ResilienceConfig(3, 60, 900),
    )
    asyncio.run(generate_chapter_specs(
        plan, workspace, Adapter(), ChapterEngineConfig(),
    ))

    assert max_active == 3
    assert plan.goals[0].agents[2].chapter["opening"]["inputs"] == [
        {"path": "goals/goal-1/data-collection.json"},
        {"path": "goals/goal-1/data-collection-2.json"},
    ]


def _fix5_retry_case(tmp_path: Path, *, always_bad: bool):
    from app.adapters.routing import RoutedAdapter
    from tests.test_plan_generate import (
        FakeEngine,
        FakeStore,
        ForbiddenEngine,
        _agent,
        _valid_skeleton,
    )

    valid = _valid_skeleton()
    # 节化章 shape 恒 object（agents-spec §2.3.1，规则 27）。
    valid["goals"][1]["deliverable"]["shape"] = "object"
    valid["goals"][1]["agents"] = [
        _agent(
            "交叉验证",
            "交叉验证全卷采集证据",
            output={"shape": "object"},
        )
    ]
    invalid_first = deepcopy(valid)
    invalid_first["goals"][0]["acceptance"] = ["结果质量良好"]
    invalid_second = deepcopy(invalid_first)

    class ChapterFlakyEngine(FakeEngine):
        def __init__(self, skeletons):
            super().__init__(skeletons)
            self.target_calls = 0
            self.untouched_mtimes: list[int] = []

        async def run(self, task, ctx, on_event=None):
            result = await super().run(task, ctx, on_event)
            if task.output_path.stem != "goal-2-ch-1":
                return result
            self.target_calls += 1
            payload = json.loads(task.output_path.read_text(encoding="utf-8"))
            payload["closing"]["entities"] = (
                ["幽灵竞品"]
                if always_bad or self.target_calls == 1
                else ["飞书"]
            )
            task.output_path.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            untouched = (
                tmp_path / "runs" / "r-01JXPLAN0000000000000000"
                / "goals" / "goal-1" / "ch-1.md"
            )
            self.untouched_mtimes.append(untouched.stat().st_mtime_ns)
            return result

    engine = ChapterFlakyEngine([invalid_first, invalid_second, valid])
    store = FakeStore(tmp_path)
    adapter = RoutedAdapter(
        adapters={"claude": engine, "codex": ForbiddenEngine()},
    )
    return store, adapter, engine


def test_段级用满预算后章级仍有独立重试(tmp_path) -> None:
    from app.config import ResilienceConfig
    from app.plan.generate import generate_plan

    store, adapter, engine = _fix5_retry_case(tmp_path, always_bad=False)
    plan = asyncio.run(generate_plan(
        "飞书竞品优缺点",
        store,
        adapter,
        ResilienceConfig(
            3,
            1,
            1,
            plan_chapter_lint_retries=2,
        ),
        segment_retry_sleep=lambda _: asyncio.sleep(0),
    ))

    assert plan.status == "awaiting_review"
    assert engine._goal_calls == {1: 3, 2: 1, 3: 1}
    assert engine.target_calls == 2


def test_章级失败只重生成被点名章且其他章mtime不变(tmp_path) -> None:
    from app.config import ResilienceConfig
    from app.plan.generate import generate_plan

    store, adapter, engine = _fix5_retry_case(tmp_path, always_bad=False)
    asyncio.run(generate_plan(
        "飞书竞品优缺点",
        store,
        adapter,
        ResilienceConfig(
            3,
            1,
            1,
            plan_chapter_lint_retries=2,
        ),
        segment_retry_sleep=lambda _: asyncio.sleep(0),
    ))

    chapter_calls = [task.output_path.stem for task in engine.chapter_tasks]
    assert chapter_calls.count("goal-2-ch-1") == 2
    assert chapter_calls.count("goal-1-ch-1") == 1
    assert chapter_calls.count("goal-3-ch-1") == 1
    assert len(engine.untouched_mtimes) == 2
    assert len(set(engine.untouched_mtimes)) == 1
    assert engine._goal_calls == {1: 3, 2: 1, 3: 1}


def test_章级lint耗尽reason可机读并保留末轮原文(tmp_path) -> None:
    from app.config import ResilienceConfig
    from app.plan.generate import PlanGenerationError, generate_plan

    store, adapter, engine = _fix5_retry_case(tmp_path, always_bad=True)
    with pytest.raises(PlanGenerationError) as captured:
        asyncio.run(generate_plan(
            "飞书竞品优缺点",
            store,
            adapter,
            ResilienceConfig(
                3,
                1,
                1,
                plan_chapter_lint_retries=2,
            ),
            segment_retry_sleep=lambda _: asyncio.sleep(0),
        ))

    message = str(captured.value)
    assert captured.value.reason == "chapter_lint_not_converged"
    assert "reason=chapter_lint_not_converged" in message
    assert "章级 lint 未收敛" in message
    assert "[规则26] goal-2/ch-1" in message
    assert "幽灵竞品" in message
    assert engine.target_calls == 2
    assert store.saved == []
