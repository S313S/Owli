"""§D-013：阶段卡回复竞态 + 后台异常被吞。

两货：
- 货 1 `scheduler.answer_card` 入口做原子 compare-and-set——重复回复要么幂等成功、
  要么明确 409，不许把 `RuntimeError("卡片已处理")` 抛进后台任务让 goal 死等。
- 货 2 `runtime._track_auto_task` 补异常回调——后台任务的异常必须进事件流或日志，
  不许再出现 `Task exception was never retrieved`。

真实竞态形态（§W-1 第 4/6 轮实测）：`runtime.respond_card` 检查的是 runtime 自己
那份 **Card 副本**（`_emit_scheduler_event` 里 `_external_card` 重建的），
scheduler 才是权威。scheduler 已置 ANSWERED、card_update 事件还没回流到 runtime 副本时，
第二个调用照样通过 runtime 的 PENDING 检查，进到 scheduler 才炸。
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from plan_factory import make_goal, make_plan_dict


def async_test(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return wrapper


class FakeClockTimer:
    def __init__(self) -> None:
        self.now = datetime(2026, 8, 28, tzinfo=timezone.utc)
        self.jobs: list[tuple[datetime, Any]] = []

    def clock(self) -> datetime:
        return self.now

    def timer(self, delay_seconds: float, callback: Any) -> None:
        self.jobs.append((self.now + timedelta(seconds=delay_seconds), callback))


def plan_with_goals(*goals: dict[str, Any]):
    from app.plan.model import Plan

    source = make_plan_dict()
    source["goals"] = list(goals)
    source["baseline"] = None
    return Plan.from_dict(source)


def goal(number: int, *, depends_on=()):
    result = make_goal(number)
    result["depends_on"] = list(depends_on)
    result["on_upstream_failure"] = "skip"
    return result


def card_events(events: list[dict[str, Any]], card_type: str):
    return [
        event["data"]["card"]
        for event in events
        if event.get("type") == "card_update"
        and event["data"]["card"]["card_type"] == card_type
    ]


async def _scheduler_stopped_at_intervention():
    """起一个两 goal 的计划，跑到 goal-1 的阶段卡挂住。"""
    from app.orchestrator.scheduler import Scheduler, TaskRunResult

    calls: list[str] = []
    events: list[dict[str, Any]] = []
    fake_time = FakeClockTimer()

    async def run_task(agent, context):
        calls.append(context.goal_id)
        return TaskRunResult(succeeded=True, engine=context.engine)

    scheduler = Scheduler(
        plan_with_goals(goal(1), goal(2, depends_on=("goal-1",))),
        run_task,
        events.append,
        fake_time.clock,
        fake_time.timer,
    )
    await scheduler.start()
    return scheduler, events, calls


# --------------------------------------------------------------------------
# 货 1：answer_card 原子 compare-and-set
# --------------------------------------------------------------------------


@async_test
async def test_并发两次回复同一卡片不抛异常且goal正常推进():
    """本包的正脸判据：两个调用同秒点同一张卡，goal 照常放行，谁都不炸。"""
    scheduler, events, calls = await _scheduler_stopped_at_intervention()
    card = card_events(events, "INTERVENE")[0]
    assert "goal-2" not in calls

    outcomes = await asyncio.gather(
        scheduler.answer_card(card["card_id"], {"choice": "continue"}),
        scheduler.answer_card(card["card_id"], {"choice": "continue"}),
        return_exceptions=True,
    )

    assert [type(item) for item in outcomes if isinstance(item, BaseException)] == []
    assert calls[-1] == "goal-2", "goal 必须被放行，不许死等"


@async_test
async def test_重复回复只解析一次不重复触发下游():
    """幂等不是「再跑一遍」：副作用只许发生一次。"""
    scheduler, events, calls = await _scheduler_stopped_at_intervention()
    card = card_events(events, "INTERVENE")[0]

    await asyncio.gather(
        scheduler.answer_card(card["card_id"], {"choice": "continue"}),
        scheduler.answer_card(card["card_id"], {"choice": "continue"}),
        scheduler.answer_card(card["card_id"], {"choice": "continue"}),
    )

    assert calls.count("goal-2") == 1
    answered = [
        item
        for item in card_events(events, "INTERVENE")
        if item["card_id"] == card["card_id"] and item["status"] == "answered"
    ]
    assert len(answered) == 1, "只许发一次 answered 的 card_update"


@async_test
async def test_串行重复回复也幂等成功不抛卡片已处理():
    """驱动兜底代点撞上自动确认，走的就是这条串行路径。"""
    scheduler, events, _ = await _scheduler_stopped_at_intervention()
    card = card_events(events, "INTERVENE")[0]

    await scheduler.answer_card(card["card_id"], {"choice": "continue"})
    await scheduler.answer_card(card["card_id"], {"choice": "continue"})


@async_test
async def test_未知卡片仍然报错不静默吞掉():
    """幂等只覆盖「这张卡已经答过」，不认识的卡片 id 必须继续报错。"""
    scheduler, _events, _ = await _scheduler_stopped_at_intervention()

    raised = None
    try:
        await scheduler.answer_card("card-不存在", {"choice": "continue"})
    except ValueError as error:
        raised = error
    assert raised is not None and "未知卡片" in str(raised)


@async_test
async def test_R8卡超时默认与人工回复相撞只解析一次():
    """另一条同形竞态：`_expire_r8` 的定时默认 vs 人点「接受计费」。"""
    from app.orchestrator.scheduler import Scheduler, TaskRunResult

    events: list[dict[str, Any]] = []
    fake_time = FakeClockTimer()

    async def run_task(agent, context):
        return TaskRunResult(succeeded=True, engine=context.engine)

    scheduler = Scheduler(
        plan_with_goals(goal(1)),
        run_task,
        events.append,
        fake_time.clock,
        fake_time.timer,
    )
    await scheduler._begin_r8_confirmation()
    card = card_events(events, "EXTRA_QUOTA_CONFIRM")[0]

    outcomes = await asyncio.gather(
        scheduler.answer_card(card["card_id"], {"choice": "接受计费继续"}),
        scheduler._expire_r8(card["card_id"]),
        return_exceptions=True,
    )
    assert [item for item in outcomes if isinstance(item, BaseException)] == [], outcomes

    resolved = [
        item
        for item in card_events(events, "EXTRA_QUOTA_CONFIRM")
        if item["card_id"] == card["card_id"] and item["status"] != "pending"
    ]
    assert len(resolved) == 1, resolved
    assert resolved[0]["status"] == "answered", "先到的是人工回复，超时默认不许覆盖它"


@async_test
async def test_已过期默认的卡片再被回复也幂等不抛():
    """R8 卡超时默认切引擎后，人再点一下不该炸。"""
    from app.orchestrator.scheduler import Scheduler, TaskRunResult
    from app.plan.cards import CardStatus

    events: list[dict[str, Any]] = []
    fake_time = FakeClockTimer()

    async def run_task(agent, context):
        return TaskRunResult(succeeded=True, engine=context.engine)

    scheduler = Scheduler(
        plan_with_goals(goal(1)),
        run_task,
        events.append,
        fake_time.clock,
        fake_time.timer,
    )
    await scheduler.start()
    intervene = card_events(events, "INTERVENE")[0]
    await scheduler.answer_card(intervene["card_id"], {"choice": "continue"})

    entry = scheduler._cards[intervene["card_id"]]
    entry["card"].status = CardStatus.EXPIRED_DEFAULTED
    await scheduler.answer_card(intervene["card_id"], {"choice": "continue"})


# --------------------------------------------------------------------------
# 货 2：后台任务异常必须可见
# --------------------------------------------------------------------------


def _runtime():
    from app.orchestrator.runtime import RuntimeCoordinator

    return RuntimeCoordinator.__new__(RuntimeCoordinator)


@async_test
async def test_后台任务异常进日志不再无声消失(caplog=None):
    """§W-1 第 6 轮的现成样本：`ValueError: 未知卡片：card-1` 被吞在 create_task 里。"""
    from app.orchestrator import runtime as runtime_module

    records: list[logging.LogRecord] = []

    class Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = Collector(level=logging.ERROR)
    logger = logging.getLogger(runtime_module.__name__)
    logger.addHandler(handler)
    previous = logger.level
    logger.setLevel(logging.ERROR)
    try:
        instance = _runtime()
        instance._auto_tasks = set()

        async def boom() -> None:
            raise ValueError("未知卡片：card-1")

        task = asyncio.create_task(boom())
        instance._track_auto_task(task)
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)

    messages = [record.getMessage() for record in records]
    assert any("未知卡片：card-1" in message for message in messages), messages
    assert any(record.exc_info for record in records), "要带堆栈，不能只留一行字"


@async_test
async def test_后台任务取消不算异常不刷错误日志():
    from app.orchestrator import runtime as runtime_module

    records: list[logging.LogRecord] = []

    class Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = Collector(level=logging.ERROR)
    logger = logging.getLogger(runtime_module.__name__)
    logger.addHandler(handler)
    try:
        instance = _runtime()
        instance._auto_tasks = set()

        async def sleeper() -> None:
            await asyncio.sleep(60)

        task = asyncio.create_task(sleeper())
        instance._track_auto_task(task)
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)
    finally:
        logger.removeHandler(handler)

    assert records == []


@async_test
async def test_后台任务成功时不记错误日志():
    from app.orchestrator import runtime as runtime_module

    records: list[logging.LogRecord] = []

    class Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = Collector(level=logging.ERROR)
    logger = logging.getLogger(runtime_module.__name__)
    logger.addHandler(handler)
    try:
        instance = _runtime()
        instance._auto_tasks = set()

        async def fine() -> None:
            return None

        task = asyncio.create_task(fine())
        instance._track_auto_task(task)
        await asyncio.gather(task)
        await asyncio.sleep(0)
    finally:
        logger.removeHandler(handler)

    assert records == []


@async_test
async def test_后台任务不再留下未取回的异常():
    """`Task exception was never retrieved` 的直接判据：异常已被取走。"""
    instance = _runtime()
    instance._auto_tasks = set()

    async def boom() -> None:
        raise RuntimeError("后台炸了")

    task = asyncio.create_task(boom())
    instance._track_auto_task(task)
    # 不用 gather：gather 会替我们把异常取走，那样量的就是尺子不是被测代码
    await asyncio.wait([task])
    await asyncio.sleep(0)

    # done callback 必须自己取过一次，否则解释器退出时才会补打那句话
    assert getattr(task, "_log_traceback", True) is False
    assert task.exception() is not None


# --------------------------------------------------------------------------
# 生产形态：runtime 那份 Card 副本是陈旧的，PENDING 检查根本拦不住第二个调用
# --------------------------------------------------------------------------


def _runtime_with_real_scheduler(tmp_path, monkeypatch, *, auto_confirm: bool):
    """按 `_build_scheduler` 的接线搭一套真 runtime + 真 scheduler。"""
    from types import SimpleNamespace

    from app.orchestrator import runtime as runtime_module
    from app.orchestrator.runtime import RuntimeCoordinator
    from app.orchestrator.scheduler import Scheduler, TaskRunResult
    from app.plan.model import Plan
    from tests.test_m3h_ledger import _store

    source = make_plan_dict()
    source["research_id"] = "r-d013"
    source["baseline"] = None
    source["goals"] = [goal(1), goal(2, depends_on=("goal-1",))]
    plan = Plan.from_dict(source)

    published: list[dict[str, Any]] = []

    async def publish(research_id, payload):
        published.append(dict(payload))

    monkeypatch.setattr(runtime_module, "load_plan", lambda store_, rid: plan)
    coordinator = RuntimeCoordinator(
        store=_store(tmp_path),
        event_buffer=SimpleNamespace(publish=publish),
        researches={},
        cards={},
        runs_root=tmp_path / "runs",
        auto_confirm=auto_confirm,
        routing_utc_clock=lambda: datetime(2026, 8, 28, tzinfo=timezone.utc),
    )
    coordinator.researches[plan.research_id] = coordinator._state_from_plan(plan)
    coordinator.researches[plan.research_id]["status"] = "running"

    calls: list[str] = []
    fake_time = FakeClockTimer()

    async def run_task(agent, context):
        calls.append(context.goal_id)
        return TaskRunResult(succeeded=True, engine=context.engine)

    scheduler = Scheduler(
        plan,
        run_task,
        lambda event: coordinator._emit_scheduler_event(plan.research_id, event),
        fake_time.clock,
        fake_time.timer,
    )
    coordinator._schedulers[plan.research_id] = scheduler
    return coordinator, scheduler, plan, calls, published


@async_test
async def test_runtime副本陈旧时两路回复同一卡片仍然放行goal(tmp_path=None, monkeypatch=None):
    """§W-1 第 4 轮的真身：runtime 检查的是副本，scheduler 才是权威。"""
    import pytest
    from _pytest.monkeypatch import MonkeyPatch

    patcher = MonkeyPatch()
    root = Path(__file__).resolve().parent / ".d013-tmp"
    root.mkdir(parents=True, exist_ok=True)
    try:
        coordinator, scheduler, plan, calls, _published = _runtime_with_real_scheduler(
            root, patcher, auto_confirm=False,
        )
        await scheduler.start()
        card_id = next(
            item
            for item, card in coordinator.cards.items()
            if card.card_type.value == "INTERVENE"
        )
        assert coordinator.cards[card_id].status.value == "pending"
        assert "goal-2" not in calls

        outcomes = await asyncio.gather(
            coordinator.respond_card(card_id, action="continue", payload={"choice": "continue"}),
            coordinator.respond_card(card_id, action="continue", payload={"choice": "continue"}),
            return_exceptions=True,
        )

        failures = [item for item in outcomes if isinstance(item, BaseException)]
        assert failures == [], failures
        assert calls[-1] == "goal-2", "goal 必须被放行，不许死等"
    finally:
        patcher.undo()
        import shutil

        shutil.rmtree(root, ignore_errors=True)


@async_test
async def test_自动确认与驱动同秒点卡不再把异常吞进后台任务():
    """OWLI_AUTO_CONFIRM 自动点 + 驱动兜底代点，就是整跑里那两路。"""
    from _pytest.monkeypatch import MonkeyPatch

    from app.orchestrator import runtime as runtime_module

    patcher = MonkeyPatch()
    root = Path(__file__).resolve().parent / ".d013-tmp-auto"
    root.mkdir(parents=True, exist_ok=True)

    records: list[logging.LogRecord] = []

    class Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = Collector(level=logging.ERROR)
    runtime_logger = logging.getLogger(runtime_module.__name__)
    runtime_logger.addHandler(handler)
    previous_level = runtime_logger.level
    runtime_logger.setLevel(logging.ERROR)
    try:
        coordinator, scheduler, plan, calls, published = _runtime_with_real_scheduler(
            root, patcher, auto_confirm=True,
        )
        # 自动确认走 `_emit_scheduler_event` 的 create_task 那条路；
        # 后台任务 done 就从 `_auto_tasks` 里被摘掉，所以自己留一份底
        spawned: list[asyncio.Task[Any]] = []
        original_track = coordinator._track_auto_task

        def track(task):
            spawned.append(task)
            return original_track(task)

        coordinator._track_auto_task = track  # type: ignore[method-assign]
        await scheduler.start()
        card_id = next(
            item
            for item, card in coordinator.cards.items()
            if card.card_type.value == "INTERVENE"
        )
        # 驱动兜底：不看 grace、不看 running 章，直接点（= 去掉 D-013 规避）
        outcome = None
        try:
            await coordinator.respond_card(
                card_id, action="continue", payload={"choice": "continue"},
            )
        except BaseException as error:  # noqa: BLE001 - 判据就是它不该发生
            outcome = error
        await coordinator._drain_auto_tasks()

        assert outcome is None, outcome
        assert calls.count("goal-2") == 1, calls
        assert spawned, "自动确认没起后台任务，这条用例就没量到东西"
        swallowed = [
            task.exception() for task in spawned
            if task.done() and not task.cancelled() and task.exception() is not None
        ]
        # 货 1：卡片竞态那一类，一条都不许再有
        assert [item for item in swallowed if "卡片" in str(item)] == [], swallowed
        # 货 2：夹具没铺报告行，收尾会抛 KeyError——那是夹具噪声，不是本包的货，
        # 但它必须**被看见**：剩下的后台异常一条不落地进错误日志。
        logged = " | ".join(record.getMessage() for record in records)
        for item in swallowed:
            assert str(item) in logged, (item, logged)
    finally:
        runtime_logger.removeHandler(handler)
        runtime_logger.setLevel(previous_level)
        patcher.undo()
        import shutil

        shutil.rmtree(root, ignore_errors=True)


# --------------------------------------------------------------------------
# 顺带：规划期 POST /pause 返回 500（§W-1 登记未立项）
# --------------------------------------------------------------------------


@async_test
async def test_规划期暂停给409说清现在能做什么而不是500():
    """500 是句假话——没崩，只是还没到能暂停的阶段。"""
    from tests.test_m2_wiring import api_client

    root = Path(__file__).resolve().parent / ".d013-tmp-pause"
    root.mkdir(parents=True, exist_ok=True)
    try:
        async with api_client(root) as (_app, client, _engine):
            created = await client.post(
                "/api/researches",
                json={"query": "飞书竞品优缺点"},
                headers={"X-Request-ID": "d013-create-pause"},
            )
            research_id = created.json()["data"]["research_id"]
            # 计划还没批准，scheduler 不存在——这就是规划期
            snapshot = await client.get(f"/api/researches/{research_id}")
            assert snapshot.json()["data"]["status"] != "running"

            paused = await client.post(
                f"/api/researches/{research_id}/pause",
                headers={"X-Request-ID": "d013-pause-planning"},
            )
            stopped = await client.post(
                f"/api/researches/{research_id}/stop",
                headers={"X-Request-ID": "d013-stop-planning"},
            )

        assert paused.status_code == 409, paused.text
        assert "还不能暂停" in paused.text
        assert stopped.status_code == 409, stopped.text
        assert "还不能终止" in stopped.text
    finally:
        import shutil

        shutil.rmtree(root, ignore_errors=True)


@async_test
async def test_暂停不存在的研究给404不是500():
    from tests.test_m2_wiring import api_client

    root = Path(__file__).resolve().parent / ".d013-tmp-404"
    root.mkdir(parents=True, exist_ok=True)
    try:
        async with api_client(root) as (_app, client, _engine):
            paused = await client.post(
                "/api/researches/r-根本不存在/pause",
                headers={"X-Request-ID": "d013-pause-404"},
            )
        assert paused.status_code == 404, paused.text
    finally:
        import shutil

        shutil.rmtree(root, ignore_errors=True)
