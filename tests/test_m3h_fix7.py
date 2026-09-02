"""D-008（缺陷 E 第 2 次修复）：章墙钟主动取消、节级重试时间预算、json 章按 shape 组装。"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from tests.test_m3h_fix6 import (
    RATE_LIMIT_ERROR,
    SECTION_BODY,
    TRANSPORT_ERROR,
    _plan,
    _task,
)
from tests.test_m3h_ledger import _store


DENIAL = "Glob 未提供可校验的写入路径"


def _result(*, engine_error=None, denials=(), conclusion_error=None):
    from app.adapters import validation
    from app.adapters.contracts import EngineRunResult

    return EngineRunResult(
        conclusion=None,
        conclusion_error=conclusion_error,
        validation=validation.ValidationReport(validation.Verdict.PASS, []),
        events=[],
        permission_denials=list(denials),
        engine_error=engine_error,
    )


def test_denial与传输断连同现时归传输类不被tool_unavailable掩盖(tmp_path: Path) -> None:
    from app.orchestrator.chapter_failure import chapter_failure_reason

    path = tmp_path / "none.md"
    assert chapter_failure_reason(
        _result(engine_error=TRANSPORT_ERROR, denials=[DENIAL]), path,
    ) == "retry_exhausted"
    # 纯 denial 仍是 tool_unavailable；denial 撞真 429 也仍按限流处理
    assert chapter_failure_reason(_result(denials=[DENIAL]), path) == "tool_unavailable"
    assert chapter_failure_reason(
        _result(engine_error=RATE_LIMIT_ERROR, denials=[DENIAL]), path,
    ) == "tool_unavailable"


def test_denial与超时文案同现时归timeout(tmp_path: Path) -> None:
    from app.orchestrator.chapter_failure import chapter_failure_reason

    reason = chapter_failure_reason(
        _result(
            engine_error="Claude 任务超时（300 秒），已终止并要求整任务重跑",
            denials=[DENIAL],
        ),
        tmp_path / "none.md",
    )
    assert reason == "timeout"


def _hanging_sdk():
    class Client:
        def __init__(self, options) -> None:
            self.options = options

        async def connect(self, prompt) -> None:
            async for _ in prompt:
                pass

        async def receive_response(self):
            await asyncio.Event().wait()
            yield None  # pragma: no cover

        async def disconnect(self) -> None:
            pass

    return SimpleNamespace(
        ClaudeSDKClient=Client,
        ClaudeAgentOptions=lambda **values: SimpleNamespace(**values),
        ResultMessage=type("ResultMessage", (), {}),
        AssistantMessage=type("AssistantMessage", (), {}),
        UserMessage=type("UserMessage", (), {}),
        SystemMessage=type("SystemMessage", (), {}),
        TextBlock=type("TextBlock", (), {}),
        ToolUseBlock=type("ToolUseBlock", (), {}),
        PermissionResultAllow=type("Allow", (), {}),
        PermissionResultDeny=type("Deny", (), {"__init__": lambda self, **v: None}),
        HookMatcher=type(
            "HookMatcher", (), {"__init__": lambda self, matcher=None, hooks=None: None},
        ),
    )


def test_claude单次run有可配引擎超时且超时归timeout(tmp_path: Path) -> None:
    from app.adapters.capability import Capability, FileSystemScope
    from app.adapters.claude import ClaudeAdapter, DEFAULT_CLAUDE_TIMEOUT_SECONDS
    from app.adapters.contracts import EngineTask
    from app.orchestrator.chapter_failure import chapter_failure_reason

    # 默认与 codex 同档 300 s
    assert DEFAULT_CLAUDE_TIMEOUT_SECONDS == 300.0
    output_path = tmp_path / "runs/r/goals/goal-1/hang.md"
    output_path.parent.mkdir(parents=True)
    task = EngineTask(
        body="永不返回", output_path=output_path, output_format="markdown",
        research_id="r", goal_id="goal-1", agent_id="hang", agent_kind="report",
        validators=["file_exists"],
        capability=Capability(
            tools=("fs.write",), fs=FileSystemScope(write=("goals/goal-1/**",)),
        ),
    )
    adapter = ClaudeAdapter(
        sdk=_hanging_sdk(), log_root=tmp_path / "logs", timeout_seconds=0.5,
    )
    result = asyncio.run(adapter.run(task, SimpleNamespace()))
    assert chapter_failure_reason(result, output_path) == "timeout"
    assert "超时" in str(result.engine_error)


def test_selfcheck对claude同样校验墙钟大于引擎超时() -> None:
    import pytest

    from app.adapters.selfcheck import RuntimeConfigCheckError, validate_runtime_config
    from app.config import load_research_scale_config

    report = validate_runtime_config(load_research_scale_config())
    assert report["claude_timeout_seconds"] == 300.0
    with pytest.raises(RuntimeConfigCheckError, match="Claude"):
        validate_runtime_config(
            load_research_scale_config(), codex_timeout_seconds=1.0,
            claude_timeout_seconds=10_000.0,
        )


def _scheduled_wall_clock(
    tmp_path: Path, *, scale: str, deadline: int,
    agent_id: str, profile: str, output_format: str,
) -> tuple[float, float | None]:
    from app.orchestrator.scheduler import Scheduler, TaskRunResult
    from app.plan.model import Plan
    from tests.test_m3h_ledger import make_plan_dict

    tmp_path.mkdir(parents=True)
    source = make_plan_dict()
    source["research_id"] = "r-ledger"
    source["scale"] = scale
    source["baseline"] = None
    first = source["goals"][0]["agents"][0]
    first["agent_id"] = agent_id
    first["capability"]["profile"] = profile
    first["output"]["format"] = output_format
    source["goals"][0]["retry_policy"].update(
        max_attempts_per_round=1, max_rounds=1,
        ask_engine_switch_at=1, chapter_deadline_seconds=deadline,
    )
    plan = Plan.from_dict(source)
    callbacks: list[tuple[float, object]] = []
    contexts = []

    async def run_task(agent, context):
        contexts.append(context)
        return TaskRunResult(True, context.engine, actual_count=1)

    scheduler = Scheduler(
        plan, run_task, lambda event: None,
        lambda: datetime(2026, 8, 27, tzinfo=timezone.utc),
        lambda delay, callback: callbacks.append((delay, callback)),
        chapter_ledger=_store(tmp_path),
    )
    goal = plan.goals[0]
    scheduler.goal_statuses[goal.goal_id] = "running"
    asyncio.run(scheduler._execute_agent(goal, goal.agents[0]))
    return callbacks[0][0], contexts[0].section_deadline_seconds


def test_节化章墙钟按节数放大而非节化与采集章不变(tmp_path: Path) -> None:
    # §D-031：节内还可能再切片，章预算改乘「节数 × 片数上限」。
    # 片墙钟（section_deadline_seconds）一秒没加，仍是章墙钟本身；
    # 放大的是**预算天花板**——池 ≤ 一片装得下时一片都不切，余量原样不花。
    from app.orchestrator.sectioning import WRITE_SHARD_MAX

    assert _scheduled_wall_clock(
        tmp_path / "fast", scale="fast", deadline=330,
        agent_id="report-writing", profile="report-writer", output_format="markdown",
    ) == (330.0 * 3 * WRITE_SHARD_MAX, 330)
    assert _scheduled_wall_clock(
        tmp_path / "standard", scale="standard", deadline=1800,
        agent_id="report-writing", profile="report-writer", output_format="markdown",
    ) == (1800.0 * 3 * WRITE_SHARD_MAX, 1800)
    for agent_id, profile, output_format in (
        ("agent-1", "readonly-analyst", "markdown"),
        ("data-collection", "web-collector", "json"),
    ):
        assert _scheduled_wall_clock(
            tmp_path / agent_id, scale="fast", deadline=330,
            agent_id=agent_id, profile=profile, output_format=output_format,
        ) == (330.0, None)


def test_单节墙钟严格大于引擎单次超时不变量() -> None:
    from app.adapters.selfcheck import validate_runtime_config
    from app.config import load_research_scale_config

    config = load_research_scale_config()
    report = validate_runtime_config(config)
    assert config.fast.chapter_wall_clock_seconds == 330
    assert report["codex_timeout_seconds"] == 300.0
    assert report["claude_timeout_seconds"] == 300.0
    assert 330 > max(
        report["codex_timeout_seconds"], report["claude_timeout_seconds"],
    )


def _deadline_plan(*, deadline_seconds: int, per_round: int = 3, max_rounds: int = 1):
    from app.plan.model import Plan
    from tests.test_m3h_ledger import make_plan_dict

    source = make_plan_dict()
    source["research_id"] = "r-ledger"
    source["scale"] = "fast"
    source["baseline"] = None
    source["goals"] = source["goals"][:1]
    source["goals"][0]["retry_policy"].update(
        max_attempts_per_round=per_round, max_rounds=max_rounds,
        ask_engine_switch_at=per_round, chapter_deadline_seconds=deadline_seconds,
    )
    return Plan.from_dict(source)


def _real_timer(delay, callback):
    loop = asyncio.get_running_loop()
    return loop.call_later(delay, callback)


def test_墙钟到点主动取消在跑任务并落timeout(tmp_path: Path) -> None:
    from app.orchestrator.scheduler import Scheduler

    store = _store(tmp_path)
    plan = _deadline_plan(deadline_seconds=1, per_round=1, max_rounds=1)
    cancelled: list[str] = []

    async def run_task(agent, context):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(agent.agent_id)
            raise
        raise AssertionError("unreachable")  # pragma: no cover

    async def scenario():
        scheduler = Scheduler(
            plan, run_task, lambda event: None,
            lambda: datetime.now(timezone.utc), _real_timer, chapter_ledger=store,
        )
        await asyncio.wait_for(scheduler.start(), timeout=10)
        return scheduler

    scheduler = asyncio.run(scenario())
    row = store.list_chapters("r-ledger")[0]
    assert cancelled, "墙钟到点必须主动取消在跑任务，不能等它自己返回"
    assert row["status"] == "missing"
    assert row["reason"] == "timeout"
    assert scheduler.agent_statuses["agent-1"] == "missing"


def test_假时钟到点取消节化章并把半截JSON节落timeout(tmp_path: Path) -> None:
    from app.adapters.capability import Capability, FileSystemScope
    from app.adapters.contracts import EngineTask
    from app.orchestrator.scheduler import Scheduler
    from app.orchestrator.sectioning import run_sectioned_task

    store = _store(tmp_path)
    plan = _deadline_plan(deadline_seconds=330, per_round=1, max_rounds=1)
    agent = plan.goals[0].agents[0]
    agent.output.update(format="json", shape="object", validators=["file_exists"])
    agent.chapter = {"chapter_id": "ch-sectioned", "opening": {"inputs": []}}
    runs_root = tmp_path / "runs"
    output_path = runs_root / "r-ledger/goals/goal-1/report.json"
    started = asyncio.Event()
    callbacks: list[tuple[float, object]] = []

    class HangingAdapter:
        async def run(self, task, ctx, on_event=None):
            del on_event
            task.output_path.parent.mkdir(parents=True, exist_ok=True)
            task.output_path.write_text('{"markdown":"半截', encoding="utf-8")
            started.set()
            await asyncio.Event().wait()

    async def run_task(current_agent, context):
        task = EngineTask(
            body="写节", output_path=output_path, output_format="json",
            research_id="r-ledger", goal_id="goal-1",
            agent_id=current_agent.agent_id, agent_kind="report",
            validators=["file_exists"],
            capability=Capability(
                tools=("fs.write",),
                fs=FileSystemScope(write=("goals/goal-1/**",)),
            ),
        )
        return await run_sectioned_task(
            plan=plan, agent=current_agent, context=context, base_task=task,
            adapter=HangingAdapter(), store=store, runs_root=runs_root,
            now_iso=lambda: "2026-08-27T00:00:00+00:00",
            on_event=lambda event: None,
        )

    def timer(delay, callback):
        callbacks.append((delay, callback))
        return object()

    async def scenario():
        scheduler = Scheduler(
            plan, run_task, lambda event: None,
            lambda: datetime.now(timezone.utc), timer, chapter_ledger=store,
        )
        driving = asyncio.create_task(scheduler.start())
        await asyncio.wait_for(started.wait(), timeout=5)
        chapter_deadline = next(item for item in callbacks if item[0] == 330)
        chapter_deadline[1]()
        await asyncio.wait_for(driving, timeout=5)
        return scheduler

    scheduler = asyncio.run(scenario())
    rows = {row["chapter_id"]: row for row in store.list_chapters("r-ledger")}
    section = rows["ch-sectioned/sec-1"]
    assert any(delay == 330 for delay, _ in callbacks)
    assert section["status"] == "missing" and section["reason"] == "timeout"
    assert output_path.parent.joinpath("report/sec-1.rejected.md").is_file()
    assert "半截" not in output_path.parent.joinpath("report/sec-1.md").read_text()
    assert scheduler.agent_statuses[agent.agent_id] == "missing"


def test_单节到点只取消当前节并保住同章已完成节(tmp_path: Path) -> None:
    from app.adapters import validation
    from app.adapters.contracts import EngineRunResult, OwliResult
    from app.orchestrator.sectioning import run_sectioned_task

    store = _store(tmp_path)
    runs_root = tmp_path / "runs"
    events: list[dict] = []
    cancelled: list[str] = []

    class SecondSectionHangs:
        async def run(self, task, ctx, on_event=None):
            del on_event
            task.output_path.parent.mkdir(parents=True, exist_ok=True)
            if task.output_path.name == "sec-2.md":
                task.output_path.write_text("半截原文", encoding="utf-8")
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.append(task.output_path.name)
                    raise
            task.output_path.write_text(
                "## 结论\n\n已完成正文。\n\n## 信息源\n\n- 来源 A。\n",
                encoding="utf-8",
            )
            return EngineRunResult(
                conclusion=OwliResult(
                    "done", str(task.output_path), "完成", [], [], [], None,
                ),
                conclusion_error=None,
                validation=validation.validate(ctx, task.validators),
                events=[], permission_denials=[],
            )

    context = SimpleNamespace(
        goal_id="goal-3", engine="claude",
        section_deadline_seconds=0.2,
        cancellation_reason=lambda: None,
    )
    task = _task(runs_root, ["file_exists"])
    result = asyncio.run(run_sectioned_task(
        plan=_plan(1),
        agent=SimpleNamespace(
            chapter={"chapter_id": "ch-1", "opening": {"inputs": []}},
            output={"format": "markdown", "shape": "object"},
        ),
        context=context, base_task=task, adapter=SecondSectionHangs(),
        store=store, runs_root=runs_root,
        now_iso=lambda: "2026-08-27T00:00:00+00:00",
        on_event=lambda event: events.append(event),
        engine_timeout_seconds=0.01,
    ))

    rows = {row["chapter_id"]: row for row in store.list_chapters("r-ledger")}
    assert rows["ch-1/sec-1"]["status"] == "done"
    assert rows["ch-1/sec-2"]["status"] == "missing"
    assert rows["ch-1/sec-2"]["reason"] == "timeout"
    assert cancelled == ["sec-2.md"]
    assert task.output_path.parent.joinpath("report/sec-2.rejected.md").is_file()
    report = task.output_path.read_text(encoding="utf-8")
    assert "已完成正文" in report and "原因：timeout" in report
    assert "半截原文" not in report
    assert result.succeeded is True and result.actual_count == 1
    assert [event["data"]["reason"] for event in events
            if event["type"] != "section_pool_composed"] == ["timeout"]


def test_墙钟在首次派活前误触发会按剩余预算重挂(tmp_path: Path) -> None:
    from app.orchestrator.scheduler import Scheduler

    plan = _deadline_plan(deadline_seconds=330, per_round=1, max_rounds=1)
    current = [datetime(2026, 8, 27, tzinfo=timezone.utc)]
    callbacks: list[tuple[float, object]] = []
    scheduler = Scheduler(
        plan, lambda agent, context: None, lambda event: None,
        lambda: current[0],
        lambda delay, callback: callbacks.append((delay, callback)),
        chapter_ledger=_store(tmp_path),
    )
    agent = plan.goals[0].agents[0]
    scheduler._agent_started_at[agent.agent_id] = current[0]
    scheduler._arm_chapter_deadline(agent, 330)
    current[0] += timedelta(seconds=2)
    callbacks[0][1]()

    assert [delay for delay, _ in callbacks] == [330.0, 328.0]
    assert agent.agent_id not in scheduler._deadline_expired


def test_断连不产生第二次重挂且调度器到点时刻不变(tmp_path: Path) -> None:
    from app.orchestrator.scheduler import Scheduler, TaskRunResult

    plan = _deadline_plan(deadline_seconds=330, per_round=1, max_rounds=1)
    current = [datetime(2026, 8, 27, tzinfo=timezone.utc)]
    callbacks: list[tuple[float, object]] = []
    contexts = []

    async def run_task(agent, context):
        contexts.append(context)
        return TaskRunResult(
            False,
            context.engine,
            reason="retry_exhausted",
            engine_error=TRANSPORT_ERROR,
        )

    scheduler = Scheduler(
        plan, run_task, lambda event: None,
        lambda: current[0],
        lambda delay, callback: callbacks.append((delay, callback)),
        chapter_ledger=_store(tmp_path),
    )
    goal = plan.goals[0]
    scheduler.goal_statuses[goal.goal_id] = "running"
    asyncio.run(scheduler._execute_agent(goal, goal.agents[0]))

    assert callbacks[0][0] == 330.0
    assert not hasattr(contexts[0], "extend_deadline")
    current[0] += timedelta(seconds=330)
    callbacks[0][1]()
    assert [delay for delay, _ in callbacks] == [330.0]
    assert goal.agents[0].agent_id in scheduler._deadline_expired


def test_stop取消在跑任务走与墙钟同一条取消路径(tmp_path: Path) -> None:
    """/stop 不再等在跑的 adapter 自己返回：与墙钟取消共用 _cancel_running_run。"""
    from app.orchestrator.scheduler import Scheduler

    store = _store(tmp_path)
    plan = _deadline_plan(deadline_seconds=3600, per_round=2, max_rounds=2)
    started = asyncio.Event()
    cancelled: list[str] = []

    async def run_task(agent, context):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(agent.agent_id)
            raise
        raise AssertionError("unreachable")  # pragma: no cover

    async def scenario():
        scheduler = Scheduler(
            plan, run_task, lambda event: None,
            lambda: datetime.now(timezone.utc), _real_timer, chapter_ledger=store,
        )
        driving = asyncio.create_task(scheduler.start())
        await asyncio.wait_for(started.wait(), timeout=5)
        await scheduler.stop()
        await asyncio.wait_for(driving, timeout=5)
        return scheduler

    scheduler = asyncio.run(scenario())
    assert cancelled == ["agent-1"], "/stop 必须取消在跑的 adapter 任务"
    assert scheduler.agent_statuses["agent-1"] == "queued"
    assert store.list_chapters("r-ledger")[0]["status"] == "pending"


RATED = {
    "competitor": "讯飞输入法", "permalink": "https://example.com/a",
    "score_authority": 2, "score_freshness": 1, "score_crossref": 1,
    "score_completeness": 2, "score_independence": 1,
    "rating_notes": "官方实测页", "rated_by": "unit",
}


def _json_agent(shape: str):
    return SimpleNamespace(
        chapter={"chapter_id": "ch-3", "opening": {"inputs": []}},
        output={"format": "json", "shape": shape},
    )


def _json_adapter(payloads: dict[str, str], calls: list[str]):
    from app.adapters import validation
    from app.adapters.contracts import EngineRunResult, OwliResult

    class Adapter:
        async def run(self, task, ctx, on_event=None):
            del on_event
            calls.append(task.output_path.name)
            task.output_path.parent.mkdir(parents=True, exist_ok=True)
            task.output_path.write_text(payloads[task.output_path.name], encoding="utf-8")
            return EngineRunResult(
                conclusion=OwliResult(
                    "done", str(task.output_path), "完成", [], [], [], None,
                ),
                conclusion_error=None,
                validation=validation.validate(ctx, task.validators),
                events=[], permission_denials=[],
            )

    return Adapter()


def _run_json(tmp_path, *, shape, payloads, validators):
    from app.orchestrator.sectioning import run_sectioned_task

    store = _store(tmp_path)
    runs_root = tmp_path / "runs"
    calls: list[str] = []
    events: list[dict] = []
    task = _task(runs_root, validators)
    task = type(task)(**{
        **{f: getattr(task, f) for f in task.__dataclass_fields__},
        "output_path": runs_root / "r-ledger/goals/goal-3/cross-validation.json",
        "output_format": "json",
    })
    result = asyncio.run(run_sectioned_task(
        plan=_plan(1), agent=_json_agent(shape),
        context=SimpleNamespace(goal_id="goal-3", engine="claude"),
        base_task=task, adapter=_json_adapter(payloads, calls), store=store,
        runs_root=runs_root, now_iso=lambda: "2026-08-24T00:00:00Z",
        on_event=lambda event: events.append(event), timer=lambda d, cb: cb(),
    ))
    rows = {r["chapter_id"]: r for r in store.list_chapters("r-ledger")}
    return SimpleNamespace(result=result, rows=rows, calls=calls, events=events,
                           output=task.output_path, store=store)


def test_shape为array的json章组装成数组且缺失清单另置(tmp_path: Path) -> None:
    payloads = {
        "sec-1.md": json.dumps([RATED], ensure_ascii=False),
        "sec-2.md": json.dumps([RATED], ensure_ascii=False),
    }
    run = _run_json(tmp_path, shape="array", payloads=payloads,
                    validators=["file_exists", "no_item_missing_rating"])
    document = json.loads(run.output.read_text(encoding="utf-8"))
    assert isinstance(document, list) and len(document) == 2
    missing = json.loads(
        run.output.with_name("cross-validation.missing.json").read_text(encoding="utf-8")
    )
    assert missing["缺失清单"] == []
    # 一轮即成：不复位已写成的节、不发组装错误
    assert run.result.succeeded is True
    assert run.calls == ["sec-1.md", "sec-2.md"]
    assert [row["status"] for row in run.rows.values() if "/sec-" in row["chapter_id"]] == [
        "done", "done",
    ]
    assert [e["type"] for e in run.events
            if e["type"] != "section_pool_composed"] == []


def test_shape为array但节产物形状不一致时conclusion_invalid且不进第二轮(tmp_path: Path) -> None:
    payloads = {
        "sec-1.md": json.dumps([RATED], ensure_ascii=False),
        "sec-2.md": "## 结论\n\n叙述体正文。\n",
    }
    run = _run_json(tmp_path, shape="array", payloads=payloads,
                    validators=["file_exists"])
    assert run.result.succeeded is False
    assert run.result.chapter_status == "missing"
    assert run.result.reason == "conclusion_invalid"
    assert [e["type"] for e in run.events
            if e["type"] != "section_pool_composed"] == ["section_assembly_error"]
    # 确定性失败不复位已写成的节
    assert run.rows["ch-3/sec-1"]["status"] == "done"


def test_声明json但整章都是叙述体时仍落对象文档(tmp_path: Path) -> None:
    payloads = {"sec-1.md": "## 结论\n\n甲。\n", "sec-2.md": "## 结论\n\n乙。\n"}
    run = _run_json(tmp_path, shape="array", payloads=payloads,
                    validators=["file_exists"])
    document = json.loads(run.output.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    assert [item["section_id"] for item in document["sections"]] == [
        "ch-3/sec-1", "ch-3/sec-2",
    ]
    assert run.result.succeeded is True


def _failing_adapter(
    calls: list[str], clock: list[datetime], step: float, engine_error: str,
):
    from app.adapters import validation
    from app.adapters.contracts import EngineRunResult, OwliResult

    class Adapter:
        async def run(self, task, ctx, on_event=None):
            del on_event
            calls.append(task.output_path.name)
            if task.output_path.name == "sec-1.md":
                task.output_path.parent.mkdir(parents=True, exist_ok=True)
                task.output_path.write_text(SECTION_BODY, encoding="utf-8")
                return EngineRunResult(
                    conclusion=OwliResult(
                        "done", str(task.output_path), "完成", [], [], [], None,
                    ),
                    conclusion_error=None,
                    validation=validation.validate(ctx, task.validators),
                    events=[], permission_denials=[],
                )
            clock[0] += timedelta(seconds=step)
            return EngineRunResult(
                conclusion=None, conclusion_error=None,
                validation=validation.ValidationReport(validation.Verdict.PASS, []),
                events=[], permission_denials=[], engine_error=engine_error,
                session_id=("session-b-prime" if engine_error == TRANSPORT_ERROR else None),
            )

    return Adapter()


def _run_budget(
    tmp_path, *, budget_seconds, step, engine_timeout=300.0,
    engine_error=TRANSPORT_ERROR,
):
    from app.orchestrator.sectioning import run_sectioned_task

    store = _store(tmp_path)
    runs_root = tmp_path / "runs"
    calls: list[str] = []
    events: list[dict] = []
    delays: list[float] = []
    clock = [datetime(2026, 8, 24, tzinfo=timezone.utc)]
    deadline_at = None if budget_seconds is None else clock[0] + timedelta(
        seconds=budget_seconds,
    )
    context = SimpleNamespace(
        goal_id="goal-3",
        engine="claude",
        section_deadline_seconds=budget_seconds,
    )

    def timer(delay, callback):
        delays.append(delay)
        clock[0] += timedelta(seconds=delay)
        callback()

    result = asyncio.run(run_sectioned_task(
        plan=_plan(10), agent=SimpleNamespace(
            chapter={"chapter_id": "ch-1", "opening": {"inputs": []}}),
        context=context,
        base_task=_task(runs_root, ["file_exists"]),
        adapter=_failing_adapter(calls, clock, step, engine_error), store=store,
        runs_root=runs_root, now_iso=lambda: clock[0].isoformat(),
        on_event=lambda event: events.append(event), timer=timer,
        now=lambda: clock[0], deadline_at=deadline_at,
        engine_timeout_seconds=engine_timeout,
    ))
    rows = {r["chapter_id"]: r for r in store.list_chapters("r-ledger")}
    return SimpleNamespace(
        result=result, rows=rows, calls=calls, events=events, delays=delays,
    )


def test_节级重试次数上限是独立常量不沿用max_attempts_per_round(tmp_path: Path) -> None:
    from app.orchestrator.sectioning import SECTION_RETRY_MAX_ATTEMPTS

    # max_attempts_per_round=10、墙钟够用：节级仍只派 SECTION_RETRY_MAX_ATTEMPTS 次
    assert SECTION_RETRY_MAX_ATTEMPTS <= 3
    run = _run_budget(tmp_path, budget_seconds=100_000, step=1)
    assert run.calls.count("sec-2.md") == SECTION_RETRY_MAX_ATTEMPTS
    assert run.rows["ch-1/sec-2"]["reason"] == "retry_exhausted"


def test_断连在预算内resume重试且预算耗尽即timeout(tmp_path: Path) -> None:
    # 330 s 节墙钟：第一次断连耗 100 s 后仍够退避并 resume；第二次后不够 136 s。
    run = _run_budget(tmp_path, budget_seconds=330, step=100)

    assert run.calls.count("sec-2.md") == 2
    assert run.rows["ch-1/sec-2"]["attempts"] == 2
    assert run.rows["ch-1/sec-2"]["reason"] == "timeout"
    assert run.delays == [5.0]
    retry_events = [event for event in run.events if event["type"] == "section_retry"]
    assert [event["data"] for event in retry_events] == [{
        "goal_id": "goal-3",
        "chapter_id": "ch-1/sec-2",
        "attempt": 2,
        "resume": True,
        "session_id": "session-b-prime",
    }]


def test_剩余节墙钟不足136秒时不重试且如实落timeout(tmp_path: Path) -> None:
    run = _run_budget(tmp_path, budget_seconds=330, step=200)

    assert run.calls.count("sec-2.md") == 1
    assert run.rows["ch-1/sec-2"]["reason"] == "timeout"
    assert run.rows["ch-1/sec-2"]["attempts"] == 1
    assert not [event for event in run.events if event["type"] == "section_retry"]
    assert run.delays == []
    # §X-1 货 2：闭集仍落 timeout，但事件里标 resume_floor 并留住真实原因。
    error = [e["data"] for e in run.events if e["type"] == "section_error"][0]
    assert error["timeout_kind"] == "resume_floor" and error["original_reason"] != "timeout"


def test_非断连的引擎超时不重试(tmp_path: Path) -> None:
    run = _run_budget(
        tmp_path,
        budget_seconds=100_000,
        step=300,
        engine_error="Claude 任务超时（300 秒），已终止并要求整任务重跑",
    )
    assert run.calls.count("sec-2.md") == 1
    assert run.rows["ch-1/sec-2"]["status"] == "missing"
    assert run.rows["ch-1/sec-2"]["reason"] == "timeout"
    assert run.rows["ch-1/sec-2"]["attempts"] == 1
    assert run.delays == []


def _resume_result(*, engine_error=None, session_id=None, resume_failed=False):
    from app.adapters import validation

    return SimpleNamespace(
        succeeded=False,
        conclusion=None,
        conclusion_error=None,
        validation=validation.ValidationReport(validation.Verdict.PASS, []),
        events=[],
        permission_denials=[],
        engine_error=engine_error,
        session_id=session_id,
        resume_failed=resume_failed,
    )


def _run_resume_case(tmp_path: Path, *, fail_resume: bool):
    from app.adapters import validation
    from app.adapters.contracts import EngineRunResult, OwliResult
    from app.orchestrator.sectioning import run_sectioned_task

    store = _store(tmp_path)
    runs_root = tmp_path / "runs"
    contexts: list[str | None] = []
    events: list[dict] = []

    class Adapter:
        async def run(self, task, ctx, on_event=None):
            del on_event
            if task.output_path.name == "sec-1.md":
                task.output_path.parent.mkdir(parents=True, exist_ok=True)
                task.output_path.write_text(SECTION_BODY, encoding="utf-8")
                return EngineRunResult(
                    conclusion=OwliResult(
                        "done", str(task.output_path), "完成", [], [], [], None,
                    ),
                    conclusion_error=None,
                    validation=validation.validate(ctx, task.validators),
                    events=[], permission_denials=[],
                )
            resume_session_id = getattr(ctx, "resume_session_id", None)
            contexts.append(resume_session_id)
            if len(contexts) == 1:
                return _resume_result(
                    engine_error=TRANSPORT_ERROR, session_id="session-d014",
                )
            if fail_resume and len(contexts) == 2:
                return _resume_result(
                    engine_error="Claude resume 不可用",
                    session_id="session-d014",
                    resume_failed=True,
                )
            task.output_path.write_text(SECTION_BODY, encoding="utf-8")
            return EngineRunResult(
                conclusion=OwliResult(
                    "done", str(task.output_path), "完成", [], [], [], None,
                ),
                conclusion_error=None,
                validation=validation.validate(ctx, task.validators),
                events=[], permission_denials=[],
            )

    result = asyncio.run(run_sectioned_task(
        plan=_plan(3),
        agent=SimpleNamespace(
            chapter={"chapter_id": "ch-1", "opening": {"inputs": []}},
        ),
        context=SimpleNamespace(goal_id="goal-3", engine="claude"),
        base_task=_task(runs_root, ["file_exists"]),
        adapter=Adapter(), store=store, runs_root=runs_root,
        now_iso=lambda: "2026-08-28T00:00:00+00:00",
        on_event=lambda event: events.append(event), timer=lambda delay, callback: callback(),
    ))
    return SimpleNamespace(result=result, contexts=contexts, events=events)


def test_断连重试优先带_session_id_resume_且事件可观测(tmp_path: Path) -> None:
    run = _run_resume_case(tmp_path, fail_resume=False)

    assert run.result.succeeded is True
    assert run.contexts == [None, "session-d014"]
    retry_events = [event for event in run.events if event["type"] == "section_retry"]
    assert [(event["data"]["resume"], event["data"]["session_id"])
            for event in retry_events] == [(True, "session-d014")]


def test_resume_失败同一次重试回退从头跑且事件_resume_false(tmp_path: Path) -> None:
    run = _run_resume_case(tmp_path, fail_resume=True)

    assert run.result.succeeded is True
    assert run.contexts == [None, "session-d014", None]
    retry_events = [event for event in run.events if event["type"] == "section_retry"]
    assert [(event["data"]["resume"], event["data"]["session_id"])
            for event in retry_events] == [
        (True, "session-d014"),
        (False, "session-d014"),
    ]
