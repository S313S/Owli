from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.plan_factory import make_plan_dict


SCHEMA = Path(__file__).resolve().parents[1] / "app" / "store" / "schema.sql"


def _store(tmp_path):
    from app.store.dao import Store

    database = tmp_path / "owli.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(SCHEMA.read_text(encoding="utf-8"))
    store = Store(database)
    store.create_report(
        id="r-ledger", title="章节账本", research_question="测试",
        created_at="2026-08-22T00:00:00Z",
    )
    return store


def test_章节账本表与固定接口记录实际结尾(tmp_path):
    store = _store(tmp_path)
    store.ensure_chapters("r-ledger", [{
        "goal_id": "goal-1", "chapter_id": "ch-1",
    }], updated_at="2026-08-22T00:00:00Z")

    assert store.start_chapter(
        "r-ledger", "goal-1", "ch-1", engine="codex",
        updated_at="2026-08-22T00:01:00Z",
    ) is True
    store.finish_chapter(
        "r-ledger", "goal-1", "ch-1", status="done", reason=None,
        actual_output_path="goals/goal-1/data.json", actual_count=3,
        updated_at="2026-08-22T00:02:00Z",
    )

    row = store.list_chapters("r-ledger")[0]
    assert row == {
        "research_id": "r-ledger", "goal_id": "goal-1", "chapter_id": "ch-1",
        "status": "done", "attempts": 1, "engine": "codex", "reason": None,
        "engine_error": None, "conclusion_error": None,
        "actual_output_path": "goals/goal-1/data.json", "actual_count": 3,
        "extra": {},
        "updated_at": "2026-08-22T00:02:00Z",
    }
    assert store.start_chapter(
        "r-ledger", "goal-1", "ch-1", engine="claude",
        updated_at="2026-08-22T00:03:00Z",
    ) is False


def test_章节账本保留引擎与结论错误(tmp_path):
    store = _store(tmp_path)
    store.ensure_chapters("r-ledger", [{"goal_id": "goal-1", "chapter_id": "ch-1"}],
                          updated_at="2026-08-22T00:00:00Z")
    store.finish_chapter(
        "r-ledger", "goal-1", "ch-1", status="missing", reason="retry_exhausted",
        actual_output_path=None, actual_count=0,
        engine_error="socket closed", conclusion_error="result missing",
        updated_at="2026-08-22T00:01:00Z",
    )
    row = store.list_chapters("r-ledger")[0]
    assert row["engine_error"] == "socket closed"
    assert row["conclusion_error"] == "result missing"


def test_v3章节账本迁移后增加错误原文字段(tmp_path):
    from app.store.schema import initialize_database_if_empty

    database = tmp_path / "owli-v3.db"
    with sqlite3.connect(database) as connection:
        connection.executescript("""
        CREATE TABLE reports (id TEXT PRIMARY KEY) STRICT;
        CREATE TABLE chapter_progress (
          research_id TEXT NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
          goal_id TEXT NOT NULL,
          chapter_id TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending',
          attempts INTEGER NOT NULL DEFAULT 0,
          engine TEXT,
          reason TEXT,
          actual_output_path TEXT,
          actual_count INTEGER,
          updated_at TEXT NOT NULL,
          PRIMARY KEY (research_id, goal_id, chapter_id)
        ) STRICT;
        PRAGMA user_version = 3;
        """)

    initialize_database_if_empty(database, SCHEMA)

    with sqlite3.connect(database) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        columns = {
            row[1] for row in connection.execute("PRAGMA table_xinfo(chapter_progress)")
        }
    assert version == 8
    assert {"engine_error", "conclusion_error"} <= columns


@pytest.mark.parametrize("reason", [
    "empty_result", "tool_unavailable", "quota_exhausted", "retry_exhausted",
    "conclusion_invalid", "timeout",
])
def test_missing_reason_闭集(reason, tmp_path):
    store = _store(tmp_path)
    store.ensure_chapters("r-ledger", [{"goal_id": "goal-1", "chapter_id": "ch-1"}],
                          updated_at="2026-08-22T00:00:00Z")
    store.finish_chapter(
        "r-ledger", "goal-1", "ch-1", status="missing", reason=reason,
        actual_output_path=None, actual_count=0,
        updated_at="2026-08-22T00:01:00Z",
    )
    assert store.list_chapters("r-ledger")[0]["reason"] == reason


def test_v4章节账本迁移后接受结论无效原因(tmp_path):
    from app.store.dao import Store
    from app.store.schema import initialize_database_if_empty

    database = tmp_path / "owli-v4.db"
    with sqlite3.connect(database) as connection:
        connection.executescript("""
        CREATE TABLE reports (id TEXT PRIMARY KEY) STRICT;
        CREATE TABLE chapter_progress (
          research_id TEXT NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
          goal_id TEXT NOT NULL,
          chapter_id TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending','running','done','missing','deferred')),
          attempts INTEGER NOT NULL DEFAULT 0,
          engine TEXT,
          reason TEXT CHECK (reason IN (
            'empty_result','tool_unavailable','quota_exhausted','retry_exhausted'
          ) OR reason IS NULL),
          engine_error TEXT,
          conclusion_error TEXT,
          actual_output_path TEXT,
          actual_count INTEGER,
          updated_at TEXT NOT NULL,
          PRIMARY KEY (research_id, goal_id, chapter_id)
        ) STRICT;
        PRAGMA user_version = 4;
        """)

    initialize_database_if_empty(database, SCHEMA)
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 8
        connection.execute("INSERT INTO reports (id) VALUES ('r-ledger')")
    store = Store(database)
    store.ensure_chapters(
        "r-ledger", [{"goal_id": "goal-1", "chapter_id": "ch-1"}],
        updated_at="2026-08-22T00:00:00Z",
    )
    store.finish_chapter(
        "r-ledger", "goal-1", "ch-1",
        status="missing", reason="conclusion_invalid",
        actual_output_path=None, actual_count=0,
        updated_at="2026-08-22T00:01:00Z",
    )
    assert store.list_chapters("r-ledger")[0]["reason"] == "conclusion_invalid"


def test_账本只返回_pending_deferred_供重跑(tmp_path):
    store = _store(tmp_path)
    chapters = [
        {"goal_id": "goal-1", "chapter_id": "ch-1"},
        {"goal_id": "goal-1", "chapter_id": "ch-2"},
        {"goal_id": "goal-1", "chapter_id": "ch-3"},
        {"goal_id": "goal-1", "chapter_id": "ch-4"},
    ]
    store.ensure_chapters("r-ledger", chapters, updated_at="2026-08-22T00:00:00Z")
    for chapter_id, status, reason in (
        ("ch-1", "done", None),
        ("ch-2", "missing", "empty_result"),
        ("ch-3", "deferred", "quota_exhausted"),
    ):
        store.finish_chapter(
            "r-ledger", "goal-1", chapter_id, status=status, reason=reason,
            actual_output_path=None, actual_count=0,
            updated_at="2026-08-22T00:01:00Z",
        )

    assert store.runnable_chapter_keys("r-ledger") == {
        ("goal-1", "ch-3"), ("goal-1", "ch-4"),
    }


def test_scheduler_跳过_done_章并把重试耗尽写成_missing后仍_completed(tmp_path):
    from app.orchestrator.scheduler import Scheduler, TaskRunResult
    from app.plan.model import Plan

    store = _store(tmp_path)
    source = make_plan_dict()
    source["research_id"] = "r-ledger"
    source["goals"] = source["goals"][:1]
    source["baseline"] = None
    first = source["goals"][0]["agents"][0]
    first["chapter"]["chapter_id"] = "ch-1"
    second = {**first, "agent_id": "agent-2", "depends_on": []}
    second["chapter"] = {
        **first["chapter"], "chapter_id": "ch-2",
        "plan_path": "goals/goal-1/ch-2.md",
        "closing": {**first["chapter"]["closing"],
                    "output": {"path": "goals/goal-1/agent-2.md"}},
    }
    second["output"] = {**first["output"], "path": "goals/goal-1/agent-2.md"}
    source["goals"][0]["agents"] = [first, second]
    source["goals"][0]["retry_policy"].update(
        max_attempts_per_round=1, max_rounds=1, ask_engine_switch_at=1,
    )
    plan = Plan.from_dict(source)
    store.ensure_chapters("r-ledger", [
        {"goal_id": "goal-1", "chapter_id": "ch-1"},
        {"goal_id": "goal-1", "chapter_id": "ch-2"},
    ], updated_at="2026-08-22T00:00:00Z")
    store.finish_chapter(
        "r-ledger", "goal-1", "ch-1", status="done", reason=None,
        actual_output_path=first["output"]["path"], actual_count=1,
        updated_at="2026-08-22T00:00:30Z",
    )
    calls = []
    events = []

    async def run_task(agent, context):
        calls.append(agent.agent_id)
        return TaskRunResult(False, context.engine)

    async def scenario():
        scheduler = Scheduler(
            plan, run_task, events.append,
            lambda: datetime(2026, 8, 22, tzinfo=timezone.utc),
            lambda delay, callback: None,
            chapter_ledger=store,
        )
        await scheduler.start()
        card = next(
            item["data"]["card"] for item in events
            if item["type"] == "card_update"
            and item["data"]["card"]["card_type"] == "INTERVENE"
        )
        await scheduler.answer_card(card["card_id"], {"choice": "continue"})
        return scheduler

    scheduler = asyncio.run(scenario())
    assert calls == ["agent-2"]
    assert scheduler.status == "completed"
    rows = {row["chapter_id"]: row for row in store.list_chapters("r-ledger")}
    assert rows["ch-1"]["attempts"] == 0
    assert rows["ch-2"]["status"] == "missing"
    assert rows["ch-2"]["reason"] == "retry_exhausted"


def test_fast_章级墙钟超限直接转_missing_不留补轮(tmp_path):
    """§D-039 改语义：原用例名为「先 deferred 补一轮仍超限转 missing」。

    章 deadline 是从首次起跑算的绝对墙钟，补轮那次派活的 deadline_at 从起跑
    就已过期，跑也是白跑——timeout 型不再进 deferred，一次落 missing。
    D-008 期望 a（墙钟到点 reason 恒为 timeout）原样保留。
    """
    from app.orchestrator.scheduler import Scheduler, TaskRunResult
    from app.plan.model import Plan

    store = _store(tmp_path)
    source = make_plan_dict()
    source["research_id"] = "r-ledger"
    source["scale"] = "fast"
    source["baseline"] = None
    source["goals"] = source["goals"][:1]
    source["goals"][0]["retry_policy"].update(
        max_attempts_per_round=3,
        max_rounds=2,
        ask_engine_switch_at=3,
        chapter_deadline_seconds=10,
    )
    plan = Plan.from_dict(source)
    current = [datetime(2026, 8, 22, tzinfo=timezone.utc)]
    calls = []

    async def run_task(agent, context):
        calls.append(context.attempt)
        current[0] = current[0].replace(second=current[0].second + 11)
        return TaskRunResult(False, context.engine, reason="socket_closed")

    scheduler = Scheduler(
        plan, run_task, lambda event: None,
        lambda: current[0],
        lambda delay, callback: None,
        chapter_ledger=store,
    )
    asyncio.run(scheduler.start())
    row = store.list_chapters("r-ledger")[0]
    assert calls == [1]
    assert row["status"] == "missing"
    # D-008 期望 a：墙钟到点的章 reason 恒为 timeout，不再退回 retry_exhausted
    assert row["reason"] == "timeout"
    assert row["attempts"] == 1


def test_D003缺陷B_在跑章复位只动running且保留attempts(tmp_path):
    """/stop 打断的章必须从 running 复位成 pending，终态章不受影响。"""
    store = _store(tmp_path)
    store.ensure_chapters(
        "r-ledger",
        [{"goal_id": "goal-1", "chapter_id": "ch-1"},
         {"goal_id": "goal-1", "chapter_id": "ch-2"}],
        updated_at="2026-08-22T00:00:00Z",
    )
    store.start_chapter("r-ledger", "goal-1", "ch-1", engine="codex",
                        updated_at="2026-08-22T00:01:00Z")
    store.start_chapter("r-ledger", "goal-1", "ch-2", engine="codex",
                        updated_at="2026-08-22T00:01:00Z")
    store.finish_chapter("r-ledger", "goal-1", "ch-2", status="done", reason=None,
                         actual_output_path="goals/goal-1/ch-2.json", actual_count=1,
                         updated_at="2026-08-22T00:02:00Z")

    assert store.reset_running_chapter(
        "r-ledger", "goal-1", "ch-1", updated_at="2026-08-22T00:03:00Z") is True
    # 终态章不是 running，复位是空操作
    assert store.reset_running_chapter(
        "r-ledger", "goal-1", "ch-2", updated_at="2026-08-22T00:03:00Z") is False

    rows = {row["chapter_id"]: row for row in store.list_chapters("r-ledger")}
    assert rows["ch-1"]["status"] == "pending"
    assert rows["ch-1"]["attempts"] == 1
    assert rows["ch-1"]["reason"] is None
    assert rows["ch-2"]["status"] == "done"
    # 复位后可以重新派活
    assert store.start_chapter("r-ledger", "goal-1", "ch-1", engine="codex",
                               updated_at="2026-08-22T00:04:00Z") is True
    assert store.list_chapters("r-ledger")[0]["attempts"] == 2


def test_D003缺陷B_stop打断未成功的章后resume重跑到done(tmp_path):
    """/stop 时在跑且没跑成的章复位成 pending，agent 回到 queued，resume 后真续跑。"""
    from app.orchestrator.scheduler import Scheduler, TaskRunResult
    from app.plan.model import Plan

    store = _store(tmp_path)
    source = make_plan_dict()
    source["research_id"] = "r-ledger"
    source["goals"] = source["goals"][:1]
    source["baseline"] = None
    source["goals"][0]["agents"][0]["chapter"]["chapter_id"] = "ch-1"
    plan = Plan.from_dict(source)
    agent_id = plan.goals[0].agents[0].agent_id
    attempts: list[int] = []
    started = asyncio.Event()
    release = asyncio.Event()

    # 时钟必须前进，否则第二次尝试会卡在章级重试间隔上（假 timer 不会回调）
    current = [datetime(2026, 8, 22, tzinfo=timezone.utc)]

    async def run_task(agent, context):
        attempts.append(context.attempt)
        current[0] = current[0] + timedelta(minutes=1)
        if len(attempts) == 1:
            started.set()
            await release.wait()
            return TaskRunResult(False, context.engine, engine_error="被 stop 打断")
        return TaskRunResult(
            True, context.engine, actual_output_path=str(agent.output["path"]),
            actual_count=1,
        )

    async def scenario():
        scheduler = Scheduler(
            plan, run_task, lambda event: None,
            lambda: current[0],
            lambda delay, callback: None,
            chapter_ledger=store,
        )
        driving = asyncio.create_task(scheduler.start())
        await started.wait()
        await scheduler.stop()
        release.set()
        await driving
        assert scheduler.status == "stopped"
        assert scheduler.agent_statuses[agent_id] == "queued"
        assert store.list_chapters("r-ledger")[0]["status"] == "pending"

        await scheduler.resume()
        return scheduler

    scheduler = asyncio.run(scenario())
    assert attempts == [1, 2]
    row = store.list_chapters("r-ledger")[0]
    assert row["status"] == "done"
    assert row["attempts"] == 2
    assert scheduler.agent_statuses[agent_id] == "done"
