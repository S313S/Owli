from __future__ import annotations

import asyncio
import json

from tests.plan_factory import make_plan_dict


def _chapter_value(agent, *, chapter_type, inputs, entities):
    return {
        "chapter_type": chapter_type,
        "opening": {
            "inputs": [{"path": path} for path in inputs],
            "task": agent.task,
            "acceptance": ["产物按 output.path 落盘并通过声明的 validators"],
        },
        "closing": {
            "output": {"path": agent.output["path"]},
            "entities": entities,
            "expected_count": 1,
            "notes": {
                "positioning": "产品定位",
                "pricing": "定价",
                "feature_differences": "功能差异",
                "social_proof": "社证",
                "strengths_weaknesses": "强弱",
            },
        },
    }


def test_同_goal线性链仍并发生成_提示词只带派生上游信息(tmp_path):
    from app.config import ChapterEngineConfig, ResilienceConfig
    from app.plan.chapters import generate_chapter_specs
    from app.plan.model import Plan
    from app.plan.segments import PlanSegmentWorkspace

    source = make_plan_dict()
    source["goals"] = source["goals"][:1]
    second = dict(source["goals"][0]["agents"][0])
    second["agent_id"] = "cross-validation"
    second["display_name"] = "交叉验证"
    second["task"] = "对比全部采集章"
    second["depends_on"] = [source["goals"][0]["agents"][0]["agent_id"]]
    second["capability"] = {
        **second["capability"], "profile": "readonly-analyst",
        "tools": ["fs.read", "fs.write", "db.read"], "sources": [],
    }
    second["output"] = {
        "format": "json", "path": "goals/goal-1/cross-validation.json",
        "validators": ["file_exists"],
    }
    source["goals"][0]["agents"].append(second)
    plan = Plan.from_dict(source)
    calls = []
    active = 0
    max_active = 0

    class Adapter:
        async def run_planning_segment(self, request, on_text=None):
            nonlocal active, max_active
            from app.adapters.contracts import PlanningSegmentResult

            calls.append((request.segment_name, request.prompt, request.output_schema))
            index = int(request.segment_name.rsplit("-", 1)[1]) - 1
            agent = plan.goals[0].agents[index]
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            if index == 0:
                value = _chapter_value(
                    agent, chapter_type="collection", inputs=[], entities=["豆包"],
                )
            else:
                value = _chapter_value(
                    agent,
                    chapter_type="cross_validation",
                    inputs=[plan.goals[0].agents[0].output["path"]],
                    entities=["豆包"],
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

    assert [name for name, _, _ in calls] == ["goal-1-ch-1", "goal-1-ch-2"]
    assert max_active == 2
    assert "产品定位" not in calls[0][1]
    assert "产品定位" not in calls[1][1]
    assert "系统从计划树派生的上游信息" in calls[1][1]
    assert plan.goals[0].agents[0].output["path"] in calls[1][1]
    assert "正文" not in calls[1][1]
    assert all(call[2]["required"] == ["chapter_type", "opening", "closing"]
               for call in calls)
    chapter_root = tmp_path / "runs" / plan.research_id / "goals" / "goal-1"
    assert (chapter_root / "ch-1.md").is_file()
    assert (chapter_root / "ch-2.md").is_file()
    assert plan.goals[0].agents[0].chapter["chapter_id"] == "ch-1"
    assert plan.goals[0].agents[0].engine == "codex"
    assert plan.goals[0].agents[1].engine == "claude"


def test_同_goal_无依赖章并发生成(tmp_path):
    from app.config import ChapterEngineConfig, ResilienceConfig
    from app.plan.chapters import generate_chapter_specs
    from app.plan.model import Plan
    from app.plan.segments import PlanSegmentWorkspace

    source = make_plan_dict()
    source["goals"] = source["goals"][:1]
    first = source["goals"][0]["agents"][0]
    second = {**first, "agent_id": "agent-parallel", "depends_on": []}
    second["output"] = {
        **first["output"], "path": "goals/goal-1/agent-parallel.md",
    }
    source["goals"][0]["agents"] = [first, second]
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
                agent, chapter_type="audit", inputs=[], entities=["竞品"],
            )
            text = json.dumps(value, ensure_ascii=False)
            await on_text(text)
            active -= 1
            return PlanningSegmentResult(text, True)

    workspace = PlanSegmentWorkspace(
        tmp_path / "runs" / plan.research_id, ResilienceConfig(3, 60, 900),
    )
    asyncio.run(generate_chapter_specs(
        plan, workspace, Adapter(), ChapterEngineConfig(),
    ))

    assert max_active == 2


def test_对比章必须覆盖全卷全部采集章_错误列出缺章与路径():
    from app.plan.lint import lint

    plan = make_plan_dict()
    first = plan["goals"][0]["agents"][0]
    first["chapter"] = {
        "chapter_id": "ch-1", "chapter_type": "collection",
        "plan_path": "goals/goal-1/ch-1.md",
        "opening": {"inputs": [], "task": first["task"], "acceptance": ["完成"]},
        "closing": {"output": {"path": first["output"]["path"]}, "entities": ["豆包"],
                    "expected_count": 1, "notes": {}},
    }
    compare = plan["goals"][-1]["agents"][-1]
    compare["chapter"] = {
        "chapter_id": "ch-1", "chapter_type": "comparison",
        "plan_path": "goals/goal-3/ch-1.md",
        "opening": {"inputs": [], "task": compare["task"], "acceptance": ["完成"]},
        "closing": {"output": {"path": compare["output"]["path"]}, "entities": ["豆包"],
                    "expected_count": 1, "notes": {}},
    }

    errors = lint(plan)["errors"]
    assert any("规则22" in item and "goal-1/ch-1" in item and first["output"]["path"] in item
               for item in errors)

    compare["chapter"]["opening"]["inputs"] = [{"path": first["output"]["path"]}]
    assert not any("规则22" in item for item in lint(plan)["errors"])


def test_章_inputs_由_depends_on_output_path_确定性派生(tmp_path):
    from app.config import ChapterEngineConfig, ResilienceConfig
    from app.plan.chapters import generate_chapter_specs
    from app.plan.model import Plan
    from app.plan.segments import PlanSegmentWorkspace

    source = make_plan_dict()
    source["goals"] = source["goals"][:1]
    first = source["goals"][0]["agents"][0]
    second = dict(first)
    second["agent_id"] = "data-cleaning"
    second["display_name"] = "数据清洗"
    second["depends_on"] = [first["agent_id"]]
    second["output"] = {
        "format": "json",
        "path": "goals/goal-1/cleaned.json",
        "validators": ["file_exists"],
    }
    source["goals"][0]["agents"] = [first, second]
    plan = Plan.from_dict(source)

    class Adapter:
        async def run_planning_segment(self, request, on_text=None):
            from app.adapters.contracts import PlanningSegmentResult

            agent = plan.goals[0].agents[0 if request.segment_name.endswith("1") else 1]
            value = _chapter_value(
                agent,
                chapter_type="collection" if agent.agent_id == first["agent_id"] else "data_cleaning",
                inputs=[],
                entities=["豆包"],
            )
            text = json.dumps(value, ensure_ascii=False)
            await on_text(text)
            return PlanningSegmentResult(text, True)

    workspace = PlanSegmentWorkspace(
        tmp_path / "runs" / plan.research_id, ResilienceConfig(3, 60, 900),
    )
    asyncio.run(generate_chapter_specs(
        plan, workspace, Adapter(), ChapterEngineConfig(),
    ))

    assert plan.goals[0].agents[1].chapter["opening"]["inputs"] == [
        {"path": first["output"]["path"]}
    ]


def test_规则22_扩到所有消费上游的章类型():
    from app.plan.lint import lint

    plan = make_plan_dict()
    first = plan["goals"][0]["agents"][0]
    second = dict(first)
    second["agent_id"] = "summary"
    second["output"] = {
        "format": "markdown",
        "path": "goals/goal-1/summary.md",
        "validators": ["file_exists"],
    }
    second["depends_on"] = [first["agent_id"]]
    first["chapter"] = {
        "chapter_id": "ch-1", "chapter_type": "collection",
        "plan_path": "goals/goal-1/ch-1.md",
        "opening": {"inputs": [], "task": first["task"], "acceptance": ["完成"]},
        "closing": {"output": {"path": first["output"]["path"]}, "entities": ["豆包"],
                    "expected_count": 1, "notes": {}},
    }
    second["chapter"] = {
        "chapter_id": "ch-2", "chapter_type": "summary",
        "plan_path": "goals/goal-1/ch-2.md",
        "opening": {"inputs": [], "task": second["task"], "acceptance": ["完成"]},
        "closing": {"output": {"path": second["output"]["path"]}, "entities": ["豆包"],
                    "expected_count": 1, "notes": {}},
    }
    plan["goals"][0]["agents"] = [first, second]

    errors = lint(plan)["errors"]
    assert any("规则22" in item and first["output"]["path"] in item for item in errors)

    second["chapter"]["opening"]["inputs"] = [{"path": first["output"]["path"]}]
    assert not any("规则22" in item for item in lint(plan)["errors"])


def test_章引擎默认按结构化类型选择且配置可覆盖():
    from app.config import ChapterEngineConfig

    defaults = ChapterEngineConfig()
    assert defaults.engine_for("comparison") == "claude"
    assert defaults.engine_for("collection") == "codex"

    overridden = ChapterEngineConfig(overrides={"collection": "claude"})
    assert overridden.engine_for("collection") == "claude"


def test_竞品章模板只声明_Owli_源适配器与五组结尾字段():
    from pathlib import Path

    path = Path("app/skills/competitor-profiling/chapter-ending-template.md")
    text = path.read_text(encoding="utf-8")
    assert all(field in text for field in (
        "positioning", "pricing", "feature_differences", "social_proof",
        "strengths_weaknesses",
    ))
    assert "Owli source adapter" in text
    assert "Firecrawl" not in text and "DataForSEO" not in text


def test_采集章强制一个竞品乘一个信息源的实体颗粒度():
    from app.plan.chapters import validate_chapter_value
    from app.plan.model import Agent

    agent = Agent.from_dict(make_plan_dict()["goals"][0]["agents"][0])
    value = _chapter_value(
        agent, chapter_type="collection", inputs=[], entities=["豆包", "讯飞"],
    )
    with __import__("pytest").raises(ValueError, match="竞品 × 信息源"):
        validate_chapter_value(value, agent)


# ---- 2026-08-22 r-99fdccf53cae 取证：围栏与 acceptance 同义写法 ----
import json as _json

from app.plan.segments import _json_payload


def test_json_payload_accepts_fence_without_closing():
    raw = '```json\n{"a": 1}\n'
    assert _json.loads(_json_payload(raw)) == {"a": 1}


def test_json_payload_accepts_bare_fence_and_trailing_text():
    raw = '```\n{"a": 1}\n```\n以上是 JSON。'
    assert _json.loads(_json_payload(raw)) == {"a": 1}


def test_json_payload_keeps_plain_json():
    assert _json_payload('  {"a": 1}  ') == '{"a": 1}'


def test_validate_chapter_accepts_string_acceptance():
    from app.plan.chapters import validate_chapter_value

    class _Agent:
        output = {"path": "goals/goal-3/summary.md"}

    value = {
        "chapter_type": "summary",
        "opening": {"inputs": [], "task": "归纳", "acceptance": "有结论"},
        "closing": {
            "output": {"path": "goals/goal-3/summary.md"},
            "entities": ["豆包语音输入法"],
            "expected_count": None,
            "notes": {},
        },
    }
    assert validate_chapter_value(value, _Agent())["opening"]["acceptance"] == ["有结论"]


def test_章节重试预算按轮独立_跨轮重生成不累加(tmp_path):
    """r-072721cddbb0 取证：第 1 轮语义退回 2 次 + 第 2 轮整份重生成 → 预算耗尽。"""
    from app.config import ChapterEngineConfig, ResilienceConfig
    from app.plan.chapters import generate_chapter_specs
    from app.plan.model import Plan
    from app.plan.segments import PlanSegmentWorkspace

    source = make_plan_dict()
    source["goals"] = source["goals"][:1]
    source["goals"][0]["agents"] = source["goals"][0]["agents"][:1]
    plan = Plan.from_dict(source)
    agent = plan.goals[0].agents[0]
    calls: list[str] = []

    class Adapter:
        async def run_planning_segment(self, request, on_text=None):
            from app.adapters.contracts import PlanningSegmentResult

            calls.append(request.segment_name)
            value = _chapter_value(
                agent, chapter_type="collection", inputs=[], entities=["豆包"],
            )
            # 每轮前两次给语义错误（acceptance 为空数组），第三次才合法
            if len(calls) % 3 != 0:
                value["opening"]["acceptance"] = []
            text = json.dumps(value, ensure_ascii=False)
            await on_text(text)
            return PlanningSegmentResult(text, True)

    workspace = PlanSegmentWorkspace(
        tmp_path / "runs" / plan.research_id, ResilienceConfig(3, 60, 900),
    )
    for _ in range(2):  # 模拟 lint 失败后整份章节重生成两轮；hash 未变应复用
        asyncio.run(generate_chapter_specs(
            plan, workspace, Adapter(), ChapterEngineConfig(),
        ))
    assert calls == ["goal-1-ch-1"] * 3


def test_上游实际closing变化不影响基于派生输入的章节缓存(tmp_path):
    from app.config import ChapterEngineConfig, ResilienceConfig
    from app.plan.chapters import generate_chapter_specs
    from app.plan.model import Plan
    from app.plan.segments import PlanSegmentWorkspace

    source = make_plan_dict()
    source["goals"] = source["goals"][:1]
    first = source["goals"][0]["agents"][0]
    second = {**first, "agent_id": "summary", "display_name": "摘要"}
    second["depends_on"] = [first["agent_id"]]
    second["output"] = {
        "format": "markdown", "shape": "object",
        "path": "goals/goal-1/summary.md", "validators": ["file_exists"],
    }
    source["goals"][0]["agents"] = [first, second]
    plan = Plan.from_dict(source)
    calls = []

    class Adapter:
        async def run_planning_segment(self, request, on_text=None):
            from app.adapters.contracts import PlanningSegmentResult

            calls.append(request.segment_name)
            index = int(request.segment_name.rsplit("-", 1)[1]) - 1
            agent = plan.goals[0].agents[index]
            value = _chapter_value(
                agent,
                chapter_type="collection" if index == 0 else "summary",
                inputs=[], entities=["初版实体"],
            )
            text = json.dumps(value, ensure_ascii=False)
            await on_text(text)
            return PlanningSegmentResult(text, True)

    workspace = PlanSegmentWorkspace(
        tmp_path / "runs" / plan.research_id, ResilienceConfig(3, 60, 900),
    )
    asyncio.run(generate_chapter_specs(
        plan, workspace, Adapter(), ChapterEngineConfig(),
    ))
    upstream = workspace.formal_path("goal-1-ch-1")
    value = json.loads(upstream.read_text(encoding="utf-8"))
    value["closing"]["entities"] = ["新版实体"]
    upstream.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    asyncio.run(generate_chapter_specs(
        plan, workspace, Adapter(), ChapterEngineConfig(),
    ))

    assert calls == ["goal-1-ch-1", "goal-1-ch-2"]


def test_有shell能力的章引擎强制codex_章级默认不覆盖规则7(tmp_path):
    from app.config import ChapterEngineConfig, ResilienceConfig
    from app.plan.chapters import generate_chapter_specs
    from app.plan.model import Plan
    from app.plan.segments import PlanSegmentWorkspace

    source = make_plan_dict()
    source["goals"] = source["goals"][:1]
    source["goals"][0]["agents"] = source["goals"][0]["agents"][:1]
    source["goals"][0]["agents"][0]["capability"]["shell"] = "workspace"
    plan = Plan.from_dict(source)
    agent = plan.goals[0].agents[0]

    class Adapter:
        async def run_planning_segment(self, request, on_text=None):
            from app.adapters.contracts import PlanningSegmentResult

            value = _chapter_value(
                agent, chapter_type="comparison", inputs=[], entities=["豆包"],
            )
            text = json.dumps(value, ensure_ascii=False)
            await on_text(text)
            return PlanningSegmentResult(text, True)

    workspace = PlanSegmentWorkspace(
        tmp_path / "runs" / plan.research_id, ResilienceConfig(3, 60, 900),
    )
    asyncio.run(generate_chapter_specs(
        plan, workspace, Adapter(), ChapterEngineConfig(),
    ))
    assert agent.engine == "codex"
