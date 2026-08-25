from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.plan_factory import make_plan_dict
from tests.test_m3h_ledger import _store


def _single_agent_plan(*, deadline_seconds: int | None = None):
    from app.plan.model import Plan

    source = make_plan_dict()
    source["research_id"] = "r-ledger"
    source["scale"] = "fast"
    source["baseline"] = None
    source["goals"] = source["goals"][:1]
    source["goals"][0]["retry_policy"].update(
        max_attempts_per_round=3,
        max_rounds=2,
        ask_engine_switch_at=3,
    )
    if deadline_seconds is not None:
        source["goals"][0]["retry_policy"]["chapter_deadline_seconds"] = (
            deadline_seconds
        )
    return Plan.from_dict(source)


def test_E1_共享原因归类识别超时且保留闭集优先级() -> None:
    from app.orchestrator.chapter_failure import chapter_failure_reason

    assert chapter_failure_reason({
        "engine_error": None,
        "conclusion_error": "Codex 任务超时（300 秒），已终止",
    }) == "timeout"
    assert chapter_failure_reason({
        "reason": "quota_exhausted",
        "engine_error": "timeout after rate limit",
    }) == "quota_exhausted"
    assert chapter_failure_reason({
        "reason": "timeout",
        "permission_denials": ["附带的旧拒绝记录"],
    }) == "timeout"


def test_E1_墙钟降级按最后结果真实死因落_timeout(tmp_path: Path) -> None:
    from app.orchestrator.scheduler import Scheduler, TaskRunResult

    store = _store(tmp_path)
    plan = _single_agent_plan(deadline_seconds=1)
    current = [datetime(2026, 8, 22, tzinfo=timezone.utc)]

    async def run_task(agent, context):
        current[0] += timedelta(seconds=2)
        return TaskRunResult(
            False,
            context.engine,
            conclusion_error="Codex 任务超时（300 秒），已终止并要求整任务重跑",
        )

    def timer(delay: float, callback):
        if delay <= 15:
            current[0] += timedelta(seconds=delay)
            callback()
        return object()

    scheduler = Scheduler(
        plan,
        run_task,
        lambda event: None,
        lambda: current[0],
        timer,
        chapter_ledger=store,
    )
    asyncio.run(scheduler.start())

    row = store.list_chapters("r-ledger")[0]
    assert row["status"] == "missing"
    assert row["reason"] == "timeout"
    assert row["attempts"] == 2


def test_E1_同因收敛也使用共享原因归类(tmp_path: Path) -> None:
    from app.orchestrator.scheduler import Scheduler, TaskRunResult

    store = _store(tmp_path)
    plan = _single_agent_plan()
    current = [datetime(2026, 8, 22, tzinfo=timezone.utc)]

    async def run_task(agent, context):
        return TaskRunResult(
            False,
            context.engine,
            engine_error="夹具注入：引擎调用超时",
            conclusion_error="夹具注入：结构化结论未生成",
        )

    def timer(delay: float, callback):
        if delay <= 15:
            current[0] += timedelta(seconds=delay)
            callback()
        return object()

    scheduler = Scheduler(
        plan,
        run_task,
        lambda event: None,
        lambda: current[0],
        timer,
        chapter_ledger=store,
    )
    asyncio.run(scheduler.start())

    row = store.list_chapters("r-ledger")[0]
    assert row["reason"] == "timeout"
    assert row["attempts"] == 3


def test_E1_v5账本迁移到v6后接受_timeout(tmp_path: Path) -> None:
    from app.store.dao import Store
    from app.store.schema import initialize_database_if_empty

    database = tmp_path / "owli-v5.db"
    schema = Path(__file__).resolve().parents[1] / "app" / "store" / "schema.sql"
    with sqlite3.connect(database) as connection:
        connection.executescript("""
        CREATE TABLE reports (id TEXT PRIMARY KEY) STRICT;
        INSERT INTO reports(id) VALUES ('r-ledger');
        CREATE TABLE chapter_progress (
          research_id TEXT NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
          goal_id TEXT NOT NULL,
          chapter_id TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending','running','done','missing','deferred')),
          attempts INTEGER NOT NULL DEFAULT 0,
          engine TEXT,
          reason TEXT CHECK (reason IN (
            'empty_result','tool_unavailable','quota_exhausted','retry_exhausted',
            'conclusion_invalid'
          ) OR reason IS NULL),
          engine_error TEXT,
          conclusion_error TEXT,
          actual_output_path TEXT,
          actual_count INTEGER,
          updated_at TEXT NOT NULL,
          PRIMARY KEY (research_id, goal_id, chapter_id)
        ) STRICT;
        INSERT INTO chapter_progress(
          research_id, goal_id, chapter_id, updated_at
        ) VALUES ('r-ledger', 'goal-1', 'ch-1', '2026-08-22T00:00:00Z');
        PRAGMA user_version = 5;
        """)

    initialize_database_if_empty(database, schema)
    store = Store(database)
    store.finish_chapter(
        "r-ledger",
        "goal-1",
        "ch-1",
        status="missing",
        reason="timeout",
        actual_output_path=None,
        actual_count=0,
        updated_at="2026-08-22T00:01:00Z",
    )

    with sqlite3.connect(database) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert version == 7
    assert store.list_chapters("r-ledger")[0]["reason"] == "timeout"


def test_E2_fast墙钟为600且启动自检拒绝不大于引擎超时的档位() -> None:
    from app.adapters.selfcheck import RuntimeConfigCheckError, validate_runtime_config
    from app.config import load_research_scale_config

    product = load_research_scale_config()
    assert product.fast.chapter_wall_clock_seconds == 600
    assert product.standard.chapter_wall_clock_seconds is None
    validate_runtime_config(product)

    invalid = load_research_scale_config({
        "fast": {"chapter_wall_clock_seconds": 300},
    })
    with pytest.raises(
        RuntimeConfigCheckError,
        match=r"fast.*300.*严格大于.*Codex.*300",
    ):
        validate_runtime_config(invalid)


def test_E2_API_lifespan执行运行配置自检(tmp_path: Path) -> None:
    from app.api.main import create_app
    from app.config import load_research_scale_config

    schema = Path(__file__).resolve().parents[1] / "app" / "store" / "schema.sql"
    invalid = load_research_scale_config({
        "fast": {"chapter_wall_clock_seconds": 300},
    })
    app = create_app(
        tmp_path / "owli.db",
        schema,
        frontend_dist=tmp_path / "web",
        scale_config=invalid,
        engine_probe=lambda: {},
    )

    async def start() -> None:
        async with app.router.lifespan_context(app):
            pass

    with pytest.raises(RuntimeError, match="chapter_wall_clock_seconds"):
        asyncio.run(start())


def test_E3_Claude权限根使用EngineTask注入runs_root(tmp_path: Path) -> None:
    from app.adapters import claude
    from app.adapters.capability import Capability
    from app.adapters.contracts import EngineTask

    runs_root = tmp_path / "runs"
    task = EngineTask(
        body="写报告",
        output_path=runs_root / "r" / "goals" / "goal-1" / "report.md",
        output_format="markdown",
        research_id="r",
        goal_id="goal-1",
        agent_id="report-writing",
        agent_kind="report",
        validators=["file_exists"],
        capability=Capability(),
        runs_root=runs_root,
    )

    assert claude._goal_root(task) == (
        runs_root / "r" / "goals" / "goal-1"
    ).resolve(strict=False)
    assert claude._capability_path(
        task,
        str(runs_root / "r" / "goals" / "goal-1" / "report.md"),
    ) == "goals/goal-1/report.md"


async def _consume_codex_stream(adapter, events, emitted, *items: dict) -> None:
    stream = asyncio.StreamReader()
    for item in items:
        stream.feed_data((json.dumps(item) + "\n").encode())
    stream.feed_eof()
    await adapter._consume(stream, events, emitted.append)


def test_E4_无关联的启动期error在后续DONE时降为普通事件(tmp_path: Path) -> None:
    from app.adapters.codex import CodexAdapter
    from app.adapters.events import ItemKind

    raw_warning = {
        "type": "item.completed",
        "item": {"id": "item_0", "type": "error", "message": "未来版本的非致命启动告警"},
    }
    events = []
    emitted = []
    adapter = CodexAdapter(codex_home=tmp_path / "codex-home")

    asyncio.run(_consume_codex_stream(
        adapter,
        events,
        emitted,
        {"type": "thread.started", "thread_id": "thread-1"},
        {"type": "turn.started"},
        raw_warning,
        {"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": "完成"}},
        {"type": "turn.completed"},
    ))

    warning = next(event for event in emitted if event.raw == raw_warning)
    assert warning.item_kind is ItemKind.THINKING
    assert warning.is_error is False
    assert any(event.item_kind is ItemKind.DONE for event in emitted)


def test_E4_无DONE的结构化error仍保留为真错误(tmp_path: Path) -> None:
    from app.adapters.codex import CodexAdapter
    from app.adapters.events import ItemKind

    raw_error = {
        "type": "item.completed",
        "item": {"id": "item_0", "type": "error", "message": "真实启动失败"},
    }
    events = []
    emitted = []
    adapter = CodexAdapter(codex_home=tmp_path / "codex-home")

    asyncio.run(_consume_codex_stream(
        adapter,
        events,
        emitted,
        {"type": "thread.started", "thread_id": "thread-1"},
        {"type": "turn.started"},
        raw_error,
    ))

    error = next(event for event in emitted if event.raw == raw_error)
    assert error.item_kind is ItemKind.ERROR
    assert error.is_error is True


def test_E4_plugin_catalog刷新WARN不升级为基础设施错误() -> None:
    from app.adapters.codex import _infrastructure_error
    from app.adapters.events import ItemKind, NormalizedEvent

    text = (
        "2026-08-22T14:53:12Z WARN codex_core_plugins::manager: "
        "failed to refresh cached remote plugin catalog "
        "error=error sending request for url"
    )
    warning = NormalizedEvent(
        "Codex", None, None, ItemKind.THINKING, text, False, text,
    )

    assert _infrastructure_error([warning]) is None
