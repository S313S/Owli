"""§D-043：/stop 要掐得到 goal finalize 触发的可靠度回填引擎调用。

D-041 真机重放（8963）撞出的第三条钱漏腿：`_finalize_if_terminal` 里的
`backfill_report` 不在 `scheduler._running_runs` 里，`/stop` 的
`_cancel_running_run` 遍历不到它——**没人 cancel**，D-041 那个「被取消就杀
子进程」的修复也就无从触发，codex 子进程在 /stop 之后又活了 61 s。
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

#: 取消后允许引擎子进程存活的上限。
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
import time

print(json.dumps({{"type": "thread.started", "thread_id": "thread-d043"}}), flush=True)
pathlib.Path({str(pid_path)!r}).write_text(str(os.getpid()), encoding="utf-8")
time.sleep(600)
'''
    path.write_text(source, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


async def _await_pid(pid_path: Path, timeout: float = 20.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pid_path.is_file():
            text = pid_path.read_text(encoding="utf-8").strip()
            if text:
                return int(text)
        await asyncio.sleep(0.02)
    raise AssertionError("假 codex 子进程始终没报出 PID")


def _coordinator(tmp_path: Path, monkeypatch, events: list, adapter):
    """搭一个「goal 已全 done、正在收尾」的运行时：finalize 一进来就跑回填。"""

    from app.orchestrator import runtime as runtime_module
    from app.orchestrator.runtime import RuntimeCoordinator
    from tests.test_m3h_finalize import _plan, _write
    from tests.test_m3h_ledger import _store
    from tests.test_m4fork_followup import _evidence

    plan = _plan(report_format="markdown")
    _write(
        tmp_path / "runs" / "r-ledger" / "goals" / "goal-3" / "report.md",
        "# 结论\n\n- 收尾期。\n\n# 信息源\n\n- 无。\n",
    )
    store = _store(tmp_path)
    # 只有源基线、没被 agent 评过的行才进得了回填的引擎批（RATE-1 货 5 的兜底口径）。
    store.upsert_evidence_batch([
        _evidence("r-ledger", f"b{index:03d}", rated_by="baseline:web_search@v1", extra={})
        for index in range(3)
    ])

    async def publish(research_id, payload):
        events.append(payload)

    monkeypatch.setattr(runtime_module, "load_plan", lambda store_, rid: plan)
    coordinator = RuntimeCoordinator(
        store=store, event_buffer=SimpleNamespace(publish=publish), researches={}, cards={},
        runs_root=tmp_path / "runs", auto_confirm=True,
        routing_utc_clock=lambda: datetime(2026, 9, 4, tzinfo=timezone.utc),
    )
    coordinator.researches[plan.research_id] = coordinator._state_from_plan(plan)

    async def scheduler_stop() -> None:
        scheduler.status = "stopped"

    scheduler = SimpleNamespace(
        status="completed",
        goal_statuses={"goal-1": "done", "goal-2": "done", "goal-3": "done"},
        stop=scheduler_stop,
    )
    coordinator._schedulers[plan.research_id] = scheduler
    coordinator._adapters[plan.research_id] = adapter
    return coordinator, plan, store


def test_收尾期_stop_掐得到回填的引擎子进程(tmp_path: Path, monkeypatch) -> None:
    """货 1/2：finalize 正跑回填时 /stop → 引擎任务被取消、子进程 ≤1 s 退出。"""

    from app.adapters import codex
    from app.adapters import validation

    monkeypatch.setattr(validation, "RUNS_ROOT", tmp_path / "runs")
    pid_path = tmp_path / "child.pid"
    executable = tmp_path / "fake-codex"
    _write_sleepy_codex(executable, pid_path)
    adapter = codex.CodexAdapter(
        executable=str(executable), codex_home=tmp_path / "runtime-home"
    )
    events: list[dict] = []
    coordinator, plan, _store_ = _coordinator(tmp_path, monkeypatch, events, adapter)

    async def scenario() -> tuple[int, float, bool, bool]:
        finalize = asyncio.create_task(
            coordinator._finalize_if_terminal(plan.research_id)
        )
        pid = await _await_pid(pid_path)
        assert _alive(pid), "假引擎子进程应当在跑"
        started = time.monotonic()
        await coordinator.stop(plan.research_id)
        elapsed = time.monotonic() - started
        still_alive = _alive(pid)
        # 修好之后收尾自己就走完了；旧码里它会一直等假子进程睡满，所以给个上限。
        try:
            await asyncio.wait_for(asyncio.shield(finalize), timeout=20)
            finished = True
        except asyncio.TimeoutError:
            finalize.cancel()
            await asyncio.gather(finalize, return_exceptions=True)
            finished = False
        return pid, elapsed, still_alive, finished

    pid, elapsed, still_alive, finished = asyncio.run(scenario())
    try:
        assert not still_alive, "/stop 之后回填的引擎子进程还在跑：D-043 复现"
        assert elapsed <= KILL_DEADLINE_SECONDS, f"/stop 耗时 {elapsed:.2f}s"
        assert finished, "取消回填不该把收尾卡死"
        cancelled = [
            event for event in events
            if event.get("type") == "reliability_backfill_cancelled"
        ]
        assert cancelled, "取消要留痕（判据落库不落日志）"
        # 收尾没被掐断的引擎任务连累：报告照常落盘。
        assert _store_.get_report("r-ledger")["status"] == "completed"
    finally:
        if _alive(pid):
            os.killpg(pid, 9)


def test_取消回填时已付费的一手性审计照常结算落库(tmp_path: Path) -> None:
    """判据 5：取消后状态一致——判完的批次结算，没判到的断言原样不改口。

    `_audit_firsthand` 原本把所有批次的判读攒在内存里、最后一次性写 claims，
    中途被 /stop 掐掉等于把已经付过钱的引擎调用全丢了（不是半批写入，是全丢）。
    """
    from app.reliability.backfill import backfill_report
    from app.reliability.claims import register_claims
    from tests.test_m3h_ledger import _store
    from tests.test_m4fork_followup import _evidence

    store = _store(tmp_path)
    urls = {key: f"https://e.example/{key}" for key in ("a", "b")}
    store.upsert_evidence_batch([
        _evidence("r-ledger", key, permalink=urls[key], extra={}) for key in urls
    ])
    register_claims(store, "r-ledger", [
        {"id": f"c-{index:02d}", "text": f"断言 {index}",
         "evidence": [{"permalink": urls[key], "stance": "supports", "firsthand": True}]}
        for index, key in enumerate(urls, 1)
    ], source="chapter")

    seen: list[str] = []
    gate = asyncio.Event()

    class OneBatchThenBlockAdapter:
        """第一批照常判完，第二批进去就长睡——模拟「掐在第二批」。"""

        timeout_seconds = None

        async def run(self, task, ctx, on_event=None):
            body = task.body
            seen.append(body)
            if len(seen) > 1:
                await gate.wait()
            pairs = json.JSONDecoder().raw_decode(
                body.split("输入 (断言, 证据) 对：", 1)[1]
            )[0] if "输入 (断言, 证据) 对：" in body else []
            payload = [
                {"claim_id": pair["claim_id"], "evidence_id": pair["evidence_id"],
                 "firsthand": True, "reason": "本人一手叙述"}
                for pair in pairs
            ]
            Path(task.output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(task.output_path).write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            return SimpleNamespace(succeeded=True)

    async def scenario() -> None:
        run = asyncio.ensure_future(backfill_report(
            store, "r-ledger", adapter=OneBatchThenBlockAdapter(),
            runs_root=tmp_path / "runs", batch_size=1,
        ))
        while len(seen) < 2:
            await asyncio.sleep(0.01)
        run.cancel()
        await asyncio.gather(run, return_exceptions=True)

    asyncio.run(scenario())
    claims = {claim["id"]: claim for claim in store.get_report("r-ledger")["extra"]["claims"]}
    assert claims["c-01"].get("firsthand_source") == "audited", "判完的那条要落库"
    assert claims["c-02"].get("firsthand_source") != "audited", "没判到的不许改口"


def test_墙钟形态的取消者同样掐得到回填(tmp_path: Path, monkeypatch) -> None:
    """判据 3：取消钩子不认「谁在掐」——定时器回调直接调它，效果与 /stop 一致。

    说明：当下代码里**没有**研究级总墙钟（墙钟只有章级与 goal 级，且都在
    scheduler 里，收尾期已经出了它的管辖），所以这条只能验到「钩子对任何
    取消者都生效」。将来真加研究级墙钟，到点调这一个钩子即可。
    """
    from app.adapters import codex
    from app.adapters import validation

    monkeypatch.setattr(validation, "RUNS_ROOT", tmp_path / "runs")
    pid_path = tmp_path / "child.pid"
    executable = tmp_path / "fake-codex"
    _write_sleepy_codex(executable, pid_path)
    adapter = codex.CodexAdapter(
        executable=str(executable), codex_home=tmp_path / "runtime-home"
    )
    events: list[dict] = []
    coordinator, plan, _store_ = _coordinator(tmp_path, monkeypatch, events, adapter)

    async def scenario() -> tuple[int, float, bool]:
        finalize = asyncio.create_task(
            coordinator._finalize_if_terminal(plan.research_id)
        )
        pid = await _await_pid(pid_path)
        started = time.monotonic()
        # 墙钟到点该做的事就是这一句（scheduler 的 expire() 也是同款同步回调风格）。
        await coordinator._cancel_backfill_run(plan.research_id)
        elapsed = time.monotonic() - started
        still_alive = _alive(pid)
        await asyncio.wait_for(finalize, timeout=20)
        return pid, elapsed, still_alive

    pid, elapsed, still_alive = asyncio.run(scenario())
    try:
        assert not still_alive, "墙钟形态的取消没掐到回填的引擎子进程"
        assert elapsed <= KILL_DEADLINE_SECONDS, f"取消耗时 {elapsed:.2f}s"
    finally:
        if _alive(pid):
            os.killpg(pid, 9)
