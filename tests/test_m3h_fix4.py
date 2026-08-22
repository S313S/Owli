from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.plan_factory import make_plan_dict
from tests.test_m3h_ledger import _store


def _plan(*, scale: str = "fast", attempts: int = 20):
    from app.plan.model import Plan

    source = make_plan_dict()
    source["research_id"] = "r-ledger"
    source["scale"] = scale
    source["baseline"] = None
    source["goals"] = source["goals"][:1]
    source["goals"][0]["retry_policy"].update(
        max_attempts_per_round=attempts,
        max_rounds=1,
        ask_engine_switch_at=attempts,
    )
    return Plan.from_dict(source)


def test_D1_采集章终态保留并截断最后一次双腿死因(tmp_path: Path):
    from app.orchestrator.scheduler import Scheduler

    store = _store(tmp_path)
    plan = _plan(attempts=1)
    long_engine_error = "引擎死因" * 700
    long_conclusion_error = "结论死因" * 700

    async def run_task(agent, context):
        return SimpleNamespace(
            succeeded=False,
            engine=context.engine,
            engine_error=long_engine_error,
            conclusion_error=long_conclusion_error,
            conclusion=None,
            validation=SimpleNamespace(results=[]),
        )

    scheduler = Scheduler(
        plan,
        run_task,
        lambda event: None,
        lambda: datetime(2026, 8, 22, tzinfo=timezone.utc),
        lambda delay, callback: None,
        chapter_ledger=store,
    )
    asyncio.run(scheduler.start())

    row = store.list_chapters("r-ledger")[0]
    assert row["engine_error"] == long_engine_error[:2000]
    assert row["conclusion_error"] == long_conclusion_error[:2000]


def test_D1_runtime手工映射TaskRunResult不丢适配器双腿死因(tmp_path: Path):
    from app.orchestrator.runtime import RuntimeCoordinator

    plan = _plan(attempts=1)
    agent = plan.goals[0].agents[0]
    adapter_result = SimpleNamespace(
        succeeded=False,
        conclusion=SimpleNamespace(reason="empty_result"),
        conclusion_error="结构化结论原文",
        engine_error="引擎错误原文",
        events=[],
    )

    class Adapter:
        async def run(self, task, ctx, on_event=None):
            return adapter_result

    class Events:
        async def publish(self, research_id, payload):
            return None

    coordinator = RuntimeCoordinator(
        store=SimpleNamespace(),
        event_buffer=Events(),
        researches={},
        cards={},
        runs_root=tmp_path / "runs",
        routing_utc_clock=lambda: datetime(2026, 8, 22, tzinfo=timezone.utc),
    )
    coordinator._adapters[plan.research_id] = Adapter()

    async def sink(event):
        return None

    result = asyncio.run(coordinator._run_task(
        plan,
        agent,
        SimpleNamespace(
            goal_id="goal-1",
            attempt=1,
            engine="codex",
            failure_feedback=None,
            on_event=sink,
        ),
    ))
    assert result.chapter_status == "missing"
    assert result.engine_error == "引擎错误原文"
    assert result.conclusion_error == "结构化结论原文"


def test_D2_同一归一化死因三次收敛且相邻尝试至少间隔五秒(tmp_path: Path):
    from app.orchestrator.scheduler import Scheduler, TaskRunResult

    store = _store(tmp_path)
    plan = _plan(scale="fast")
    current = [datetime(2026, 8, 22, tzinfo=timezone.utc)]
    starts: list[datetime] = []
    original_start = store.start_chapter

    def start_chapter(*args, **kwargs):
        starts.append(datetime.fromisoformat(kwargs["updated_at"]))
        return original_start(*args, **kwargs)

    store.start_chapter = start_chapter  # type: ignore[method-assign]

    def timer(delay: float, callback):
        if delay <= 15:
            current[0] += timedelta(seconds=delay)
            callback()
        return object()

    async def run_task(agent, context):
        changing = f"2026-08-22T00:00:0{context.attempt}Z id={context.attempt:016x}"
        return TaskRunResult(
            False,
            context.engine,
            conclusion_error=f"确定性拒绝 {changing}",
            engine_error=f"transport {changing}",
        )

    scheduler = Scheduler(
        plan, run_task, lambda event: None, lambda: current[0], timer,
        chapter_ledger=store,
    )
    asyncio.run(scheduler.start())

    row = store.list_chapters("r-ledger")[0]
    assert row["attempts"] == 3
    assert row["status"] == "missing"
    assert row["reason"] == "retry_exhausted"
    assert all(
        (right - left).total_seconds() >= 5
        for left, right in zip(starts, starts[1:])
    )


def test_D3_runtime注入根贯通EngineTask与Codex三处路径(tmp_path: Path):
    from app.adapters import codex, validation
    from app.adapters.capability import Capability, FileSystemScope
    from app.adapters.contracts import EngineTask

    runs_root = tmp_path / "injected-runs"
    output = runs_root / "r-ledger" / "goals" / "goal-1" / "result.md"
    task = EngineTask(
        body="测试",
        output_path=output,
        output_format="markdown",
        research_id="r-ledger",
        goal_id="goal-1",
        agent_id="agent-1",
        agent_kind="data_collection",
        validators=["file_exists"],
        capability=Capability(
            profile="custom",
            tools=("fs.write",),
            fs=FileSystemScope(write=("goals/goal-1/**",)),
        ),
        runs_root=runs_root,
    )
    ctx = validation.Ctx(
        output_path=output,
        output_format="markdown",
        research_id="r-ledger",
        goal_id="goal-1",
        agent_id="agent-1",
        read_text=lambda: "",
        read_json=lambda: {},
        store=None,
        source_domains=frozenset(),
        runs_root=runs_root,
    )

    assert validation.runs_root_of(task) == runs_root
    assert codex._task_contract_failure(task, ctx) is None
    command = codex.build_codex_command(task)
    assert str(output.parent) == command[command.index("-C") + 1]
    assert str(runs_root / "r-ledger" / "goals" / "goal-1") in command


def test_D3_契约拒绝消息直接展示前三条差异(tmp_path: Path):
    from app.adapters import codex, validation
    from app.adapters.capability import Capability
    from app.adapters.contracts import EngineTask

    runs_root = tmp_path / "runs"
    task = EngineTask(
        body="测试",
        output_path=runs_root / "r" / "goals" / "goal-1" / "result.md",
        output_format="markdown",
        research_id="r",
        goal_id="goal-1",
        agent_id="agent-1",
        agent_kind="data_collection",
        validators=["file_exists"],
        capability=Capability(),
        runs_root=runs_root,
    )
    ctx = validation.Ctx(
        output_path=tmp_path / "wrong.md",
        output_format="json",
        research_id="other",
        goal_id="goal-2",
        agent_id="agent-2",
        read_text=lambda: "",
        read_json=lambda: {},
        store=None,
        source_domains=frozenset(),
        runs_root=runs_root,
    )

    failure = codex._task_contract_failure(task, ctx)
    assert failure is not None
    assert failure.offenders[:3]
    assert all(item in failure.message for item in failure.offenders[:3])


def test_D4_start_research返回前排干自动放行任务(monkeypatch, tmp_path: Path):
    from app.orchestrator import runtime as runtime_module
    from app.orchestrator.runtime import RuntimeCoordinator
    from app.plan.cards import Card, CardActionType, CardBlocking, CardStatus, CardType

    plan = _plan(attempts=1)

    class Events:
        async def publish(self, research_id, payload):
            return None

    class FakeScheduler:
        def __init__(self, plan, run_task, emit, clock, timer, chapter_ledger=None):
            self.plan = plan
            self.emit = emit
            self.status = "running"
            self.goal_statuses = {"goal-1": "awaiting_intervention"}
            self.agent_statuses = {"agent-1": "done"}

        async def start(self):
            card = Card(
                card_id="card-auto",
                card_type=CardType.INTERVENE,
                research_id=self.plan.research_id,
                goal_id="goal-1",
                agent_id=None,
                title="确认",
                body="继续",
                target={},
                actions=[{
                    "type": CardActionType.CHOICE_2.value,
                    "options": ["继续", "调整"],
                }],
                blocking=CardBlocking.GOAL,
                deadline=None,
                status=CardStatus.PENDING,
                result=None,
                created_at="2026-08-22T00:00:00+00:00",
                resolved_at=None,
            )
            await self.emit(card.to_event())

        async def answer_card(self, card_id, result):
            await asyncio.sleep(0)
            self.goal_statuses["goal-1"] = "done"
            self.status = "completed"

    monkeypatch.setattr(runtime_module, "Scheduler", FakeScheduler)
    monkeypatch.setattr(runtime_module, "load_plan", lambda store, research_id: plan)
    coordinator = RuntimeCoordinator(
        store=SimpleNamespace(),
        event_buffer=Events(),
        researches={},
        cards={},
        runs_root=tmp_path / "runs",
        auto_confirm=True,
        routing_utc_clock=lambda: datetime(2026, 8, 22, tzinfo=timezone.utc),
    )
    coordinator.researches[plan.research_id] = coordinator._state_from_plan(plan)
    finalized: list[str] = []

    async def finalize(research_id: str):
        finalized.append(coordinator.scheduler_for(research_id).status)

    coordinator._finalize_if_terminal = finalize  # type: ignore[method-assign]
    asyncio.run(coordinator.start_research(plan))

    scheduler = coordinator.scheduler_for(plan.research_id)
    assert scheduler.status == "completed"
    assert scheduler.goal_statuses == {"goal-1": "done"}
    assert finalized and set(finalized) == {"completed"}


def test_D4_resume返回前同样排干自动任务(tmp_path: Path):
    from app.orchestrator.runtime import RuntimeCoordinator

    class Events:
        async def publish(self, research_id, payload):
            return None

    coordinator = RuntimeCoordinator(
        store=SimpleNamespace(),
        event_buffer=Events(),
        researches={},
        cards={},
        runs_root=tmp_path / "runs",
        routing_utc_clock=lambda: datetime(2026, 8, 22, tzinfo=timezone.utc),
    )
    completed: list[str] = []

    class Scheduler:
        async def resume(self):
            async def finish():
                await asyncio.sleep(0)
                completed.append("auto")

            coordinator._track_auto_task(asyncio.create_task(finish()))

    coordinator._schedulers["r-ledger"] = Scheduler()
    finalized: list[str] = []

    async def finalize(research_id: str):
        finalized.append(research_id)

    coordinator._finalize_if_terminal = finalize  # type: ignore[method-assign]
    asyncio.run(coordinator.resume("r-ledger"))

    assert completed == ["auto"]
    assert finalized == ["r-ledger"]


def test_D5_Plan与Scheduler都拒绝跨goal重复agent_id():
    from app.orchestrator.scheduler import Scheduler
    from app.plan.model import Plan

    source = make_plan_dict()
    source["baseline"] = None
    duplicate = source["goals"][0]["agents"][0]["agent_id"]
    source["goals"][1]["agents"][0]["agent_id"] = duplicate
    with pytest.raises(ValueError, match=rf"agent_id 跨 goal 重复.*{duplicate}"):
        Plan.from_dict(source)

    valid = Plan.from_dict(make_plan_dict())
    valid.goals[1].agents[0].agent_id = valid.goals[0].agents[0].agent_id
    with pytest.raises(ValueError, match=rf"agent_id 跨 goal 重复.*{duplicate}"):
        Scheduler(
            valid,
            lambda agent, context: None,
            lambda event: None,
            lambda: datetime(2026, 8, 22, tzinfo=timezone.utc),
            lambda delay, callback: None,
        )
