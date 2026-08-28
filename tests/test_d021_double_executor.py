"""§D-021：一个研究同时跑两个 Scheduler。

两条启动路径各起一套执行器，谁都不知道对方存在：

- A `OWLI_AUTO_CONFIRM=1` 时 `runtime.prepare_research` 自己 `approve` 并起跑；
- B 几秒后驱动/用户照常 `POST /plan/approve`，路由再起一次。

从前 `start_research` 里 `self._schedulers[rid] = scheduler` 直接覆盖，A 被挤掉
但没人注销、继续在跑——每章跑两遍（账单与 `attempts` 翻倍），且两套 `_card_sequence`
各自从 `card-1` 编号，回复时 `scheduler_for()` 拿到的是 B，于是 `未知卡片：card-1`。

修法：**一个研究只能有一套执行器**。起跑权同步认领（`_claim_execution`），
显式批准发现已经起跑就幂等返回已批准的计划 + 200，不起第二套。
判据用 `scheduler_for()` 而不是 `plan.status`——两条路都会把 status 写成 approved。
"""

from __future__ import annotations

import asyncio
import copy
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator

import httpx

from plan_factory import make_goal, make_plan_dict

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "app" / "store" / "schema.sql"


def async_test(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return wrapper


def _plan():
    from app.plan.model import Plan

    source = make_plan_dict()
    source["goals"] = [make_goal(1)]
    source["baseline"] = None
    return Plan.from_dict(source)


class SlowEvents:
    """publish 让出一次事件循环——认领与登记之间那段真实的 await 空窗。"""

    async def publish(self, research_id, payload):
        await asyncio.sleep(0)


def _coordinator(tmp_path: Path, plan):
    from app.orchestrator.runtime import RuntimeCoordinator

    coordinator = RuntimeCoordinator(
        store=SimpleNamespace(),
        event_buffer=SlowEvents(),
        researches={},
        cards={},
        runs_root=tmp_path / "runs",
        auto_confirm=True,
        routing_utc_clock=lambda: datetime(2026, 8, 28, tzinfo=timezone.utc),
    )
    coordinator.researches[plan.research_id] = coordinator._state_from_plan(plan)

    async def finalize(research_id: str) -> None:
        return None

    coordinator._finalize_if_terminal = finalize  # type: ignore[method-assign]
    return coordinator


def _fake_scheduler_factory(coordinator, *, hold: asyncio.Event | None = None,
                            fail_first: bool = False):
    """把 `_build_scheduler` 换成计数器；返回 built 列表供断言「造了几套」。"""

    built: list[Any] = []

    class FakeScheduler:
        def __init__(self) -> None:
            self.status = "running"
            self.goal_statuses = {"goal-1": "running"}
            self.agent_statuses = {}
            self.started = 0

        async def start(self) -> None:
            self.started += 1
            if fail_first and len(built) == 1:
                raise RuntimeError("起跑当场炸了")
            if hold is not None:
                await hold.wait()

    def build(plan):
        scheduler = FakeScheduler()
        built.append(scheduler)
        return scheduler

    coordinator._build_scheduler = build  # type: ignore[method-assign]
    return built


@async_test
async def test_同一研究第二次起跑不造第二套执行器(tmp_path: Path, caplog) -> None:
    plan = _plan()
    coordinator = _coordinator(tmp_path, plan)
    built = _fake_scheduler_factory(coordinator)

    await coordinator.start_research(plan)
    with caplog.at_level(logging.WARNING, logger="app.orchestrator.runtime"):
        await coordinator.start_research(plan)

    assert len(built) == 1, "第二次起跑又造了一套执行器"
    assert coordinator.scheduler_for(plan.research_id) is built[0]
    assert built[0].started == 1
    assert any("已在运行" in record.getMessage() for record in caplog.records), (
        "重复起跑必须留痕，不许静默丢弃"
    )


@async_test
async def test_两条启动路径并发起跑也只造一套(tmp_path: Path) -> None:
    """A 还卡在 publish 的 await 上、B 就进来了——认领必须同步完成才挡得住。"""

    plan = _plan()
    coordinator = _coordinator(tmp_path, plan)
    hold = asyncio.Event()
    built = _fake_scheduler_factory(coordinator, hold=hold)

    first = asyncio.create_task(coordinator.start_research(plan))
    second = asyncio.create_task(coordinator.start_research(plan))
    # 用 wait 不用 gather：gather 会替我们把异常取走，抹掉被测行为（§D-013 教训）。
    done, pending = await asyncio.wait({second}, timeout=1.0)
    assert not pending, "第二条路径应当立刻返回，而不是跟着跑起来"
    assert second.exception() is None
    hold.set()
    await asyncio.wait({first}, timeout=1.0)
    assert first.done() and first.exception() is None

    assert len(built) == 1
    assert built[0].started == 1


@async_test
async def test_起跑中途炸了要把起跑权还回去(tmp_path: Path) -> None:
    """认领不是单向锁：起跑失败后必须还能重起，否则一次异常就把研究钉死。"""

    plan = _plan()
    coordinator = _coordinator(tmp_path, plan)
    built = _fake_scheduler_factory(coordinator, fail_first=True)

    failed = False
    try:
        await coordinator.start_research(plan)
    except RuntimeError:
        failed = True
    assert failed, "第一次起跑本来就该炸"
    assert plan.research_id not in coordinator._starting

    # `_schedulers` 在炸之前已经登记过——重起要先把它清掉，模拟真实的重建入口。
    coordinator._schedulers.pop(plan.research_id, None)
    await coordinator.start_research(plan)
    assert len(built) == 2
    assert coordinator.scheduler_for(plan.research_id) is built[1]


@asynccontextmanager
async def api_client(tmp_path: Path) -> AsyncIterator[tuple[Any, httpx.AsyncClient, str]]:
    from app.api.main import create_app

    application = create_app(
        tmp_path / "owli.db",
        SCHEMA_PATH,
        enable_test_routes=True,
        engine_probe=lambda: {},
    )
    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            loaded = await client.post(
                "/api/test/fixtures/m2-d",
                json={"unanswered": False},
                headers={"X-Request-ID": "d021-fixture"},
            )
            assert loaded.status_code == 200, loaded.text
            yield application, client, loaded.json()["data"]["research_id"]


def _stub_start(runtime, *, register: bool) -> list[str]:
    """把真起跑换成记账；register=True 时照真实行为登记一套执行器。"""

    calls: list[str] = []

    async def start_research(plan) -> None:
        calls.append(plan.research_id)
        if register:
            runtime._schedulers[plan.research_id] = SimpleNamespace(
                status="running", goal_statuses={}, agent_statuses={}
            )

    runtime.start_research = start_research  # type: ignore[method-assign]
    return calls


async def _settle() -> None:
    """路由把起跑丢进后台任务，等它真跑起来再断言。"""

    for _ in range(10):
        await asyncio.sleep(0)


@async_test
async def test_已经起跑后再批准幂等返回200且不起第二套(tmp_path: Path) -> None:
    async with api_client(tmp_path) as (application, client, research_id):
        runtime = application.state.runtime
        calls = _stub_start(runtime, register=True)

        first = await client.post(
            f"/api/researches/{research_id}/plan/approve",
            headers={"X-Request-ID": "d021-approve-1"},
        )
        assert first.status_code == 200, first.text
        await _settle()
        # 换一个 X-Request-ID：驱动每轮都是新请求，不能靠请求缓存把这条路挡掉。
        second = await client.post(
            f"/api/researches/{research_id}/plan/approve",
            headers={"X-Request-ID": "d021-approve-2"},
        )
        await _settle()

    assert second.status_code == 200, second.text
    assert calls == [research_id], "显式批准又起了一套执行器"
    assert second.json()["data"] == first.json()["data"], (
        "重复批准要原样返回已批准的计划，plan_rev 不许再涨"
    )
    assert second.json()["data"]["status"] == "approved"
    assert second.json()["data"]["approved_at"]


@async_test
async def test_批准过但还没起跑时批准仍会起跑(tmp_path: Path) -> None:
    """反向：判据是 `scheduler_for()` 非空，不是 `plan.status == approved`。

    若把闸挂在计划状态上，这一条会红——计划早就 approved 了，但没有人在跑。
    """

    async with api_client(tmp_path) as (application, client, research_id):
        runtime = application.state.runtime
        calls = _stub_start(runtime, register=False)

        first = await client.post(
            f"/api/researches/{research_id}/plan/approve",
            headers={"X-Request-ID": "d021-noexec-1"},
        )
        assert first.status_code == 200, first.text
        assert first.json()["data"]["status"] == "approved"
        await _settle()

        second = await client.post(
            f"/api/researches/{research_id}/plan/approve",
            headers={"X-Request-ID": "d021-noexec-2"},
        )
        await _settle()

    assert second.status_code == 200, second.text
    assert calls == [research_id, research_id], "没有执行器在跑时，批准必须真的起跑"
