from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

from tests.plan_factory import make_plan_dict
from tests.test_m3h_ledger import _store


def test_standard_报告章按节短调用_失败详情进SSE与账本(tmp_path):
    from app.adapters import validation
    from app.adapters.contracts import EngineRunResult, OwliResult
    from app.orchestrator.runtime import RuntimeCoordinator
    from app.plan.model import Plan

    store = _store(tmp_path)
    for index, goal_id in enumerate(("goal-1", "goal-2"), start=1):
        store.add_evidence(
            id=f"ev-{index}",
            report_id="r-ledger",
            goal_id=goal_id,
            platform="web_search",
            permalink=f"https://example.com/{goal_id}",
            fetched_at="2026-08-22T00:00:00Z",
            title=f"来源 {goal_id}",
            content_excerpt="可复核正文",
        )
    source = make_plan_dict()
    source["research_id"] = "r-ledger"
    source["scale"] = "standard"
    source["baseline"] = None
    source["goals"] = source["goals"][:2]
    # 节级传输重试次数是独立常量 SECTION_RETRY_MAX_ATTEMPTS（D-008 期望 c），
    # 与 max_attempts_per_round 无关；本用例只看「耗尽后落账 + 进 SSE」的细节。
    source["goals"][1]["retry_policy"]["max_attempts_per_round"] = 1
    report_agent = source["goals"][1]["agents"][0]
    report_agent["agent_id"] = "report-writing"
    report_agent["display_name"] = "报告撰写"
    report_agent["output"] = {
        "format": "markdown",
        "path": "goals/goal-2/report.md",
        "validators": ["file_exists", "sections_exist:结论,信息源"],
    }
    report_agent["chapter"] = {
        "chapter_id": "ch-1",
        "chapter_type": "report",
        "plan_path": "goals/goal-2/ch-1.md",
        "opening": {"inputs": [], "task": report_agent["task"], "acceptance": ["完成"]},
        "closing": {
            "output": {"path": report_agent["output"]["path"]},
            "entities": ["豆包"],
            "expected_count": None,
            "notes": {},
        },
    }
    plan = Plan.from_dict(source)
    done_section = tmp_path / "runs" / "r-ledger" / "goals" / "goal-2" / "report" / "sec-1.md"
    done_section.parent.mkdir(parents=True)
    done_section.write_text(
        "## 结论\n\n- 已有节 [S01]。\n\n"
        "## 信息源\n\n- [S01] [来源 A](https://example.com/goal-1)\n",
        encoding="utf-8",
    )
    store.ensure_chapters(
        "r-ledger",
        [
            {"goal_id": "goal-2", "chapter_id": "ch-1/sec-1"},
            {"goal_id": "goal-2", "chapter_id": "ch-1/sec-2"},
        ],
        updated_at="2026-08-22T00:00:00Z",
    )
    store.finish_chapter(
        "r-ledger", "goal-2", "ch-1/sec-1",
        status="done", reason=None,
        actual_output_path=str(done_section),
        actual_count=1,
        updated_at="2026-08-22T00:00:01Z",
    )
    calls = []

    emitted = []
    empty_sections = set()

    class Adapter:
        async def run(self, task, ctx, on_event=None):
            del on_event
            calls.append((task.agent_id, task.body, task.output_path.name))
            if task.output_path.name == "sec-2.md":
                return EngineRunResult(
                    conclusion=None,
                    conclusion_error="socket closed",
                    validation=validation.ValidationReport(
                        validation.Verdict.FAIL,
                        [validation.Result(validation.Verdict.FAIL, "file_exists", "missing", [])],
                    ),
                    events=[],
                    permission_denials=[],
                    engine_error="socket closed by peer",
                )
            task.output_path.parent.mkdir(parents=True, exist_ok=True)
            task.output_path.write_text(
                (
                    "\n"
                    if task.output_path.name in empty_sections
                    else (
                        f"## 结论\n\n- 正文 [S{int(task.output_path.stem[-1]):02d}]\n\n"
                        f"## 信息源\n\n- [S{int(task.output_path.stem[-1]):02d}] "
                        f"[来源](https://example.com/goal-{task.output_path.stem[-1]})\n"
                    )
                ),
                encoding="utf-8",
            )
            return EngineRunResult(
                conclusion=OwliResult(
                    "done", str(task.output_path), "完成", [], [], [], None,
                ),
                conclusion_error=None,
                validation=validation.validate(ctx, task.validators),
                events=[],
                permission_denials=[],
            )

    class Events:
        async def publish(self, research_id, payload):
            emitted.append((research_id, payload))

    coordinator = RuntimeCoordinator(
        store=store,
        event_buffer=Events(),
        researches={},
        cards={},
        adapter_factory=lambda: Adapter(),
        runs_root=tmp_path / "runs",
        routing_utc_clock=lambda: datetime(2026, 8, 22, tzinfo=timezone.utc),
    )
    coordinator._adapters["r-ledger"] = Adapter()
    # 节级退避沿用章级口径（standard=15 s）；本用例只看次数与落账，用假 timer 立即放行
    coordinator.timer = lambda delay, callback: callback()

    result = asyncio.run(coordinator._run_task(
        plan,
        plan.goals[1].agents[0],
        SimpleNamespace(
            goal_id="goal-2",
            attempt=1,
            engine="claude",
            failure_feedback=None,
            on_event=lambda event: asyncio.sleep(0),
        ),
    ))

    output = tmp_path / "runs" / "r-ledger" / "goals" / "goal-2" / "report.md"
    text = output.read_text(encoding="utf-8")
    assert result.succeeded is True
    assert result.actual_count == 1
    from app.orchestrator.sectioning import SECTION_RETRY_MAX_ATTEMPTS

    assert [item[2] for item in calls] == ["sec-2.md"] * SECTION_RETRY_MAX_ATTEMPTS
    assert "本节须包含一个『结论』小节与一个『信息源』小节（标题逐字使用）" in calls[0][1]
    assert "本节的结论/信息源只覆盖本节范围" in calls[0][1]
    assert "已有节" in text
    assert "此处缺失：goal-2/ch-1/sec-2" in text
    assert "## 缺失清单" in text
    rows = {row["chapter_id"]: row for row in store.list_chapters("r-ledger")}
    assert rows["ch-1/sec-1"]["attempts"] == 0
    assert rows["ch-1/sec-2"]["status"] == "missing"
    assert rows["ch-1/sec-2"]["reason"] == "retry_exhausted"
    assert rows["ch-1/sec-2"]["engine_error"] == "socket closed by peer"
    assert rows["ch-1/sec-2"]["conclusion_error"] == "socket closed"
    error_event = next(payload for _, payload in emitted if payload["type"] == "section_error")
    assert error_event["data"]["reason"] == "retry_exhausted"
    assert error_event["data"]["engine_error"] == "socket closed by peer"
    assert error_event["data"]["conclusion_error"] == "socket closed"

    plan.goals[1].agents[0].output["validators"].append("sections_exist:不存在章节")
    invalid = asyncio.run(coordinator._run_task(
        plan,
        plan.goals[1].agents[0],
        SimpleNamespace(
            goal_id="goal-2", attempt=2, engine="claude",
            failure_feedback=None, on_event=lambda event: asyncio.sleep(0),
        ),
    ))
    assert invalid.succeeded is False
    assert "sections_exist" in str(invalid.failure_feedback)
    assert {
        row["chapter_id"]: row["status"]
        for row in store.list_chapters("r-ledger")
    }["ch-1/sec-1"] == "pending"

    plan.goals[1].agents[0].output["validators"].remove(
        "sections_exist:不存在章节"
    )
    recovered = asyncio.run(coordinator._run_task(
        plan,
        plan.goals[1].agents[0],
        SimpleNamespace(
            goal_id="goal-2", attempt=3, engine="claude",
            failure_feedback=invalid.failure_feedback,
            on_event=lambda event: asyncio.sleep(0),
        ),
    ))
    assert recovered.succeeded is True
    # D-007：传输耗尽的 sec-2 每一次章级尝试都会被复位重派，不再永久跳过。
    assert [item[2] for item in calls] == (
        ["sec-2.md"] * SECTION_RETRY_MAX_ATTEMPTS * 2
        + ["sec-1.md"]
        + ["sec-2.md"] * SECTION_RETRY_MAX_ATTEMPTS
    )

    store.reset_done_chapters(
        "r-ledger", "goal-2", ["ch-1/sec-1"],
        updated_at="2026-08-22T00:00:03Z",
    )
    empty_sections.add("sec-1.md")
    asyncio.run(coordinator._run_task(
        plan,
        plan.goals[1].agents[0],
        SimpleNamespace(
            goal_id="goal-2", attempt=4, engine="claude",
            failure_feedback=None, on_event=lambda event: asyncio.sleep(0),
        ),
    ))
    empty_row = {
        row["chapter_id"]: row for row in store.list_chapters("r-ledger")
    }["ch-1/sec-1"]
    assert empty_row["status"] == "missing"
    assert empty_row["reason"] == "empty_result"


def _run_章级声明输入_case(tmp_path, *, upstream_status, include_upstream_section):
    from app.adapters import validation
    from app.adapters.capability import Capability, FileSystemScope
    from app.adapters.contracts import EngineRunResult, EngineTask, OwliResult
    from app.orchestrator.sectioning import run_sectioned_task

    store = _store(tmp_path)
    runs_root = tmp_path / "runs"
    upstream_path = runs_root / "r-ledger" / "goals/goal-2/matrix.json"
    upstream_path.parent.mkdir(parents=True, exist_ok=True)
    upstream_path.write_text('{"matrix": {}}', encoding="utf-8")
    store.ensure_chapters(
        "r-ledger",
        [{"goal_id": "goal-2", "chapter_id": "ch-1"}],
        updated_at="2026-08-22T00:00:00Z",
    )
    store.start_chapter(
        "r-ledger", "goal-2", "ch-1",
        engine="claude", updated_at="2026-08-22T00:00:01Z",
    )
    store.finish_chapter(
        "r-ledger", "goal-2", "ch-1",
        status=upstream_status,
        reason=None if upstream_status == "done" else "tool_unavailable",
        actual_output_path=str(upstream_path),
        actual_count=3 if upstream_status == "done" else 0,
        updated_at="2026-08-22T00:00:02Z",
    )
    goals = []
    if include_upstream_section:
        goals.append(SimpleNamespace(goal_id="goal-2", title="六维矩阵"))
    goals.append(SimpleNamespace(goal_id="goal-3", title="竞品分析报告"))
    plan = SimpleNamespace(
        research_id="r-ledger",
        title="报告",
        goals=goals,
    )
    agent = SimpleNamespace(chapter={
        "chapter_id": "ch-report",
        "opening": {"inputs": [{"path": "goals/goal-2/matrix.json"}]},
    })
    context = SimpleNamespace(goal_id="goal-3", engine="claude")
    task = EngineTask(
        body="写报告",
        output_path=runs_root / "r-ledger/goals/goal-3/report.md",
        output_format="markdown",
        research_id="r-ledger",
        goal_id="goal-3",
        agent_id="report-writing",
        agent_kind="report",
        validators=["file_exists"],
        capability=Capability(
            tools=("fs.write",),
            fs=FileSystemScope(write=("goals/goal-3/**",)),
        ),
    )
    bodies = {}

    class Adapter:
        async def run(self, section_task, ctx, on_event=None):
            del on_event
            bodies[section_task.output_path.name] = section_task.body
            section_task.output_path.parent.mkdir(parents=True, exist_ok=True)
            section_task.output_path.write_text(
                "## 结论\n\n完成。\n\n## 信息源\n\n- 来源。\n",
                encoding="utf-8",
            )
            return EngineRunResult(
                conclusion=OwliResult(
                    "done", str(section_task.output_path), "完成", [], [], [], None,
                ),
                conclusion_error=None,
                validation=validation.validate(ctx, section_task.validators),
                events=[],
                permission_denials=[],
            )

    result = asyncio.run(run_sectioned_task(
        plan=plan,
        agent=agent,
        context=context,
        base_task=task,
        adapter=Adapter(),
        store=store,
        runs_root=runs_root,
        now_iso=lambda: "2026-08-22T00:00:03Z",
        on_event=lambda event: asyncio.sleep(0),
    ))
    return result, bodies, upstream_path


def _节输入_from_body(body):
    raw = body.split("本节上游输入 JSON：\n", 1)[1]
    return json.JSONDecoder().raw_decode(raw)[0]


def test_章级声明且账本done的上游路径注入节输入并按路径去重(tmp_path):
    result, bodies, upstream_path = _run_章级声明输入_case(
        tmp_path, upstream_status="done", include_upstream_section=True,
    )

    expected = [{
        "goal_id": "goal-2",
        "chapter_id": "ch-1",
        "path": str(upstream_path),
        "actual_count": 3,
    }]
    assert result.succeeded is True
    assert _节输入_from_body(bodies["sec-1.md"])["done"] == expected
    assert _节输入_from_body(bodies["sec-2.md"])["done"] == expected
    assert "章级 opening.inputs 声明且账本 status=done 的上游产物" in bodies["sec-2.md"]
    assert "产物 path 只用于定位，不是 permalink" in bodies["sec-2.md"]
    assert "不得把本地路径改写成 file:// 角标" in bodies["sec-2.md"]
    assert "done 产物只作事实与上下文来源，不作引用来源" in bodies["sec-2.md"]
    assert "本节可引用证据池 JSON（唯一引用源）" in bodies["sec-2.md"]
    assert "节输入只含该 goal 下 done 章" not in bodies["sec-2.md"]


def test_章级声明但账本不是done的上游路径不进节输入(tmp_path):
    result, bodies, upstream_path = _run_章级声明输入_case(
        tmp_path, upstream_status="missing", include_upstream_section=False,
    )

    inputs = _节输入_from_body(bodies["sec-1.md"])
    assert result.succeeded is True
    assert inputs == {"done": [], "missing": []}
    assert str(upstream_path) not in bodies["sec-1.md"]
    assert "只允许读取下方 done 列出的产物" in bodies["sec-1.md"]


def test_节失败原因按真实_cause_映射():
    from app.orchestrator.sectioning import section_failure_reason

    assert section_failure_reason(SimpleNamespace(
        engine_error=None,
        conclusion_error="owli-result 未找到",
        conclusion=None,
        permission_denials=[],
    )) == "empty_result"
    assert section_failure_reason(SimpleNamespace(
        engine_error=None,
        conclusion_error=None,
        conclusion=SimpleNamespace(reason="tool_unavailable", output_path=None),
        permission_denials=["工具 WebSearch 被拒绝"],
    )) == "tool_unavailable"
    assert section_failure_reason(SimpleNamespace(
        engine_error="Stream idle timeout",
        conclusion_error=None,
        conclusion=None,
        permission_denials=[],
    )) == "timeout"


def test_失败节非空正文改名保全并把路径写入错误字段(tmp_path):
    from app.adapters import validation
    from app.adapters.capability import Capability, FileSystemScope
    from app.adapters.contracts import EngineRunResult, EngineTask
    from app.orchestrator.sectioning import run_sectioned_task

    store = _store(tmp_path)
    output_path = (
        tmp_path / "runs" / "r-ledger" / "goals" / "goal-2" / "report.md"
    )
    plan = SimpleNamespace(
        research_id="r-ledger",
        title="报告",
        goals=[SimpleNamespace(goal_id="goal-1", title="采集")],
    )
    agent = SimpleNamespace(chapter={"chapter_id": "ch-1"})
    context = SimpleNamespace(goal_id="goal-2", engine="claude")
    task = EngineTask(
        body="写报告",
        output_path=output_path,
        output_format="markdown",
        research_id="r-ledger",
        goal_id="goal-2",
        agent_id="report-writing",
        agent_kind="report",
        validators=["file_exists"],
        capability=Capability(
            tools=("fs.write",),
            fs=FileSystemScope(write=("goals/goal-2/**",)),
        ),
    )
    original = "## 真实正文\n\n这段正文不能被销毁。\n"
    calls = []
    raw_block = """```json owli-result
{"status":"done","output_path":"/tmp/section.md","summary":"超长原文","assumptions":[],"unmet":[],"capability_denials":[],"reason":null}
```"""

    class Adapter:
        async def run(self, section_task, ctx, on_event=None):
            del ctx, on_event
            calls.append(section_task.body)
            section_task.output_path.parent.mkdir(parents=True, exist_ok=True)
            section_task.output_path.write_text(original, encoding="utf-8")
            return EngineRunResult(
                conclusion=None,
                conclusion_error="owli-result.summary 必须是 200 字以内字符串",
                validation=validation.ValidationReport(validation.Verdict.PASS, []),
                events=[SimpleNamespace(text=raw_block, is_error=False)],
                permission_denials=[],
            )

    result = asyncio.run(run_sectioned_task(
        plan=plan,
        agent=agent,
        context=context,
        base_task=task,
        adapter=Adapter(),
        store=store,
        runs_root=tmp_path / "runs",
        now_iso=lambda: "2026-08-22T00:00:00Z",
        on_event=lambda event: asyncio.sleep(0),
    ))

    section_path = output_path.parent / "report" / "sec-1.md"
    rejected_path = section_path.with_name("sec-1.rejected.md")
    assert rejected_path.read_text(encoding="utf-8") == original
    assert "此处缺失" in section_path.read_text(encoding="utf-8")
    report_text = output_path.read_text(encoding="utf-8")
    assert "\n# 结论\n" not in report_text
    assert "\n# 信息源\n" not in report_text
    assert result.succeeded is False
    assert result.chapter_status == "missing"
    assert result.reason == "conclusion_invalid"
    assert result.actual_count == 0
    assert output_path.is_file()
    row = store.list_chapters("r-ledger")[0]
    assert len(calls) == 2
    assert "结论块字段不合法：" in calls[1]
    assert "owli-result.summary 必须是 200 字以内字符串" in calls[1]
    assert raw_block in calls[1]
    assert "请只重发 owli-result 块" in calls[1]
    assert "不要重写产物" in calls[1]
    assert row["reason"] == "conclusion_invalid"
    assert str(rejected_path) in row["conclusion_error"]


def test_unattended_收到干预卡自动继续(monkeypatch, tmp_path):
    from app.orchestrator.runtime import RuntimeCoordinator

    monkeypatch.setenv("OWLI_UNATTENDED", "1")
    store = _store(tmp_path)
    published = []

    class Events:
        async def publish(self, research_id, payload):
            published.append((research_id, payload))

    state = {
        "status": "running",
        "goals": [{"id": "goal-1", "status": "awaiting_intervention", "agents": []}],
        "cards": [],
        "progress": {},
    }
    coordinator = RuntimeCoordinator(
        store=store, event_buffer=Events(), researches={"r-ledger": state}, cards={},
        runs_root=tmp_path / "runs",
        routing_utc_clock=lambda: datetime(2026, 8, 22, tzinfo=timezone.utc),
    )
    responses = []

    async def respond(card_id, *, action, payload):
        responses.append((card_id, action, payload))

    coordinator.respond_card = respond
    event = {
        "type": "card_update",
        "data": {"card": {
            "card_id": "intervene-1", "card_type": "INTERVENE",
            "research_id": "r-ledger", "goal_id": "goal-1", "agent_id": None,
            "title": "继续", "body": "是否继续", "target": {},
            "actions": [{"type": "CHOICE_2", "id": "continue", "label": "继续"}],
            "blocking": "goal", "deadline": None, "status": "pending",
            "result": None, "created_at": "2026-08-22T00:00:00Z", "resolved_at": None,
        }},
    }

    async def scenario():
        await coordinator._emit_scheduler_event("r-ledger", event)
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert responses == [(
        "intervene-1", "continue", {"choice": "continue", "auto": True},
    )]
    assert published[-1][1]["type"] == "card_update"
