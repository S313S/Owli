"""D-027：告警级限流不许掐掉正在跑的章，只让新任务让路。

重放 `r-5d22d1ca6392` 实测：账号 seven_day 已用 79%，SDK 发 `allowed_warning`，
路由决策写的是 `scope=new_tasks` + `allow_current_task_to_finish=True`，
但 `_run_task` 只要在事件里看见 `rate_limit` 就把本章判 `quota_exhausted` 退避；
六个评级章第一轮全被掐，重试撞满 330 s 墙钟，全部 missing、评过 0 行。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from tests.test_m3h_missing_exits import make_plan_dict


def _coordinator(tmp_path: Path, event):
    from app.orchestrator.runtime import RuntimeCoordinator
    from app.plan.model import Plan

    source = make_plan_dict()
    source["research_id"] = "r-d027"
    source["goals"] = source["goals"][:1]
    source["baseline"] = None
    plan = Plan.from_dict(source)

    class Store:
        def list_chapters(self, research_id):
            return []

    class Events:
        async def publish(self, research_id, payload):
            return None

    class Adapter:
        async def run(self, task, ctx, on_event=None):
            await on_event(event)
            task.output_path.parent.mkdir(parents=True, exist_ok=True)
            task.output_path.write_text("[]", encoding="utf-8")
            return SimpleNamespace(
                succeeded=True,
                conclusion=SimpleNamespace(reason=None, output_path=None),
                events=[], engine_error=None, conclusion_error=None,
                validation=SimpleNamespace(results=[]),
            )

    coordinator = RuntimeCoordinator(
        store=Store(), event_buffer=Events(), researches={}, cards={},
        adapter_factory=lambda: Adapter(), runs_root=tmp_path / "runs",
        routing_utc_clock=lambda: datetime(2026, 8, 30, tzinfo=timezone.utc),
    )
    coordinator._adapters[plan.research_id] = Adapter()
    return coordinator, plan, plan.goals[0].agents[0]


def _run(coordinator, plan, agent):
    async def sink(event):
        return None

    return asyncio.run(coordinator._run_task(
        plan, agent,
        SimpleNamespace(goal_id="goal-1", attempt=1, engine="codex",
                        failure_feedback=None, on_event=sink),
    ))


def _event(**overrides):
    from app.adapters.events import ItemKind, NormalizedEvent

    payload = dict(
        engine="claude", thread_id=None, turn_id=None,
        item_kind=ItemKind.THINKING, text="[RateLimitEvent]", is_error=False,
        raw={"event": "RateLimitEvent"}, route_state="WARN", cause="rate_limit",
    )
    payload.update(overrides)
    return NormalizedEvent(**payload)


def test_告警级限流_在跑的章不被判退避(tmp_path: Path) -> None:
    coordinator, plan, agent = _coordinator(tmp_path, _event(
        scope="new_tasks", allow_current_task_to_finish=True,
        failover_target="codex", no_fallback_left=True,
    ))

    result = _run(coordinator, plan, agent)

    assert getattr(result, "reason", None) != "quota_exhausted"
    assert getattr(result, "chapter_status", None) != "deferred"


def test_真限流_没有让路标志时照旧退避(tmp_path: Path) -> None:
    coordinator, plan, agent = _coordinator(tmp_path, _event(
        route_state="BACKOFF", raw={"http_status": 429},
    ))

    result = _run(coordinator, plan, agent)

    assert result.chapter_status == "deferred"
    assert result.reason == "quota_exhausted"
