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


def test_M2真实矛盾_JSON数组校验器与_object_验收冲突报_error() -> None:
    from app.plan.lint import lint

    plan = make_plan_dict()
    goal = plan["goals"][0]
    goal["acceptance"] = [
        "文件存在且为合法 JSON object，顶层含 query_params 与 hits 两个键"
    ]
    goal["agents"][0]["output"]["validators"] = [
        "file_exists", "json_array_min_items:1"
    ]

    errors = lint(plan)["errors"]
    assert any("[规则14]" in item and "agent-1" in item for item in errors)


def test_M2真实矛盾_同_goal_两个_agent_写同一路径报_error() -> None:
    from app.plan.lint import lint

    plan = make_plan_dict()
    goal = plan["goals"][0]
    second = make_agent("reliability-audit", "goal-1")
    second["output"]["path"] = goal["agents"][0]["output"]["path"]
    goal["agents"].append(second)

    errors = lint(plan)["errors"]
    assert any("[规则15]" in item and "reliability-audit" in item for item in errors)


def test_同_goal_语义相同的点路径也视为冲突() -> None:
    from app.plan.lint import lint

    plan = make_plan_dict()
    goal = plan["goals"][0]
    original = goal["agents"][0]["output"]["path"]
    second = make_agent("reliability-audit", "goal-1")
    second["output"]["path"] = original.replace("/", "/./", 1)
    goal["agents"].append(second)
    assert any("[规则15]" in item for item in lint(plan)["errors"])


def test_M3a验收真实矛盾_JSON契约未点名文件遇章节校验器报_error() -> None:
    # r-4878be30ff8c goal-2 实锤：验收要 JSON 契约但没说是哪个文件，
    # 同 goal 的 data-cleaning 却挂 sections_exist:结论 —— agent 写纯 JSON
    # 后章节校验器必失败，重试耗尽 goal 直接 failed。
    from app.plan.lint import lint

    plan = make_plan_dict()
    goal = plan["goals"][0]
    goal["acceptance"] = [
        "文件为合法 JSON，顶层恰含 columns、rows、competitor_set 三个字段"
    ]
    goal["agents"][0]["output"]["validators"] = ["file_exists", "sections_exist:结论"]

    errors = lint(plan)["errors"]
    assert any("[规则17]" in item and "agent-1" in item for item in errors)


def test_JSON契约点名了json文件时章节校验器不冲突() -> None:
    from app.plan.lint import lint

    plan = make_plan_dict()
    goal = plan["goals"][0]
    goal["acceptance"] = [
        "candidates.json 为合法 JSON，顶层恰含 columns、rows、competitor_set 三个字段"
    ]
    goal["agents"][0]["output"]["validators"] = ["file_exists", "sections_exist:结论"]

    assert not any("[规则17]" in item for item in lint(plan)["errors"])


def test_JSON契约点名按goal级判定_同goal其他结构行不再逐行索要文件名() -> None:
    # r-29586a489b34 goal-4 实锤：回喂后模型已在首行点名
    # 「文件 pros-cons.json 存在且顶层为 JSON object」，次行描述同一文件
    # 结构（「顶层 object 含 competitors 字段」）因行内无文件名被逐行
    # 误拒，三次重试全灭。契约有归属即视为已点名。
    from app.plan.lint import lint

    plan = make_plan_dict()
    goal = plan["goals"][0]
    goal["acceptance"] = [
        "文件 pros-cons.json 存在且顶层为 JSON object",
        "顶层 object 含 competitors 字段且其值为数组",
    ]
    goal["agents"][0]["output"]["validators"] = ["file_exists", "sections_exist:结论"]

    assert not any("[规则17]" in item for item in lint(plan)["errors"])


def test_规则14的object契约同样认字段措辞() -> None:
    from app.plan.lint import lint

    plan = make_plan_dict()
    goal = plan["goals"][0]
    goal["acceptance"] = ["文件是 JSON，顶层恰含 query_params 与 hits 两个字段"]
    goal["agents"][0]["output"]["validators"] = ["file_exists", "json_array_min_items:1"]

    assert any("[规则14]" in item for item in lint(plan)["errors"])


def test_M2真实矛盾_验收预设具体实体只报_warning() -> None:
    from app.plan.lint import lint

    plan = make_plan_dict()
    plan["goals"][1]["acceptance"] = [
        "文件为 JSON 数组，长度介于 3 与 10 之间，且必须包含 Feishu/Lark 条目"
    ]

    result = lint(plan)
    assert not any("必须包含 Feishu/Lark 条目" in item for item in result["errors"])
    assert any("[警告7]" in item and "Feishu/Lark" in item for item in result["warnings"])


def test_跨平台原始热度相加在_lint_层被拒() -> None:
    from app.plan.lint import lint

    plan = make_plan_dict()
    plan["goals"][0]["agents"][0]["task"] = "将 HN points 与 PH votes 跨平台相加得到总热度。"

    assert any(item.startswith("[规则16]") for item in lint(plan)["errors"])


def test_不同平台专属原始指标相加即使没写跨平台也被拒() -> None:
    from app.plan.lint import lint

    plan = make_plan_dict()
    plan["goals"][0]["agents"][0]["task"] = "将 HN points 与 PH votes 相加得到总热度。"
    assert any(item.startswith("[规则16]") for item in lint(plan)["errors"])


def test_M3a验收真实矛盾_无采集能力goal按实体写死最小条数报_error() -> None:
    # r-b1b75c7000ab goal-3 实锤：验收要「每个竞品至少列出 2 条来自不同
    # author 的独立证据」，上游契约只保证 distinct competitor ≥3、对每竞品
    # 条数零承诺；3 个竞品各只剩 1 条，禁止新抓取的分析 agent 永不可满足，
    # 重试与换引擎耗尽后整条调研 failed。
    from app.plan.lint import lint

    plan = make_plan_dict()
    plan["goals"][1]["acceptance"] = [
        "报告为每个竞品至少列出 2 条来自不同 author 的独立证据"
    ]

    errors = lint(plan)["errors"]
    assert any("[规则18]" in item and "goal-2" in item for item in errors)


def test_规则18采集goal与首goal豁免() -> None:
    from app.plan.lint import lint

    plan = make_plan_dict()
    # 首 goal（无 depends_on）豁免：数据量由采集行为决定。
    plan["goals"][0]["acceptance"] = ["每个竞品至少列出 2 条独立证据"]
    # 下游 goal 但 agent 具备采集能力（sources 非空）同样豁免。
    plan["goals"][2]["acceptance"] = ["每个竞品至少列出 2 条独立证据"]
    plan["goals"][2]["agents"][0]["capability"]["sources"] = ["hacker_news"]
    plan["goals"][2]["agents"][0]["capability"]["network"] = "sources_only"

    assert not any("[规则18]" in item for item in lint(plan)["errors"])


def test_规则18条件式措辞不报错() -> None:
    from app.plan.lint import lint

    plan = make_plan_dict()
    plan["goals"][1]["acceptance"] = [
        "每个竞品列出来自不同 author 的独立证据，上游不足 2 条时明确标注孤证"
    ]

    assert not any("[规则18]" in item for item in lint(plan)["errors"])


def test_规则18条件式不认字面单选_不足以支撑与即算达标同样豁免() -> None:
    # 真实样本 r-f14050856779：三轮回灌后模型写出的条件式因措辞不含
    # 「不足时」被拒到重试耗尽。
    from app.plan.lint import lint

    plan = make_plan_dict()
    plan["goals"][1]["acceptance"] = [
        "每条被标为 高 可靠度的断言均引用不少于 2 个来自上游 evidence 的"
        " permalink，若上游数据不足以支撑则在该断言上标注 reliability_level="
        "孤证 或在伴随 meta 的 gaps 中记录该缺口即算达标"
    ]

    assert not any("[规则18]" in item for item in lint(plan)["errors"])


def test_跨_goal_deliverable_与_agent_output_路径冲突报_error() -> None:
    from app.plan.lint import lint

    plan = make_plan_dict()
    plan["goals"][1]["deliverable"]["path"] = plan["goals"][0]["agents"][0]["output"]["path"]

    errors = lint(plan)["errors"]
    assert any(
        item.startswith("[规则19]")
        and "goal-1" in item
        and "goal-2" in item
        for item in errors
    )


def test_同_goal_最终_agent_与_deliverable_同路径是唯一合法重复() -> None:
    from app.plan.lint import lint

    plan = make_plan_dict()
    goal = plan["goals"][0]
    goal["agents"][0]["output"]["path"] = goal["deliverable"]["path"]

    assert not any("[规则19]" in item for item in lint(plan)["errors"])


def test_report_writer_必须同时具备双向角标校验器() -> None:
    from app.plan.lint import lint

    plan = make_plan_dict()
    agent = plan["goals"][2]["agents"][0]
    agent["capability"]["profile"] = "report-writer"
    agent["output"]["validators"] = [
        "file_exists",
        "citation_marks_resolvable",
    ]

    errors = lint(plan)["errors"]
    assert any(
        item.startswith("[规则20]")
        and "no_orphan_citation" in item
        for item in errors
    )
