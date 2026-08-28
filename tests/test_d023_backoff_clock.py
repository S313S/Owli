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

