"""§PLAN-1 货 3：第二轮起带上一轮原文，只改报错处——整段重写才是「四跑撞四条规则」的来源。"""

from __future__ import annotations

from tests.test_plan_generate import _generate, _valid_skeleton


def test_goal段重试带上一轮原文与逐字保留指令_首轮不带(tmp_path) -> None:
    invalid = _valid_skeleton()
    invalid["goals"][0]["acceptance"] = ["结果质量良好"]
    _, _, engine = _generate(tmp_path, [invalid, _valid_skeleton()])
    goal_tasks = [t for t in engine.tasks if t.output_path.name.startswith("goal-")]
    first = goal_tasks[0].body
    assert "逐字保留" not in first and "上一轮本段 JSON 原文=" not in first
    retry = next(t.body for t in goal_tasks[3:] if "[规则4]" in t.body)
    assert "上一轮本段 JSON 原文=" in retry
    assert '"结果质量良好"' in retry  # 原文本身在提示词里
    assert "只修改上述报错点名的字段，其余字段逐字保留，仍输出完整 JSON" in retry


def test_章提示词只在有报错且有上一轮原文时带补丁指令() -> None:
    from app.plan.chapters import _prompt
    from app.plan.model import Plan
    from tests.plan_factory import make_plan_dict

    agent = Plan.from_dict(make_plan_dict()).goals[0].agents[0]
    assert "逐字保留" not in _prompt(agent, {}, [], '{"x":1}')
    assert "逐字保留" not in _prompt(agent, {}, ["错"], None)
    text = _prompt(agent, {}, ["错"], '{"x":1}')
    assert '上一轮本章 JSON 原文={"x":1}' in text and "逐字保留" in text


def test_章内层语义重试第二轮带上一轮原文(tmp_path) -> None:
    import asyncio
    import json

    from app.config import ChapterEngineConfig, ResilienceConfig
    from app.plan.chapters import generate_chapter_specs
    from app.plan.model import Plan
    from app.plan.segments import PlanSegmentWorkspace
    from tests.plan_factory import make_plan_dict
    from tests.test_m3h_chapters import _chapter_value

    source = make_plan_dict()
    source["goals"] = source["goals"][:1]
    plan = Plan.from_dict(source)
    agent = plan.goals[0].agents[0]
    prompts: list[str] = []

    class Adapter:
        async def run_planning_segment(self, request, on_text=None):
            from app.adapters.contracts import PlanningSegmentResult

            prompts.append(request.prompt)
            value = _chapter_value(agent, chapter_type="audit", inputs=[], entities=[])
            if len(prompts) == 1:
                value["closing"]["output"]["path"] = "wrong/path.md"  # 语义校验必红
            text = json.dumps(value, ensure_ascii=False)
            await on_text(text)
            return PlanningSegmentResult(text, True)

    workspace = PlanSegmentWorkspace(tmp_path / "runs" / plan.research_id, ResilienceConfig(3, 60, 900))
    asyncio.run(generate_chapter_specs(plan, workspace, Adapter(), ChapterEngineConfig()))
    assert len(prompts) == 2
    assert "逐字保留" not in prompts[0]
    assert "上一轮本章 JSON 原文=" in prompts[1] and "wrong/path.md" in prompts[1]
