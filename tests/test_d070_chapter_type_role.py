"""§D-070：章类型与 agent 结构化职能确定矛盾时，在章规格落地处机械改回。

取证 r-82ac9ebbd0f0：goal-3「报告撰写」agent（report-writing-3）的任务写着「……口碑与用户评价采集」
节化文档，被标成 collection → 选了采集章引擎 codex、规则 22 让 8 个交叉验证 / 一致性章去
覆盖它的产物，重试只改报错的消费章、不改错标的撰写章，章级 lint 连续不收敛。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.adapters.contracts import PlanningSegmentResult
from app.config import ChapterEngineConfig, ResilienceConfig
from app.plan.chapters import generate_chapter_specs, validate_chapter_value
from app.plan.model import Agent, Plan
from app.plan.segments import PlanSegmentWorkspace
from tests.plan_factory import make_agent, make_plan_dict

FIXTURE = Path(__file__).parent / "fixtures" / "d070" / "r-82ac9ebbd0f0"


def _agent(agent_id: str, profile: str, *, entity: str | None = None) -> Agent:
    data = make_agent(agent_id, "goal-1")
    data["capability"]["profile"] = profile
    data["entity"] = entity
    return Agent.from_dict(data)


def _value(agent: Agent, chapter_type: str, entities: list[str]) -> dict:
    return {
        "chapter_type": chapter_type,
        "opening": {"inputs": [], "task": agent.task, "acceptance": ["按 output.path 落盘"]},
        "closing": {
            "output": {"path": agent.output["path"]},
            "entities": entities,
            "expected_count": None,
            "notes": {"k": "v"},
        },
    }


def test_撰写agent被标collection_改回report并留校正记录() -> None:
    agent = _agent("report-writing-3", "report-writer")
    value = validate_chapter_value(_value(agent, "collection", ["Kimi"]), agent)
    assert value["chapter_type"] == "report"
    assert value["closing"]["notes"] == {
        "k": "v",
        "chapter_type_correction": {
            "from": "collection", "to": "report", "by": "agent_kind=report_writing",
        },
    }


def test_采集agent被标report_改回collection且仍守实体颗粒度() -> None:
    agent = _agent("data-collection-2", "web-collector", entity="Kimi")
    value = validate_chapter_value(_value(agent, "report", ["Kimi"]), agent)
    assert value["chapter_type"] == "collection"
    assert value["closing"]["notes"]["chapter_type_correction"] == {
        "from": "report", "to": "collection", "by": "profile=web-collector",
    }
    # 改回 collection 后采集章的实体约束照常生效，不因校正放行。
    with pytest.raises(ValueError, match="agent.entity"):
        validate_chapter_value(_value(agent, "report", ["豆包"]), agent)


@pytest.mark.parametrize(("agent_id", "profile", "chapter_type"), [
    ("report-writing", "report-writer", "summary"),
    ("summary-2", "report-writer", "report"),
    ("report-writing-2", "report-writer", "comparison"),
    ("cross-validation", "readonly-analyst", "comparison"),
    ("consistency-check", "readonly-analyst", "audit"),
    ("goal-planning", "readonly-analyst", "report"),
])
def test_非collection章类型之间互换_不校正(agent_id, profile, chapter_type) -> None:
    agent = _agent(agent_id, profile)
    value = validate_chapter_value(_value(agent, chapter_type, []), agent)
    assert value["chapter_type"] == chapter_type
    assert "chapter_type_correction" not in value["closing"]["notes"]


@pytest.mark.parametrize(("agent_id", "profile"), [
    ("browser-automation", "sandboxed-runner"),  # MediaCrawler 这类本来就在采集
    ("agent-1", "report-writer"),  # 自定义 id：职能认不出，不按 profile 兜底猜
    ("agent-1", "readonly-analyst"),
])
def test_职能认不出或可能在采集的agent标collection_不校正(agent_id, profile) -> None:
    agent = _agent(agent_id, profile)
    value = validate_chapter_value(_value(agent, "collection", ["Kimi"]), agent)
    assert value["chapter_type"] == "collection"
    assert "chapter_type_correction" not in value["closing"]["notes"]


def test_无唯一目标的非采集职能标collection_退回语义重试() -> None:
    agent = _agent("consistency-check-2", "readonly-analyst")
    with pytest.raises(ValueError, match="chapter_type 不能是 collection.*consistency_check"):
        validate_chapter_value(_value(agent, "collection", ["Kimi"]), agent)


def test_已校正的章再校验幂等() -> None:
    agent = _agent("report-writing-3", "report-writer")
    once = validate_chapter_value(_value(agent, "collection", []), agent)
    twice = validate_chapter_value(once, agent)
    assert twice == once


def test_章生成路径_校正先于引擎选型_撰写章走claude(tmp_path) -> None:
    source = make_plan_dict()
    source["goals"] = source["goals"][:1]
    writer = make_agent("report-writing", "goal-1")
    writer["capability"]["profile"] = "report-writer"
    source["goals"][0]["agents"] = [writer]
    plan = Plan.from_dict(source)
    agent = plan.goals[0].agents[0]

    class Adapter:
        async def run_planning_segment(self, request, on_text=None):
            text = json.dumps(_value(agent, "collection", []), ensure_ascii=False)
            await on_text(text)
            return PlanningSegmentResult(text, True)

    workspace = PlanSegmentWorkspace(tmp_path / "runs" / plan.research_id, ResilienceConfig(3, 60, 900))
    asyncio.run(generate_chapter_specs(plan, workspace, Adapter(), ChapterEngineConfig()))
    assert agent.chapter["chapter_type"] == "report"
    assert agent.engine == "claude"
    rendered = (tmp_path / "runs" / plan.research_id / "goals" / "goal-1" / "ch-1.md").read_text(encoding="utf-8")
    assert "chapter_type_correction" in rendered
    # 原始模型输出原样留在 plan-segments，校正只在计划里。
    raw = json.loads(workspace.formal_path("goal-1-ch-1").read_text(encoding="utf-8"))
    assert raw["chapter_type"] == "collection"


def test_r82ac夹具回放_规则22清零_撰写章与同位章同类型同引擎() -> None:
    """真夹具离线回放章阶段（同 scripts/acceptance/d070/chapter_type_ruler.py）；期望人工写死。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "d070_ruler",
        Path(__file__).parents[1] / "scripts" / "acceptance" / "d070" / "chapter_type_ruler.py",
    )
    ruler = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ruler)
    reading = ruler.measure(FIXTURE)
    chapters = reading["chapters"]
    assert not reading["generate_error"] and not reading["missing_segments"]
    assert reading["rule22"] == [] and reading["lint_errors"] == []
    assert chapters["goal-3/ch-5"]["agent_id"] == "report-writing-3"
    assert [chapters[k]["chapter_type"] for k in ("goal-2/ch-5", "goal-3/ch-5", "goal-5/ch-5")] == ["report"] * 3
    assert [chapters[k]["engine"] for k in ("goal-2/ch-5", "goal-3/ch-5", "goal-5/ch-5")] == ["claude"] * 3
    assert [k for k, v in chapters.items() if v["correction"]] == ["goal-3/ch-5"]
