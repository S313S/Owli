"""M4-a 报告产物契约收口：信封验证器、规则 27、空判定、归并与幽灵行。"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.plan_factory import make_plan_dict
from tests.test_m3h_ledger import _store


# ---------- 契约常量一致性 ----------

def test_节化职能闭集与运行期同口径():
    from app.orchestrator.sectioning import SECTIONED_KINDS
    from app.plan.model import SECTIONED_CHAPTER_KINDS

    assert SECTIONED_CHAPTER_KINDS == frozenset(SECTIONED_KINDS)


def test_agent_kind_of_与生成器agent_id同口径():
    from app.plan.model import agent_kind_of

    assert agent_kind_of("cross-validation") == "cross_validation"
    assert agent_kind_of("cross-validation-2") == "cross_validation"
    assert agent_kind_of("report-writing") == "report_writing"
    assert agent_kind_of("summary-3") == "summary"
    assert agent_kind_of("tagging", "report-writer") == "tagging"
    assert agent_kind_of("my-report", "report-writer") == "report_writing"
    assert agent_kind_of("unknown-id", None) == "audit"


# ---------- sectioned_document_valid ----------

def _envelope(**overrides):
    document = {
        "title": "豆包语音输入法的竞品分析",
        "chapter_id": "ch-3",
        "sections": [
            {
                "section_id": "ch-3/sec-1",
                "goal_id": "goal-1",
                "title": "竞品基准信息采集",
                "markdown": "## 结论\n\n- 判断一 [S01]",
            },
        ],
        "缺失清单": [
            {"goal_id": "goal-2", "chapter_id": "ch-2", "reason": "tool_unavailable",
             "text": "此处缺失：goal-2/ch-2；原因：tool_unavailable"},
        ],
    }
    document.update(overrides)
    return document


def _validate_envelope(tmp_path: Path, payload) -> "object":
    from app.adapters import validation

    artifact = tmp_path / "runs" / "r-m4a" / "goals" / "goal-3" / "chapter.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    text = payload if isinstance(payload, str) else json.dumps(
        payload, ensure_ascii=False
    )
    artifact.write_text(text, encoding="utf-8")
    ctx = validation.Ctx(
        output_path=artifact,
        output_format="json",
        research_id="r-m4a",
        goal_id="goal-3",
        agent_id="cross-validation",
        read_text=lambda: artifact.read_text(encoding="utf-8"),
        read_json=lambda: json.loads(artifact.read_text(encoding="utf-8")),
        store=None,
        source_domains=frozenset(),
        runs_root=tmp_path / "runs",
    )
    return validation.validate(ctx, ["file_exists", "sectioned_document_valid"])


def test_合法信封PASS(tmp_path):
    from app.adapters import validation

    report = _validate_envelope(tmp_path, _envelope())
    assert report.verdict is validation.Verdict.PASS


def test_顶层数组FAIL并报形状(tmp_path):
    from app.adapters import validation

    report = _validate_envelope(tmp_path, [{"score_authority": 2}])
    assert report.verdict is validation.Verdict.FAIL
    assert "信封" in report.failures[0].message


def test_缺键空节与闭集外reason逐条列出(tmp_path):
    from app.adapters import validation

    bad = _envelope(
        chapter_id="",
        sections=[
            {"section_id": "ch-3/sec-1", "goal_id": "goal-1", "title": "t",
             "markdown": ""},
            "不是对象",
        ],
    )
    bad["缺失清单"] = [{"goal_id": "goal-2", "chapter_id": "ch-2", "reason": "自由文本"}]
    report = _validate_envelope(tmp_path, bad)
    assert report.verdict is validation.Verdict.FAIL
    offenders = "\n".join(report.failures[0].offenders)
    assert "chapter_id" in offenders
    assert "sections[0].markdown" in offenders
    assert "sections[1]" in offenders
    assert "缺失清单[0].reason" in offenders


def test_sections为空FAIL(tmp_path):
    from app.adapters import validation

    report = _validate_envelope(tmp_path, _envelope(sections=[]))
    assert report.verdict is validation.Verdict.FAIL
    assert any("sections：为空" in item for item in report.failures[0].offenders)


def test_非法JSON是FAIL不是UNAVAILABLE(tmp_path):
    from app.adapters import validation

    report = _validate_envelope(tmp_path, "{断掉的 json")
    assert report.verdict is validation.Verdict.FAIL


def test_带参数UNAVAILABLE(tmp_path):
    from app.adapters import validation

    artifact = tmp_path / "runs" / "r-m4a" / "goals" / "goal-3" / "chapter.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(json.dumps(_envelope()), encoding="utf-8")
    ctx = validation.Ctx(
        output_path=artifact, output_format="json", research_id="r-m4a",
        goal_id="goal-3", agent_id="cross-validation",
        read_text=lambda: artifact.read_text(encoding="utf-8"),
        read_json=lambda: json.loads(artifact.read_text(encoding="utf-8")),
        store=None, source_domains=frozenset(), runs_root=tmp_path / "runs",
    )
    report = validation.validate(ctx, ["sectioned_document_valid:1"])
    assert report.verdict is validation.Verdict.UNAVAILABLE


# ---------- lint 规则 27 ----------

def _plan_with_cross_validation(validators: list[str], shape: str = "object") -> dict:
    source = make_plan_dict()
    agent = source["goals"][1]["agents"][0]
    agent["agent_id"] = "cross-validation"
    agent["display_name"] = "交叉验证"
    agent["output"] = {
        "format": "json",
        "shape": shape,
        "path": "goals/goal-2/cross-validation.json",
        "validators": validators,
    }
    return source


def _rule27_errors(source: dict) -> list[str]:
    from app.plan.lint import lint

    return [item for item in lint(source)["errors"] if item.startswith("[规则27]")]


def test_规则27_数组验证器进节化章被拒():
    errors = _rule27_errors(_plan_with_cross_validation(
        ["file_exists", "no_item_missing_rating", "json_array_min_items:1"],
    ))
    assert len(errors) == 1
    assert "json_array_min_items" in errors[0]
    assert "no_item_missing_rating" in errors[0]


def test_规则27_节化章shape必须object():
    errors = _rule27_errors(_plan_with_cross_validation(
        ["file_exists", "sectioned_document_valid"], shape="array",
    ))
    assert len(errors) == 1
    assert "shape 必须为 object" in errors[0]


def test_规则27_信封声明合法零报错():
    assert _rule27_errors(_plan_with_cross_validation(
        ["file_exists", "sectioned_document_valid"],
    )) == []


def test_规则27_非节化审计章不受影响():
    source = make_plan_dict()
    agent = source["goals"][1]["agents"][0]
    agent["agent_id"] = "reliability-audit"
    agent["display_name"] = "可靠度审计"
    agent["output"] = {
        "format": "json", "shape": "array",
        "path": "goals/goal-2/reliability-audit.json",
        "validators": ["file_exists", "no_item_missing_rating"],
    }
    assert _rule27_errors(source) == []


# ---------- generate._output 契约 ----------

def test_output_交叉验证章换信封验证器组():
    from app.plan.generate import _output

    output = _output("cross_validation", "goal-2", "cross-validation", "object")
    assert output["format"] == "json"
    assert output["validators"] == ["file_exists", "sectioned_document_valid"]


def test_output_审计章保留评级验证器组():
    from app.plan.generate import _output

    output = _output("reliability_audit", "goal-2", "reliability-audit", "array")
    assert "no_item_missing_rating" in output["validators"]
    assert "sectioned_document_valid" not in output["validators"]


def test_output_报告json交付物不再塌成裸file_exists():
    from app.plan.generate import _output

    target = {"format": "json", "shape": "object",
              "path": "goals/goal-3/report.json", "description": "d"}
    output = _output("report_writing", "goal-3", "report-writing", "object", target)
    assert output["format"] == "json"
    assert output["validators"] == ["file_exists", "sectioned_document_valid"]


def test_节化章提示词按信封形描述():
    from app.plan.generate import _agent_prompt, _output

    output = _output("cross_validation", "goal-2", "cross-validation", "object")
    body = _agent_prompt("豆包", "交叉验证", output, "cross_validation")
    assert "节化文档信封" in body
    assert "顶层必须是 JSON 数组" not in body


# ---------- _artifact_is_empty（裁决点 6）----------

@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({}, True),
        ({"sections": []}, True),
        ({"sections": [{"markdown": "内容"}], "缺失清单": []}, False),
        ({"items": []}, False),  # 无必备键的普通 object 维持无键即空口径
    ],
)
def test_object空判定升级为必备键sections为空即空(tmp_path, payload, expected):
    from app.orchestrator.runtime import RuntimeCoordinator

    artifact = tmp_path / "artifact.json"
    artifact.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    task = SimpleNamespace(output_path=artifact, output_format="json")
    assert RuntimeCoordinator._artifact_is_empty(None, task) is expected


# ---------- merge_sectioned_markdown（改法 3）----------

def test_归并后整卷单结论单信息源并全卷重排角标():
    from app.report.markdown import merge_sectioned_markdown

    sec1 = (
        "## goal-1｜采集\n\n判断甲 [S01]。\n\n## 结论\n\n- 甲成立 [S01]\n\n"
        "## 信息源\n\n- [S01] [来源A](https://example.com/a)（fetched_at=t1）\n"
    )
    sec2 = (
        "## goal-2｜对标\n\n判断乙 [S01] 与丙 [S02]。\n\n## 结论\n\n"
        "- 乙成立 [S01]\n- 丙成立 [S02]\n\n## 信息源\n\n"
        "- [S01] [来源B](https://example.com/b)（fetched_at=t2）\n"
        "- [S02] [来源A](https://example.com/a)（fetched_at=t1）\n\n"
        "## 缺失清单\n\n- 节内自写的缺失清单应被剥除\n"
    )
    document = merge_sectioned_markdown("整卷标题", [sec1, sec2], [])

    assert document.count("## 结论") == 1
    assert document.count("## 信息源") == 1
    assert document.count("## 缺失清单") == 1
    assert "节内自写的缺失清单应被剥除" not in document
    # 同 URL 去重：来源A 只登记一次；来源B 编成 [S02]
    assert document.count("https://example.com/a") == 1
    assert "- [S02] [来源B](https://example.com/b)" in document
    # sec-2 正文与结论里的角标随全卷重排：其 S01→S02（来源B）、S02→S01（来源A）
    assert "判断乙 [S02] 与丙 [S01]。" in document
    assert "- 乙成立 [S02]" in document
    assert "- 丙成立 [S01]" in document
    # 正文角标集合与信息源清单双向一致
    assert "- 甲成立 [S01]" in document


def test_归并保留占位节与缺失清单并容忍无角标信息源():
    from app.report.markdown import merge_sectioned_markdown

    placeholder = (
        "## goal-2｜对标\n\n- 此处缺失：goal-2/ch-1/sec-2；原因：timeout\n"
    )
    sec1 = "## 结论\n\n已有节。\n\n## 信息源\n\n- 来源 A。\n"
    document = merge_sectioned_markdown(
        "标题", [sec1, placeholder],
        ["此处缺失：goal-2/ch-1/sec-2；原因：timeout"],
    )
    assert "已有节。" in document
    assert "- 来源 A。" in document
    assert "## 信息源" in document
    assert document.count("此处缺失：goal-2/ch-1/sec-2") == 2  # 原位占位 + 缺失清单
    assert document.rstrip().splitlines()[-1] == "- 此处缺失：goal-2/ch-1/sec-2；原因：timeout"


# ---------- running 幽灵行（改法 4）----------

def test_节化执行中被取消_在跑节复位不留running(tmp_path):
    from app.orchestrator.runtime import RuntimeCoordinator
    from app.plan.model import Plan
    from datetime import datetime, timezone

    store = _store(tmp_path)
    source = make_plan_dict()
    source["research_id"] = "r-ledger"
    source["scale"] = "standard"
    source["baseline"] = None
    source["goals"] = source["goals"][:1]
    agent = source["goals"][0]["agents"][0]
    agent["agent_id"] = "report-writing"
    agent["display_name"] = "报告撰写"
    agent["output"] = {
        "format": "markdown",
        "path": "goals/goal-1/report.md",
        "validators": ["file_exists"],
    }
    agent["chapter"] = {
        "chapter_id": "ch-1",
        "chapter_type": "report",
        "plan_path": "goals/goal-1/ch-1.md",
        "opening": {"inputs": [], "task": agent["task"], "acceptance": ["完成"]},
        "closing": {
            "output": {"path": agent["output"]["path"]},
            "entities": ["豆包"],
            "expected_count": None,
            "notes": {},
        },
    }
    plan = Plan.from_dict(source)
    dispatched = asyncio.Event()

    class HangingAdapter:
        async def run(self, task, ctx, on_event=None):
            dispatched.set()
            await asyncio.Event().wait()

    coordinator = RuntimeCoordinator(
        store=store,
        event_buffer=SimpleNamespace(publish=_noop_publish),
        researches={},
        cards={},
        adapter_factory=lambda: HangingAdapter(),
        runs_root=tmp_path / "runs",
        routing_utc_clock=lambda: datetime(2026, 8, 24, tzinfo=timezone.utc),
    )
    coordinator._adapters["r-ledger"] = HangingAdapter()

    async def scenario():
        run = asyncio.create_task(coordinator._run_task(
            plan,
            plan.goals[0].agents[0],
            SimpleNamespace(
                goal_id="goal-1", attempt=1, engine="claude",
                failure_feedback=None, on_event=lambda event: asyncio.sleep(0),
            ),
        ))
        await asyncio.wait_for(dispatched.wait(), timeout=5)
        rows = {
            row["chapter_id"]: row["status"]
            for row in store.list_chapters("r-ledger")
        }
        assert rows["ch-1/sec-1"] == "running"
        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run

    asyncio.run(scenario())
    statuses = [row["status"] for row in store.list_chapters("r-ledger")]
    assert "running" not in statuses
    rows = {row["chapter_id"]: row for row in store.list_chapters("r-ledger")}
    assert rows["ch-1/sec-1"]["status"] == "pending"


async def _noop_publish(research_id, payload):
    return None
