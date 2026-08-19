from __future__ import annotations

import asyncio
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Any
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from plan_factory import make_agent, make_goal, make_plan_dict


def async_test(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return wrapper


class FakeClockTimer:
    def __init__(self) -> None:
        self.now = datetime(2026, 8, 19, tzinfo=timezone.utc)
        self.jobs: list[tuple[datetime, Any]] = []

    def clock(self) -> datetime:
        return self.now

    def timer(self, delay_seconds: float, callback: Any) -> None:
        self.jobs.append((self.now + timedelta(seconds=delay_seconds), callback))

    async def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)
        while True:
            due = [job for job in self.jobs if job[0] <= self.now]
            if not due:
                return
            due.sort(key=lambda job: job[0])
            job = due[0]
            self.jobs.remove(job)
            result = job[1]()
            if asyncio.iscoroutine(result):
                await result


def plan_with_goals(*goals: dict[str, Any]):
    from app.plan.model import Plan

    source = make_plan_dict()
    source["goals"] = list(goals)
    source["baseline"] = None
    return Plan.from_dict(source)


def goal(number: int, *, depends_on=(), policy="skip", agents=None):
    result = make_goal(number)
    result["depends_on"] = list(depends_on)
    result["on_upstream_failure"] = policy
    if agents is not None:
        result["agents"] = agents
    return result


def card_events(events: list[dict[str, Any]], card_type: str):
    return [
        event["data"]["card"]
        for event in events
        if event.get("type") == "card_update"
        and event["data"]["card"]["card_type"] == card_type
    ]


async def continue_all_interventions(scheduler, events) -> None:
    answered: set[str] = set()
    while True:
        cards = card_events(events, "INTERVENE")
        pending = [card for card in cards if card["card_id"] not in answered]
        if not pending:
            return
        for card in pending:
            answered.add(card["card_id"])
            await scheduler.answer_card(card["card_id"], {"choice": "continue"})


@async_test
async def test_并行两_goal_同时起且汇合等待两者_done():
    from app.orchestrator.scheduler import Scheduler, TaskRunResult

    started: list[str] = []
    release = asyncio.Event()
    events: list[dict[str, Any]] = []

    async def run_task(agent, context):
        started.append(context.goal_id)
        if context.goal_id in {"goal-1", "goal-2"}:
            await release.wait()
        return TaskRunResult(succeeded=True, engine=agent.engine)

    first = goal(1)
    second = goal(2)
    second["depends_on"] = []
    joined = goal(3, depends_on=("goal-1", "goal-2"))
    fake_time = FakeClockTimer()
    scheduler = Scheduler(
        plan_with_goals(first, second, joined), run_task, events.append,
        fake_time.clock, fake_time.timer,
    )

    running = asyncio.create_task(scheduler.start())
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert set(started) == {"goal-1", "goal-2"}
    assert "goal-3" not in started

    release.set()
    await running
    assert scheduler.goal_statuses == {
        "goal-1": "awaiting_intervention",
        "goal-2": "awaiting_intervention",
        "goal-3": "pending",
    }
    await continue_all_interventions(scheduler, events)
    assert started[-1] == "goal-3"


@async_test
async def test_上游失败传播_skip_run_anyway_及无依赖照跑():
    from app.orchestrator.scheduler import Scheduler, TaskRunResult

    calls: defaultdict[str, int] = defaultdict(int)
    events: list[dict[str, Any]] = []

    async def run_task(agent, context):
        calls[context.goal_id] += 1
        return TaskRunResult(
            succeeded=context.goal_id != "goal-1", engine=agent.engine
        )

    failed = goal(1)
    failed["retry_policy"]["max_attempts_per_round"] = 1
    failed["retry_policy"]["max_rounds"] = 1
    skipped = goal(2, depends_on=("goal-1",))
    anyway = goal(3, depends_on=("goal-1",), policy="run_anyway")
    independent = goal(4)
    independent["depends_on"] = []
    fake_time = FakeClockTimer()
    scheduler = Scheduler(
        plan_with_goals(failed, skipped, anyway, independent),
        run_task, events.append, fake_time.clock, fake_time.timer,
    )

    await scheduler.start()

    assert scheduler.goal_statuses["goal-1"] == "failed"
    assert scheduler.goal_statuses["goal-2"] == "skipped"
    assert calls["goal-2"] == 0
    assert calls["goal-3"] == 1
    assert calls["goal-4"] == 1
    snapshot = scheduler.progress()
    assert snapshot["total"] == 3
    assert snapshot["done"] == 0


def test_入口防御性拒绝_goal_环和_agent_环():
    from app.orchestrator.scheduler import Scheduler

    fake_time = FakeClockTimer()
    first = goal(1, depends_on=("goal-2",))
    second = goal(2, depends_on=("goal-1",))
    with pytest.raises(ValueError, match="环"):
        Scheduler(
            plan_with_goals(first, second), lambda *_: None, lambda _: None,
            fake_time.clock, fake_time.timer,
        )

    a = make_agent("agent-a", "goal-1")
    b = make_agent("agent-b", "goal-1")
    a["depends_on"] = ["agent-b"]
    b["depends_on"] = ["agent-a"]
    with pytest.raises(ValueError, match="环"):
        Scheduler(
            plan_with_goals(goal(1, agents=[a, b])),
            lambda *_: None, lambda _: None, fake_time.clock, fake_time.timer,
        )


@async_test
async def test_C3_第5次发卡_不阻塞第6次_第11次换引擎_20次失败():
    from app.orchestrator.scheduler import Scheduler, TaskRunResult

    attempts: list[tuple[int, str]] = []
    sixth_started = asyncio.Event()
    release_sixth = asyncio.Event()
    events: list[dict[str, Any]] = []

    async def run_task(agent, context):
        attempts.append((context.attempt, context.engine))
        if context.attempt == 6:
            sixth_started.set()
            await release_sixth.wait()
        return TaskRunResult(succeeded=False, engine=context.engine)

    fake_time = FakeClockTimer()
    scheduler = Scheduler(
        plan_with_goals(goal(1)), run_task, events.append,
        fake_time.clock, fake_time.timer,
    )
    running = asyncio.create_task(scheduler.start())
    await sixth_started.wait()

    switch_cards = card_events(events, "ENGINE_SWITCH_CONFIRM")
    assert len(switch_cards) == 1
    assert attempts[:6] == [(number, "claude") for number in range(1, 7)]

    await scheduler.answer_card(
        switch_cards[0]["card_id"],
        {"choice": "switch", "engine": "codex"},
    )
    release_sixth.set()
    await running

    assert attempts[9] == (10, "claude")
    assert attempts[10] == (11, "codex")
    assert attempts[-1] == (20, "codex")
    assert scheduler.goal_statuses["goal-1"] == "failed"


@async_test
async def test_goal_推进12小时触发总闸且忽略迟到结果():
    from app.orchestrator.scheduler import Scheduler, TaskRunResult

    started = asyncio.Event()
    release = asyncio.Event()
    fake_time = FakeClockTimer()

    async def run_task(agent, context):
        started.set()
        await release.wait()
        return TaskRunResult(succeeded=True, engine=agent.engine)

    scheduler = Scheduler(
        plan_with_goals(goal(1)), run_task, lambda _: None,
        fake_time.clock, fake_time.timer,
    )
    running = asyncio.create_task(scheduler.start())
    await started.wait()
    await fake_time.advance(hours=12)

    assert scheduler.goal_statuses["goal-1"] == "failed"
    release.set()
    await running
    assert scheduler.goal_statuses["goal-1"] == "failed"


@async_test
async def test_BACKOFF_只挂起同引擎新任务_timer到点恢复():
    from app.adapters.events import ItemKind, NormalizedEvent
    from app.adapters.ratelimit import RouteState
    from app.orchestrator.scheduler import Scheduler, TaskRunResult

    calls: list[str] = []
    fake_time = FakeClockTimer()

    async def run_task(agent, context):
        calls.append(agent.agent_id)
        if agent.agent_id == "agent-a":
            event = NormalizedEvent(
                engine="claude", thread_id="t", turn_id="u",
                item_kind=ItemKind.ERROR, text="429", is_error=True,
                raw={"api_error_status": 429},
                route_state=RouteState.BACKOFF.value,
                suspend_new_tasks=True,
            )
            await context.on_event(event)
        return TaskRunResult(succeeded=True, engine=agent.engine)

    first_agent = make_agent("agent-a", "goal-1")
    second_agent = make_agent("agent-b", "goal-1")
    second_agent["depends_on"] = ["agent-a"]
    scheduler = Scheduler(
        plan_with_goals(goal(1, agents=[first_agent, second_agent])),
        run_task, lambda _: None,
        fake_time.clock, fake_time.timer,
    )
    await scheduler.start()

    assert calls == ["agent-a"]
    await fake_time.advance(seconds=59)
    assert calls == ["agent-a"]
    await fake_time.advance(seconds=1)
    assert calls == ["agent-a", "agent-b"]


@async_test
async def test_R8_发额外额度卡_15分钟默认切换并标记过期():
    from app.adapters.ratelimit import RouteDecision, RouteState
    from app.orchestrator.scheduler import Scheduler, TaskRunResult

    calls: list[str] = []
    events: list[dict[str, Any]] = []
    fake_time = FakeClockTimer()

    async def run_task(agent, context):
        calls.append(agent.agent_id)
        if agent.agent_id == "agent-a":
            await context.on_event(RouteDecision(
                RouteState.WARN,
                "seven_day 限流；overage 可用，继续跑会计费，等待用户确认",
                {"overageStatus": "allowed"},
                suspend_new_tasks=True,
            ))
        return TaskRunResult(succeeded=True, engine=context.engine)

    first_agent = make_agent("agent-a", "goal-1")
    second_agent = make_agent("agent-b", "goal-1")
    second_agent["depends_on"] = ["agent-a"]
    scheduler = Scheduler(
        plan_with_goals(goal(1, agents=[first_agent, second_agent])),
        run_task, events.append, fake_time.clock, fake_time.timer,
    )
    await scheduler.start()

    cards = card_events(events, "EXTRA_QUOTA_CONFIRM")
    assert len(cards) == 1
    assert cards[0]["deadline"] == "2026-08-19T00:15:00+00:00"
    assert calls == ["agent-a"]

    await fake_time.advance(minutes=15)

    updates = card_events(events, "EXTRA_QUOTA_CONFIRM")
    assert updates[-1]["status"] == "expired_defaulted"
    assert scheduler.future_engine == "codex"
    assert calls == ["agent-a", "agent-b"]


@async_test
async def test_完成先干预_两个并行goal各一张卡_继续才放下游():
    from app.orchestrator.scheduler import Scheduler, TaskRunResult

    calls: list[str] = []
    events: list[dict[str, Any]] = []
    fake_time = FakeClockTimer()

    async def run_task(agent, context):
        calls.append(context.goal_id)
        return TaskRunResult(succeeded=True, engine=context.engine)

    second = goal(2)
    second["depends_on"] = []
    third = goal(3, depends_on=("goal-1", "goal-2"))
    scheduler = Scheduler(
        plan_with_goals(goal(1), second, third), run_task, events.append,
        fake_time.clock, fake_time.timer,
    )
    await scheduler.start()

    cards = card_events(events, "INTERVENE")
    assert len(cards) == 2
    assert {card["goal_id"] for card in cards} == {"goal-1", "goal-2"}
    assert {card["blocking"] for card in cards} == {"goal"}
    assert "goal-3" not in calls

    await scheduler.answer_card(cards[0]["card_id"], {"choice": "continue"})
    assert "goal-3" not in calls
    await scheduler.answer_card(cards[1]["card_id"], {"choice": "continue"})
    assert calls[-1] == "goal-3"


@async_test
async def test_pause_让在跑的完成但不再起新_agent_resume后恢复():
    from app.orchestrator.scheduler import Scheduler, TaskRunResult

    calls: list[str] = []
    started = asyncio.Event()
    release = asyncio.Event()
    fake_time = FakeClockTimer()

    async def run_task(agent, context):
        calls.append(agent.agent_id)
        if agent.agent_id == "agent-a":
            started.set()
            await release.wait()
        return TaskRunResult(succeeded=True, engine=context.engine)

    first_agent = make_agent("agent-a", "goal-1")
    second_agent = make_agent("agent-b", "goal-1")
    second_agent["depends_on"] = ["agent-a"]
    scheduler = Scheduler(
        plan_with_goals(goal(1, agents=[first_agent, second_agent])),
        run_task, lambda _: None, fake_time.clock, fake_time.timer,
    )
    running = asyncio.create_task(scheduler.start())
    await started.wait()
    await scheduler.pause()
    release.set()
    await running

    assert calls == ["agent-a"]
    assert scheduler.agent_statuses["agent-a"] == "done"
    assert scheduler.agent_statuses["agent-b"] == "queued"

    await scheduler.resume()
    assert calls == ["agent-a", "agent-b"]


@async_test
async def test_progress_只读运行时状态且不改写_plan():
    from app.orchestrator.scheduler import Scheduler, TaskRunResult

    fake_time = FakeClockTimer()
    source_plan = plan_with_goals(goal(1))
    before = source_plan.to_json()

    async def run_task(agent, context):
        return TaskRunResult(succeeded=True, engine=context.engine)

    scheduler = Scheduler(
        source_plan, run_task, lambda _: None, fake_time.clock, fake_time.timer,
    )
    await scheduler.start()
    card = card_events(scheduler.emitted_events, "INTERVENE")[0]
    await scheduler.answer_card(card["card_id"], {"choice": "continue"})

    assert scheduler.progress() == {
        "done": 1,
        "total": 1,
        "agents": {"agent-1": "done"},
    }
    assert source_plan.to_json() == before


@async_test
async def test_stop_进入终态且不再起新任务():
    from app.orchestrator.scheduler import Scheduler, TaskRunResult

    calls: list[str] = []
    started = asyncio.Event()
    release = asyncio.Event()
    fake_time = FakeClockTimer()

    async def run_task(agent, context):
        calls.append(agent.agent_id)
        started.set()
        await release.wait()
        return TaskRunResult(succeeded=True, engine=context.engine)

    first_agent = make_agent("agent-a", "goal-1")
    second_agent = make_agent("agent-b", "goal-1")
    second_agent["depends_on"] = ["agent-a"]
    scheduler = Scheduler(
        plan_with_goals(goal(1, agents=[first_agent, second_agent])),
        run_task, lambda _: None, fake_time.clock, fake_time.timer,
    )
    running = asyncio.create_task(scheduler.start())
    await started.wait()
    await scheduler.stop()
    release.set()
    await running

    assert scheduler.status == "stopped"
    assert calls == ["agent-a"]


@async_test
async def test_WARN_挂起原引擎并让后续新任务走_future_engine():
    from app.adapters.ratelimit import RouteDecision, RouteState
    from app.orchestrator.scheduler import Scheduler, TaskRunResult

    engines: list[str] = []
    fake_time = FakeClockTimer()

    async def run_task(agent, context):
        engines.append(context.engine)
        if agent.agent_id == "agent-a":
            await context.on_event(RouteDecision(
                RouteState.WARN, "接近上限；后续新任务让路", {},
                failover_target="codex", scope="new_tasks",
                allow_current_task_to_finish=True,
            ))
        return TaskRunResult(succeeded=True, engine=context.engine)

    first_agent = make_agent("agent-a", "goal-1")
    second_agent = make_agent("agent-b", "goal-1")
    second_agent["depends_on"] = ["agent-a"]
    scheduler = Scheduler(
        plan_with_goals(goal(1, agents=[first_agent, second_agent])),
        run_task, lambda _: None, fake_time.clock, fake_time.timer,
    )
    await scheduler.start()

    assert engines == ["claude", "codex"]
    assert scheduler.future_engine == "codex"


@async_test
async def test_FAILOVER_只更新后续让路目标_不改在跑任务结果():
    from app.adapters.ratelimit import RouteDecision, RouteState
    from app.orchestrator.scheduler import Scheduler, TaskRunResult

    fake_time = FakeClockTimer()
    seen: list[str] = []

    async def run_task(agent, context):
        seen.append(context.engine)
        if agent.agent_id == "agent-a":
            await context.on_event(RouteDecision(
                RouteState.FAILOVER, "当前引擎不可用", {},
                failover_target="codex", scope="new_tasks",
                allow_current_task_to_finish=True,
            ))
        return TaskRunResult(succeeded=True, engine=context.engine)

    first_agent = make_agent("agent-a", "goal-1")
    second_agent = make_agent("agent-b", "goal-1")
    second_agent["depends_on"] = ["agent-a"]
    scheduler = Scheduler(
        plan_with_goals(goal(1, agents=[first_agent, second_agent])),
        run_task, lambda _: None, fake_time.clock, fake_time.timer,
    )
    await scheduler.start()

    assert seen == ["claude", "codex"]
    assert scheduler.agent_statuses == {"agent-a": "done", "agent-b": "done"}


@async_test
async def test_BACKOFF_同一_agent_重试也必须等待_timer():
    from app.adapters.ratelimit import RouteDecision, RouteState
    from app.orchestrator.scheduler import Scheduler, TaskRunResult

    calls: list[int] = []
    fake_time = FakeClockTimer()

    async def run_task(agent, context):
        calls.append(context.attempt)
        if context.attempt == 1:
            await context.on_event(RouteDecision(
                RouteState.BACKOFF, "429", {"api_error_status": 429},
                suspend_new_tasks=True,
            ))
            return TaskRunResult(succeeded=False, engine=context.engine)
        return TaskRunResult(succeeded=True, engine=context.engine)

    scheduler = Scheduler(
        plan_with_goals(goal(1)), run_task, lambda _: None,
        fake_time.clock, fake_time.timer,
    )
    await scheduler.start()
    assert calls == [1]

    await fake_time.advance(seconds=60)
    assert calls == [1, 2]


@async_test
async def test_pause_同时拦住同一_agent_下一次重试():
    from app.orchestrator.scheduler import Scheduler, TaskRunResult

    calls: list[int] = []
    started = asyncio.Event()
    release = asyncio.Event()
    fake_time = FakeClockTimer()

    async def run_task(agent, context):
        calls.append(context.attempt)
        if context.attempt == 1:
            started.set()
            await release.wait()
            return TaskRunResult(succeeded=False, engine=context.engine)
        return TaskRunResult(succeeded=True, engine=context.engine)

    scheduler = Scheduler(
        plan_with_goals(goal(1)), run_task, lambda _: None,
        fake_time.clock, fake_time.timer,
    )
    running = asyncio.create_task(scheduler.start())
    await started.wait()
    await scheduler.pause()
    release.set()
    await running
    assert calls == [1]

    await scheduler.resume()
    assert calls == [1, 2]


@async_test
async def test_R8_回答不接受计费_立即让路而不是误判为接受():
    from app.adapters.ratelimit import RouteDecision, RouteState
    from app.orchestrator.scheduler import Scheduler, TaskRunResult

    events: list[dict[str, Any]] = []
    fake_time = FakeClockTimer()

    async def run_task(agent, context):
        await context.on_event(RouteDecision(
            RouteState.WARN, "额度耗尽，overage 可用，继续跑会计费", {},
            suspend_new_tasks=True,
        ))
        return TaskRunResult(succeeded=True, engine=context.engine)

    scheduler = Scheduler(
        plan_with_goals(goal(1)), run_task, events.append,
        fake_time.clock, fake_time.timer,
    )
    await scheduler.start()
    card = card_events(events, "EXTRA_QUOTA_CONFIRM")[0]
    await scheduler.answer_card(
        card["card_id"], {"choice": "不接受，切换引擎"}
    )

    assert scheduler.future_engine == "codex"
