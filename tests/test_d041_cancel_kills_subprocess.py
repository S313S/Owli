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
