"""§D-039：timeout 型 deferred 的补轮结构上必空转。

夜跑 r-b10812f664d2 里 goal-1/ch-1（豆包·xhs）撞 fast 档 330 s 章墙钟后
`queued→running→missing` 同一秒完成——章 deadline 是从首次起跑算的**绝对**墙钟，
`_deadline_expired` 一旦置位，补轮一派活立刻被 `_cancel_running_run(timeout)`，
`_supplemented` 里的 agent 到点直接 missing。「留一次补轮」只对非墙钟原因有意义。

本文件断言的是修后语义（甲）：**timeout 不进 deferred，直接 missing**；
已落库/已落盘的产物不因此作废（missing 只表示「未完成」）。
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from tests.plan_factory import make_plan_dict


SCHEMA = Path(__file__).resolve().parents[1] / "app" / "store" / "schema.sql"


def _store(tmp_path: Path):
    from app.store.dao import Store

    database = tmp_path / "owli.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(SCHEMA.read_text(encoding="utf-8"))
    store = Store(database)
    store.create_report(
        id="r-d039", title="D-039", research_question="测试",
        created_at="2026-09-04T00:00:00Z",
    )
    return store


def _plan(deadline: int = 10, *, max_attempts: int = 3, max_rounds: int = 2):
    from app.plan.model import Plan

    source = make_plan_dict()
    source["research_id"] = "r-d039"
    source["scale"] = "fast"
    source["baseline"] = None
    source["goals"] = source["goals"][:1]
    source["goals"][0]["retry_policy"].update(
        max_attempts_per_round=max_attempts,
        max_rounds=max_rounds,
        ask_engine_switch_at=max_attempts,
        chapter_deadline_seconds=deadline,
    )
    return Plan.from_dict(source)


def _statuses(events: list[dict], agent_id: str) -> list[str]:
    return [
        event["data"]["status"] for event in events
        if event["type"] == "agent_update" and event["data"]["agent_id"] == agent_id
    ]


def test_墙钟定时器取消的章不进deferred_补轮不再同秒空转(tmp_path: Path) -> None:
    """夜跑原形态：定时器在派活途中触发 → 取消 → 终态。

    修前：deferred → `_drive` 补轮把它置回 queued → 再派活时 `_deadline_expired`
    已置位，一行代码没跑就 missing（同一秒），补轮纯空转。
    修后：一次性落 missing/timeout，状态里根本不出现 deferred。
    """
    from app.orchestrator.scheduler import Scheduler, TaskRunResult

    store = _store(tmp_path)
    plan = _plan()
    now = datetime(2026, 9, 4, tzinfo=timezone.utc)
    callbacks: list[tuple[float, object]] = []
    events: list[dict] = []
    calls: list[int] = []

    async def run_task(agent, context):
        calls.append(context.attempt)
        # 只触发章墙钟那一个定时器（goal 级的另有 delay，别误触）
        for delay, callback in list(callbacks):
            if delay == 10:
                callback()  # 墙钟到点：定时器在这次派活跑到一半时触发
        callbacks.clear()
        await asyncio.sleep(3600)  # 等着被 _cancel_running_run 掐掉
        return TaskRunResult(True, context.engine)

    scheduler = Scheduler(
        plan, run_task, lambda event: events.append(event),
        lambda: now,
        lambda delay, callback: callbacks.append((delay, callback)),
        chapter_ledger=store,
    )
    asyncio.run(scheduler.start())

    agent_id = plan.goals[0].agents[0].agent_id
    row = store.list_chapters("r-d039")[0]
    assert calls == [1], "补轮不该再派一次注定被取消的活"
    assert row["status"] == "missing"
    assert row["reason"] == "timeout"
    assert "deferred" not in _statuses(events, agent_id)
    assert agent_id not in scheduler._supplemented


def _run_until_deadline(tmp_path: Path, results: list):
    """无定时器形态：假时钟里派活自己把时间推过墙钟，按返回后的 elapsed 判超时。"""
    from app.orchestrator.scheduler import Scheduler

    store = _store(tmp_path)
    plan = _plan()
    current = [datetime(2026, 9, 4, tzinfo=timezone.utc)]
    events: list[dict] = []
    calls: list[int] = []

    async def run_task(agent, context):
        calls.append(context.attempt)
        current[0] = current[0].replace(second=current[0].second + 11)
        return results[min(len(calls), len(results)) - 1](context)

    scheduler = Scheduler(
        plan, run_task, lambda event: events.append(event),
        lambda: current[0],
        lambda delay, callback: None,
        chapter_ledger=store,
    )
    asyncio.run(scheduler.start())
    return store.list_chapters("r-d039")[0], calls, events, plan


def test_墙钟到点的章不再烧一次注定超时的补轮(tmp_path: Path) -> None:
    """修前 calls == [1, 2]：补轮那次派活的 deadline_at 早已过期，纯烧钱。"""
    from app.orchestrator.scheduler import TaskRunResult

    row, calls, events, plan = _run_until_deadline(
        tmp_path,
        [lambda context: TaskRunResult(False, context.engine, reason="socket_closed")],
    )
    assert calls == [1], "补轮那次派活从起跑就已过墙钟，不该派"
    assert row["status"] == "missing"
    assert row["reason"] == "timeout"
    assert row["attempts"] == 1
    assert "deferred" not in _statuses(events, plan.goals[0].agents[0].agent_id)


def test_timeout落missing时已落库产物不作废(tmp_path: Path) -> None:
    """missing 只表示「这一章没跑完」，不表示这一轮的产出无效。

    夜跑 xhs 采集章 294 行在取消时刻之前就已入库并被评级章消费；账本里
    这一章的 actual_output_path / actual_count 同样不该被超时判定清空。
    """
    from app.orchestrator.scheduler import TaskRunResult

    row, _calls, _events, _plan = _run_until_deadline(
        tmp_path,
        [lambda context: TaskRunResult(
            False, context.engine, reason="socket_closed",
            actual_output_path="goals/goal-1/ch-1.json", actual_count=294,
        )],
    )
    assert row["status"] == "missing"
    assert row["reason"] == "timeout"
    assert row["actual_output_path"] == "goals/goal-1/ch-1.json"
    assert row["actual_count"] == 294


def test_总墙钟不变量_章只挂一个定时器且不因超时加时(tmp_path: Path) -> None:
    """不变量：一章只有一份墙钟预算，补轮不另给新墙钟（[[no-extra-time-in-verification]]）。"""
    from app.orchestrator.scheduler import Scheduler, TaskRunResult

    store = _store(tmp_path)
    plan = _plan()
    now = datetime(2026, 9, 4, tzinfo=timezone.utc)
    armed: list[float] = []
    fired: list[object] = []

    async def run_task(agent, context):
        for delay, callback in list(armed_pairs):
            if delay == 10 and callback not in fired:
                fired.append(callback)
                callback()
        await asyncio.sleep(3600)
        return TaskRunResult(True, context.engine)

    armed_pairs: list[tuple[float, object]] = []

    def timer(delay, callback):
        armed_pairs.append((delay, callback))
        armed.append(delay)

    scheduler = Scheduler(
        plan, run_task, lambda event: None, lambda: now, timer,
        chapter_ledger=store,
    )
    asyncio.run(scheduler.start())

    assert [delay for delay in armed if delay == 10] == [10], "章墙钟只准挂一次"
    assert store.list_chapters("r-d039")[0]["status"] == "missing"


def test_stop取消路径不被timeout语义串到_resume后仍能跑成done(tmp_path: Path) -> None:
    """货 3：墙钟与 /stop 共用 `_cancel_running_run`（D-008），改的只是 timeout 一支。

    章墙钟定时器已挂上、但先被 /stop 掐掉时，cancel_reason 是 stopped，章复位
    pending（不是 missing/timeout），resume 后照常再派活跑成 done。
    """
    from datetime import timedelta

    from app.orchestrator.scheduler import Scheduler, TaskRunResult

    store = _store(tmp_path)
    plan = _plan(deadline=600)
    agent_id = plan.goals[0].agents[0].agent_id
    current = [datetime(2026, 9, 4, tzinfo=timezone.utc)]
    attempts: list[int] = []
    started = asyncio.Event()

    async def run_task(agent, context):
        attempts.append(context.attempt)
        current[0] = current[0] + timedelta(seconds=1)
        if len(attempts) == 1:
            started.set()
            await asyncio.sleep(3600)  # 等 /stop 掐
        return TaskRunResult(
            True, context.engine,
            actual_output_path=str(agent.output["path"]), actual_count=1,
        )

    async def scenario():
        scheduler = Scheduler(
            plan, run_task, lambda event: None, lambda: current[0],
            lambda delay, callback: None, chapter_ledger=store,
        )
        driving = asyncio.create_task(scheduler.start())
        await started.wait()
        await scheduler.stop()
        await driving
        assert store.list_chapters("r-d039")[0]["status"] == "pending"
        assert scheduler.agent_statuses[agent_id] == "queued"
        await scheduler.resume()
        return scheduler

    scheduler = asyncio.run(scenario())
    row = store.list_chapters("r-d039")[0]
    assert attempts == [1, 2]
    assert row["status"] == "done"
    assert row["reason"] is None


def test_配额型deferred仍留一次补轮(tmp_path: Path) -> None:
    """货 3 反向守卫：D-039 只收窄 timeout 一支，非墙钟原因的补轮一次不少。

    与 test_配额章_deferred_后仅补采一轮_仍不成则_missing 的差别：那条只看终态，
    这条盯**状态轨迹**里 deferred 还在不在——甲改错成「一律不进 deferred」时，
    只看终态的用例照样绿。
    """
    from datetime import timedelta

    from app.orchestrator.scheduler import Scheduler, TaskRunResult

    store = _store(tmp_path)
    plan = _plan(deadline=600)
    current = [datetime(2026, 9, 4, tzinfo=timezone.utc)]
    events: list[dict] = []
    calls: list[int] = []

    def timer(delay, callback):
        if delay <= 15:  # 章级重试间隔：假时钟里立刻到点
            current[0] += timedelta(seconds=delay)
            callback()
        return object()

    async def run_task(agent, context):
        calls.append(context.attempt)
        return TaskRunResult(
            False, context.engine,
            chapter_status="deferred", reason="quota_exhausted", actual_count=0,
        )

    scheduler = Scheduler(
        plan, run_task, lambda event: events.append(event), lambda: current[0],
        timer, chapter_ledger=store,
    )
    asyncio.run(scheduler.start())

    row = store.list_chapters("r-d039")[0]
    assert calls == [1, 2], "配额章必须真补采一轮"
    assert "deferred" in _statuses(events, plan.goals[0].agents[0].agent_id)
    assert row["status"] == "missing"
    assert row["reason"] == "quota_exhausted"
