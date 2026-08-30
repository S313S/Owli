"""§D-023：退避借用了限流的时钟 / 取消尸体把研究冻住。

真凶（离线复现于 r-13c9280b432e 底料）：等待退避的任务被章墙钟取消时，直接
``await backoff`` 会连带 cancel 共享的退避任务；尸体留在 ``_backoff_tasks``，
下一个等待者一 await 就抛 CancelledError 静默退出。交接原判的 resets_at
分支是潜在坑（status="allowed" 的纯信息播报），一并封住并封顶。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.adapters.routing import RoutedAdapter
from app.config import ResilienceConfig

NOW = datetime(2026, 8, 28, 17, 37, 34, tzinfo=timezone.utc)
CONFIG = ResilienceConfig(
    plan_segment_retries=1, backoff_initial_seconds=60, backoff_max_seconds=900
)
# 底料 events.sequence=408 那条 Claude CLI 纯信息播报：status=allowed，
# resets_at=1787950200（04:50:00 UTC+8），距 NOW 3 小时 12 分。
ALLOWED_INFO = {
    "status": "allowed", "resets_at": 1787950200,
    "rate_limit_type": "five_hour", "overage_status": "rejected",
}


def _adapter(slept: list[float]) -> RoutedAdapter:
    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        await asyncio.sleep(0.2)

    return RoutedAdapter(
        adapters={"claude": object(), "codex": object()},
        resilience_config=CONFIG, backoff_sleep=fake_sleep, utc_clock=lambda: NOW,
    )


def test_等待者被墙钟取消不得牵连共享退避_下一个等待者正常放行():
    async def scenario() -> tuple[str | None, bool, bool]:
        adapter = _adapter([])
        event = SimpleNamespace(raw={"subtype": "success"}, cause="transport")
        adapter._start_backoff("r", "claude", event)
        first = asyncio.create_task(adapter._await_route_gates("r", "claude"))
        await asyncio.sleep(0.05)
        first.cancel()  # 章墙钟到点：consistency-check-2 的重试被掐
        try:
            await first
        except asyncio.CancelledError:
            pass
        backoff = adapter._backoff_tasks[("r", "claude")]
        poisoned = backoff.cancelled()
        # cross-validation 起跑：修前这里抛 CancelledError，研究永远 running
        cause = await asyncio.wait_for(
            adapter._await_route_gates("r", "claude"), timeout=2
        )
        return cause, poisoned, bool(adapter._backoff_tasks)

    cause, poisoned, leftover = asyncio.run(scenario())
    assert cause == "transport"
    assert poisoned is False
    assert leftover is False


def test_退避任务自己被取消也按已释放处理_不向等待者抛取消():
    async def scenario() -> str | None:
        adapter = _adapter([])
        event = SimpleNamespace(raw={"subtype": "success"}, cause="transport")
        adapter._start_backoff("r", "claude", event)
        backoff = adapter._backoff_tasks[("r", "claude")]
        backoff.cancel()
        return await asyncio.wait_for(
            adapter._await_route_gates("r", "claude"), timeout=2
        )

    assert asyncio.run(scenario()) == "transport"


def test_status_allowed_的额度播报不得当限流时钟_回落档位表():
    async def scenario() -> float | None:
        adapter = _adapter([])
        event = SimpleNamespace(
            raw={"rate_limit_info": ALLOWED_INFO}, cause="transport"
        )
        return adapter._start_backoff("r", "claude", event)

    delay = asyncio.run(scenario())
    assert delay == 60.0
    assert delay < 3 * 3600  # 修前 = 11546 秒（睡到 04:50）


def test_真限流尊重resets_at但被backoff_max_seconds封顶():
    async def scenario() -> float | None:
        adapter = _adapter([])
        event = SimpleNamespace(
            raw={"rate_limit_info": {**ALLOWED_INFO, "status": "rejected"}},
            cause="rate_limit",
        )
        return adapter._start_backoff("r", "claude", event)

    assert asyncio.run(scenario()) == 900.0


def test_真限流resets_at在封顶内时照旧睡到重置点():
    async def scenario() -> float | None:
        adapter = _adapter([])
        resets_at = (NOW + timedelta(seconds=300)).isoformat()
        event = SimpleNamespace(
            raw={"api_error_status": 429, "resets_at": resets_at},
            cause="rate_limit",
        )
        return adapter._start_backoff("r", "claude", event)

    assert asyncio.run(scenario()) == 300.0


def test_退避开始事件带时长与预计恢复时刻(tmp_path):
    import inspect

    from app.adapters.capability import Capability
    from app.adapters.contracts import EngineTask
    from app.adapters.events import ItemKind, NormalizedEvent

    jitter = NormalizedEvent(
        engine="Claude", thread_id="s-1", turn_id="t-1",
        item_kind=ItemKind.ERROR, is_error=True,
        text="疑似网络抖动（代理/传输层）：success，原引擎退避重试",
        raw={"subtype": "success", "is_error": True},
        route_state="BACKOFF", suspend_new_tasks=True, cause="transport",
    )

    class FakeAdapter:
        def __init__(self, event=None):
            self.event = event

        async def run(self, task, ctx, on_event=None):
            del task, ctx
            if self.event is not None:
                returned = on_event(self.event)
                if inspect.isawaitable(returned):
                    await returned
            return "ok"

    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    adapter = RoutedAdapter(
        adapters={"claude": FakeAdapter(jitter), "codex": FakeAdapter()},
        resilience_config=CONFIG, backoff_sleep=fake_sleep, utc_clock=lambda: NOW,
    )
    task = EngineTask(
        body="退避可见", output_path=tmp_path / "result.md",
        output_format="markdown", research_id="r-1", goal_id="goal-1",
        agent_id="agent-1", agent_kind="report", validators=["file_exists"],
        capability=Capability(),
    )
    observed: list[NormalizedEvent] = []
    asyncio.run(adapter.run(task, object(), on_event=observed.append))

    started = [e for e in observed if e.raw.get("event") == "BACKOFF_STARTED"]
    assert [e.raw.get("event") for e in observed] == [None, "BACKOFF_STARTED"]
    assert started[0].raw["delay_seconds"] == 60.0
    assert started[0].raw["resume_at"] == "2026-08-28T17:38:34+00:00"
    assert started[0].route_state == "BACKOFF" and started[0].cause == "transport"
    assert "60 秒后重试" in started[0].text
    assert slept == [60.0]


def test_scheduler_无原因取消不再无声结束_至少发一条事件():
    from app.orchestrator.scheduler import Scheduler, TaskRunResult
    from tests.test_scheduler import FakeClockTimer, goal, plan_with_goals

    calls: list[int] = []
    events: list[dict] = []
    fake_time = FakeClockTimer()

    async def run_task(agent, context):
        calls.append(context.attempt)
        # 模拟 await 到了别人的取消尸体：既不是墙钟也不是 stop
        raise asyncio.CancelledError

    async def scenario() -> None:
        scheduler = Scheduler(
            plan_with_goals(goal(1)), run_task, events.append,
            fake_time.clock, fake_time.timer,
        )
        try:
            await asyncio.wait_for(scheduler.start(), timeout=1)
        except asyncio.TimeoutError:
            pass  # §AUTO-EXP 货 5 前调度会挂着；现在 goal 判 failed 后正常走完
        # 货 5（08-30 拍板）：无原因取消不再无声结束——goal 失败、不自动重试。
        assert scheduler.cancelled_without_reason is True
        assert scheduler.goal_statuses["goal-1"] == "failed"
        assert scheduler.status == "completed"

    asyncio.run(scenario())
    cancelled = [e for e in events if e.get("type") == "agent_run_cancelled"]
    assert calls == [1]  # 不自动重试：attempts 不增
    assert len(cancelled) == 1
    assert cancelled[0]["data"]["cancel_reason"] is None
    assert cancelled[0]["data"]["goal_id"] == "goal-1"
    assert cancelled[0]["is_error"] is True
    gates = [e for e in events if e.get("type") == "goal_gate"]
    assert gates and gates[0]["data"]["reason"] == "agent_run_cancelled"
