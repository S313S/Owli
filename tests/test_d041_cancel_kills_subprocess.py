"""§D-041：墙钟/`stop` 取消在跑任务时，引擎子进程必须跟着死。

D-039 真机读数：章判 missing 后 codex 子进程还活了 ≈187 s（273k input tokens 白烧）。
根因是 `CancelledError` 属 BaseException，`run()` 的 `except Exception` 接不住，
`finally` 只把进程从 `_processes` 里摘掉、没终止它。
"""

import asyncio
import json
import os
import stat
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

#: 取消后允许子进程存活的上限（判据 2）。
KILL_DEADLINE_SECONDS = 1.0


def _alive(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _write_sleepy_codex(path: Path, pid_path: Path) -> None:
    """假 codex：报出自己的 PID，吐一行事件，然后长睡不醒。"""

    source = f'''#!/usr/bin/env python3
import json
import os
import pathlib
import sys
import time

print(json.dumps({{"type": "thread.started", "thread_id": "thread-d041"}}), flush=True)
pathlib.Path({str(pid_path)!r}).write_text(str(os.getpid()), encoding="utf-8")
time.sleep(600)
'''
    path.write_text(source, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _ctx(validation, output_path: Path):
    return validation.Ctx(
        research_id="research-d041",
        goal_id="goal-1",
        agent_id="agent-1",
        output_path=output_path,
        output_format="markdown",
        read_text=lambda: output_path.read_text(encoding="utf-8"),
        read_json=lambda: json.loads(output_path.read_text(encoding="utf-8")),
        store=None,
        source_domains=frozenset(),
    )


def _task(codex, output_path: Path):
    return codex.CodexTask(
        body=f"长跑任务。\nARTIFACT={output_path}",
        output_path=output_path,
        output_format="markdown",
        research_id="research-d041",
        goal_id="goal-1",
        agent_id="agent-1",
        validators=["file_exists"],
        tools=frozenset(),
        sandbox="workspace-write",
        network=True,
    )


async def _await_pid(pid_path: Path, timeout: float = 20.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pid_path.is_file():
            text = pid_path.read_text(encoding="utf-8").strip()
            if text:
                return int(text)
        await asyncio.sleep(0.02)
    raise AssertionError("假 codex 子进程始终没报出 PID")


def test_墙钟取消_codex_任务时子进程一秒内退出(tmp_path, monkeypatch):
    """货 1/2：`task.cancel()` 后 codex 子进程组必须 ≤1 s 消失，且取消语义原样上抛。"""

    from app.adapters import validation
    from app.adapters import codex

    runs_root = tmp_path / "runs"
    monkeypatch.setattr(validation, "RUNS_ROOT", runs_root)
    output_path = runs_root / "research-d041" / "goals" / "goal-1" / "result.md"
    pid_path = tmp_path / "child.pid"
    executable = tmp_path / "fake-codex"
    _write_sleepy_codex(executable, pid_path)
    adapter = codex.CodexAdapter(
        executable=str(executable), codex_home=tmp_path / "runtime-home"
    )
    token = object()

    async def scenario() -> tuple[int, float]:
        run_task = asyncio.ensure_future(
            adapter.run(
                _task(codex, output_path),
                _ctx(validation, output_path),
                run_token=token,
            )
        )
        pid = await _await_pid(pid_path)
        assert _alive(pid), "假子进程应当在跑"
        run_task.cancel()
        # 调度器 `_cancel_running_run` 之后是 `await asyncio.wait({run_future})`：
        # 取消要能落地，adapter 必须在 CancelledError 里把子进程收干净。
        started = time.monotonic()
        with_cancelled = False
        try:
            await run_task
        except asyncio.CancelledError:
            with_cancelled = True
        assert with_cancelled, "取消语义必须原样上抛（reason 仍由 scheduler 记）"
        return pid, time.monotonic() - started

    pid, elapsed = asyncio.run(scenario())
    try:
        assert elapsed <= KILL_DEADLINE_SECONDS, f"取消耗时 {elapsed:.2f}s"
        assert not _alive(pid), "取消后子进程组仍在跑：D-041 复现"
        assert not adapter._processes, "取消后 _processes 应清空"
    finally:
        if _alive(pid):
            os.killpg(pid, 9)


def _write_sigterm_deaf_codex(path: Path, pid_path: Path) -> None:
    """假 codex：装聋——收到 SIGTERM 也不退，只有 SIGKILL 能收拾它。"""

    source = f'''#!/usr/bin/env python3
import json
import os
import pathlib
import signal
import time

signal.signal(signal.SIGTERM, signal.SIG_IGN)
print(json.dumps({{"type": "thread.started", "thread_id": "thread-d041"}}), flush=True)
pathlib.Path({str(pid_path)!r}).write_text(str(os.getpid()), encoding="utf-8")
time.sleep(600)
'''
    path.write_text(source, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_取消时不吃_SIGTERM_的子进程被补上_SIGKILL(tmp_path, monkeypatch):
    """货 2 核对：`_terminate_process` 的 SIGTERM→SIGKILL 升级在取消路径上也生效。"""

    from app.adapters import validation
    from app.adapters import codex

    runs_root = tmp_path / "runs"
    monkeypatch.setattr(validation, "RUNS_ROOT", runs_root)
    output_path = runs_root / "research-d041" / "goals" / "goal-1" / "result.md"
    pid_path = tmp_path / "deaf.pid"
    executable = tmp_path / "fake-codex-deaf"
    _write_sigterm_deaf_codex(executable, pid_path)
    adapter = codex.CodexAdapter(
        executable=str(executable), codex_home=tmp_path / "runtime-home"
    )

    async def scenario() -> tuple[int, float]:
        run_task = asyncio.ensure_future(
            adapter.run(_task(codex, output_path), _ctx(validation, output_path))
        )
        pid = await _await_pid(pid_path)
        run_task.cancel()
        started = time.monotonic()
        try:
            await run_task
        except asyncio.CancelledError:
            pass
        return pid, time.monotonic() - started

    pid, elapsed = asyncio.run(scenario())
    try:
        assert elapsed <= KILL_DEADLINE_SECONDS, f"取消耗时 {elapsed:.2f}s"
        assert not _alive(pid), "装聋子进程没被 SIGKILL 收拾"
    finally:
        if _alive(pid):
            os.killpg(pid, 9)


def _scheduler_store(tmp_path: Path):
    import sqlite3

    from app.store.dao import Store

    schema = ROOT / "app" / "store" / "schema.sql"
    database = tmp_path / "owli.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(schema.read_text(encoding="utf-8"))
    store = Store(database)
    store.create_report(
        id="r-d041", title="D-041", research_question="测试",
        created_at="2026-09-04T00:00:00Z",
    )
    return store


def _scheduler_plan(deadline: int):
    from app.plan.model import Plan
    from tests.plan_factory import make_plan_dict

    source = make_plan_dict()
    source["research_id"] = "r-d041"
    source["scale"] = "fast"
    source["baseline"] = None
    source["goals"] = source["goals"][:1]
    source["goals"][0]["retry_policy"].update(
        max_attempts_per_round=1,
        max_rounds=1,
        ask_engine_switch_at=1,
        chapter_deadline_seconds=deadline,
    )
    return Plan.from_dict(source)


def _run_scheduler_with_real_codex(tmp_path, monkeypatch, *, leg: str):
    """真跑一条 scheduler→CodexAdapter→假 codex 子进程的链路，按 leg 掐断它。

    leg="timeout" 走章墙钟定时器，leg="stopped" 走 /stop：两条腿共用
    `_cancel_running_run`（D-008），这里各验一次子进程有没有跟着死。
    """
    from datetime import datetime, timedelta, timezone

    from app.adapters import validation
    from app.adapters import codex
    from app.orchestrator.scheduler import Scheduler

    runs_root = tmp_path / "runs"
    monkeypatch.setattr(validation, "RUNS_ROOT", runs_root)
    output_path = runs_root / "research-d041" / "goals" / "goal-1" / "result.md"
    pid_path = tmp_path / f"{leg}.pid"
    executable = tmp_path / f"fake-codex-{leg}"
    _write_sleepy_codex(executable, pid_path)
    adapter = codex.CodexAdapter(
        executable=str(executable), codex_home=tmp_path / "runtime-home"
    )
    store = _scheduler_store(tmp_path)
    plan = _scheduler_plan(10)
    now = [datetime(2026, 9, 4, tzinfo=timezone.utc)]
    timers: list[tuple[float, object]] = []

    async def run_task(agent, context):
        await adapter.run(
            _task(codex, output_path), _ctx(validation, output_path),
            run_token=agent.agent_id,
        )
        raise AssertionError("假 codex 长睡不醒，不该正常返回")

    async def scenario():
        scheduler = Scheduler(
            plan, run_task, lambda event: None, lambda: now[0],
            lambda delay, callback: timers.append((delay, callback)),
            chapter_ledger=store,
        )
        driving = asyncio.create_task(scheduler.start())
        pid = await _await_pid(pid_path)
        started = time.monotonic()
        if leg == "stopped":
            await scheduler.stop()
        else:
            now[0] = now[0] + timedelta(seconds=60)
            assert timers, "章墙钟定时器没挂上"
            # 首次派活时挂的章墙钟是最后一个（第一个是 goal 级 `_expire_goal` 协程）。
            assert timers[-1][1]() is None, "章墙钟回调应当是同步的 expire()"
        await driving
        return pid, time.monotonic() - started

    return asyncio.run(scenario())


def test_章墙钟到点时调度器链路上的_codex_子进程跟着死(tmp_path, monkeypatch):
    """判据 2 端到端：`_cancel_running_run(timeout)` 之后不留活着的 CLI。"""

    pid, elapsed = _run_scheduler_with_real_codex(tmp_path, monkeypatch, leg="timeout")
    try:
        assert not _alive(pid), "章判 missing 后 codex 子进程还在跑（D-039 §九.1 的 187 s）"
        assert elapsed <= KILL_DEADLINE_SECONDS, f"取消耗时 {elapsed:.2f}s"
    finally:
        if _alive(pid):
            os.killpg(pid, 9)


def test_stop_掐断时调度器链路上的_codex_子进程同样被杀干净(tmp_path, monkeypatch):
    """判据 4：/stop 与墙钟共用取消路径（D-008），杀子进程这件事两边一视同仁。"""

    pid, elapsed = _run_scheduler_with_real_codex(tmp_path, monkeypatch, leg="stopped")
    try:
        assert not _alive(pid), "/stop 之后 codex 子进程还在跑"
        assert elapsed <= KILL_DEADLINE_SECONDS, f"取消耗时 {elapsed:.2f}s"
    finally:
        if _alive(pid):
            os.killpg(pid, 9)


def _fake_sdk(pid_path: Path):
    """假 SDK：client 起一个真长睡子进程，`disconnect()` 像真 transport 那样收尸。

    真 SDK（claude_agent_sdk 0.1.81）的 `disconnect() → query.close() →
    ProcessTransport.close()` 会关 stdin、最多等 5 s、再 terminate、再 kill；
    本用例锁的是**我方**这一半：取消时 `_run_once` 的 finally 必须真的走到
    `await client.disconnect()` 并等它做完，子进程才有人收。
    """

    class FakeClient:
        def __init__(self, options):
            self.options = options
            self.process = None

        async def connect(self, prompt):
            self.process = await asyncio.create_subprocess_exec(
                sys.executable, "-c", "import time; time.sleep(600)",
                start_new_session=True,
            )
            pid_path.write_text(str(self.process.pid), encoding="utf-8")

        async def receive_response(self):
            await asyncio.sleep(600)
            yield None  # pragma: no cover - 取消先到

        async def disconnect(self):
            if self.process is not None and self.process.returncode is None:
                os.killpg(self.process.pid, 9)
                await self.process.wait()

    class FakeOptions:
        def __init__(self, **values):
            self.values = values

    class FakeSdk:
        ClaudeSDKClient = FakeClient
        ClaudeAgentOptions = FakeOptions
        ResultMessage = type("ResultMessage", (), {})
        AssistantMessage = type("AssistantMessage", (), {})
        UserMessage = type("UserMessage", (), {})
        SystemMessage = type("SystemMessage", (), {})
        TextBlock = type("TextBlock", (), {})
        ToolUseBlock = type("ToolUseBlock", (), {})
        PermissionResultAllow = type("Allow", (), {})
        PermissionResultDeny = type(
            "Deny", (), {"__init__": lambda self, **values: None}
        )
        HookMatcher = type(
            "HookMatcher", (),
            {"__init__": lambda self, matcher=None, hooks=None: None},
        )

    return FakeSdk


def test_取消_claude_任务时_SDK_子进程随_disconnect_一起收尸(tmp_path, monkeypatch):
    """货 3：claude 侧取消路径靠 `_run_once` 的 finally → `client.disconnect()`。

    结论（读 claude_agent_sdk 0.1.81 源码 + 本用例）：SDK 自带 kill 钩子，
    我方无需另加 CancelledError 分支——只要 finally 里的 disconnect 在取消时
    仍被 await 完（本用例锁的就是这一点）。
    """
    from app.adapters import validation
    from app.adapters.capability import Capability, FileSystemScope
    from app.adapters.claude import ClaudeAdapter
    from app.adapters.contracts import EngineTask

    runs_root = tmp_path / "runs"
    monkeypatch.setattr(validation, "RUNS_ROOT", runs_root)
    output_path = runs_root / "r-d041" / "goals" / "goal-1" / "result.md"
    pid_path = tmp_path / "claude-child.pid"
    task = EngineTask(
        body="长跑任务", output_path=output_path, output_format="markdown",
        research_id="r-d041", goal_id="goal-1", agent_id="agent-1",
        agent_kind="report", validators=["file_exists"],
        capability=Capability(
            tools=("fs.write",),
            fs=FileSystemScope(write=("goals/goal-1/**",)),
        ),
    )
    adapter = ClaudeAdapter(sdk=_fake_sdk(pid_path), log_root=tmp_path / "logs")

    async def scenario() -> tuple[int, float]:
        run_task = asyncio.ensure_future(
            adapter.run(task, _ctx(validation, output_path), run_token=object())
        )
        pid = await _await_pid(pid_path)
        run_task.cancel()
        started = time.monotonic()
        try:
            await run_task
        except asyncio.CancelledError:
            pass
        return pid, time.monotonic() - started

    pid, elapsed = asyncio.run(scenario())
    try:
        assert not _alive(pid), "取消 claude 任务后 SDK 子进程还在跑"
        assert elapsed <= KILL_DEADLINE_SECONDS, f"取消耗时 {elapsed:.2f}s"
        assert not adapter._clients, "取消后 _clients 应清空"
    finally:
        if _alive(pid):
            os.killpg(pid, 9)
