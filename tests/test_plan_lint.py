from __future__ import annotations

from copy import deepcopy

import pytest

from tests.plan_factory import make_agent, make_plan_dict


def _messages(plan: dict) -> list[str]:
    from app.plan.lint import lint

    return lint(plan, for_approval=True)["errors"]


@pytest.mark.parametrize(
    ("rule", "mutate", "location"),
    [
        (1, lambda p: p["goals"][1].update(goal_id="goal-1"), "goal-1"),
        (2, lambda p: p["goals"][0].update(depends_on=["goal-3"]), "goal-1"),
        (3, lambda p: p["goals"][0]["agents"][0].update(depends_on=["agent-2"]), "agent-1"),
        (4, lambda p: p["goals"][0].update(acceptance=["结果质量良好"]), "goal-1.acceptance"),
        (5, lambda p: p["goals"][0]["agents"][0]["output"].update(validators=[]), "agent-1.output.validators"),
        (6, lambda p: p["goals"][0]["agents"][0]["capability"]["fs"].update(write=["../secret"]), "agent-1.capability.fs.write"),
        (7, lambda p: p["goals"][0]["agents"][0]["capability"].update(shell="workspace"), "agent-1.engine"),
        (8, lambda p: p["goals"][0]["agents"][0]["capability"].update(network="open"), "agent-1.capability.justification"),
        (9, lambda p: p["goals"][0]["agents"][0]["prompt"].update(body="请告诉我关键词后再继续"), "agent-1.prompt.body"),
        (10, lambda p: p.update(planned_steps=8), "plan.planned_steps"),
        (11, lambda p: p["goals"][0]["intervention"].update(on_complete=False), "goal-1.intervention.on_complete"),
        (12, lambda p: p["decision_balance"][0].update(answer=None), "q-1.answer"),
        (13, lambda p: p["goals"][0]["agents"][0]["output"].update(validators=["not_registered"]), "agent-1.output.validators[0]"),
    ],
)
def test_十三条_error_逐条命中且带定位(rule, mutate, location) -> None:
    plan = make_plan_dict()
    mutate(plan)

    matches = [message for message in _messages(plan) if message.startswith(f"[规则{rule}]")]
    assert matches, f"规则 {rule} 未命中：{_messages(plan)}"
    assert location in "；".join(matches)


def test_合格子集计划_lint_零_error() -> None:
    from app.plan.lint import lint

    assert lint(make_plan_dict())["errors"] == []


def test_goal_依赖成环报出环上的_id_列表() -> None:
    plan = make_plan_dict()
    plan["goals"][0]["depends_on"] = ["goal-3"]

    message = "；".join(_messages(plan))
    assert "[规则2]" in message
    assert "goal-1" in message and "goal-2" in message and "goal-3" in message


def test_agent_跨_goal_依赖与_inputs_未声明上游均报规则三() -> None:
    plan = make_plan_dict()
    agent = plan["goals"][1]["agents"][0]
    agent["depends_on"] = ["agent-1"]
    agent["inputs"] = [{"from_goal": "goal-3", "artifact": "result.md"}]

    messages = [item for item in _messages(plan) if item.startswith("[规则3]")]
    assert len(messages) == 2
    assert all("goal-2/agent-2" in item for item in messages)


@pytest.mark.parametrize(
    "validator",
    ["not_registered", "file_exists:extra", "table_rows_between:1", "sections_exist:章节, 章节二"],
)
def test_校验器表外名字及参数语法参数个数均为_error(validator: str) -> None:
    plan = make_plan_dict()
    plan["goals"][0]["agents"][0]["output"]["validators"] = [validator]
    assert any(item.startswith("[规则13]") for item in _messages(plan))


def test_六类_warning_均覆盖() -> None:
    from app.plan.lint import lint

    plan = make_plan_dict()
    plan["goals"] = plan["goals"][:1]
    goal = plan["goals"][0]
    goal["title"] = "搜索阅读总结"
    goal["deliverable"]["description"] = ""
    agent = goal["agents"][0]
    agent["prompt"]["body"] = "整理资料并输出。"
    agent["output"]["validators"] = ["section_exists:结论"]
    agent["capability"]["sources"] = ["hacker_news"]
    agent["capability"]["tools"] = ["source.hacker_news", "fs.read"]
    goal["agents"].extend(
        make_agent(f"agent-extra-{number}", "goal-1") for number in range(1, 6)
    )
    for duplicate in goal["agents"][1:3]:
        duplicate["capability"]["sources"] = ["hacker_news"]
        duplicate["capability"]["tools"] = ["source.hacker_news", "fs.read"]

    warnings = lint(plan)["warnings"]
    assert len(warnings) >= 6
    for number in range(1, 7):
        assert any(item.startswith(f"[警告{number}]") for item in warnings)
