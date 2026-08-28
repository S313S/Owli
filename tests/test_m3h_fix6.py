"""D-007（缺陷 E）：节级传输断连做有限退避重试，章级重试重新派活传输耗尽的节。"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from tests.test_m3h_ledger import _store


# 6b 整跑账本逐字文案（runs/6b-final/ledger-final.txt）
TRANSPORT_ERROR = json.dumps({
    "is_error": True,
    "api_error_status": None,
    "subtype": "error_during_execution",
    "result": "API Error: The socket connection was closed unexpectedly",
    "errors": ["API Error: The socket connection was closed unexpectedly"],
}, ensure_ascii=False)
RATE_LIMIT_ERROR = json.dumps({
    "is_error": True,
    "api_error_status": 429,
    "subtype": "error_during_execution",
    "result": "API Error: 429 rate_limit_error",
}, ensure_ascii=False)
SECTION_BODY = "## 结论\n\n正文。\n\n## 信息源\n\n- 来源 A。\n"


def _plan(per_round: int, scale: str = "fast"):
    policy = {"max_attempts_per_round": per_round, "max_rounds": 2}
    return SimpleNamespace(
        research_id="r-ledger",
        title="报告",
        scale=scale,
        goals=[
            SimpleNamespace(goal_id="goal-2", title="节一", retry_policy=dict(policy)),
            SimpleNamespace(goal_id="goal-3", title="节二", retry_policy=dict(policy)),
        ],
    )


def _task(runs_root, validators):
    from app.adapters.capability import Capability, FileSystemScope
    from app.adapters.contracts import EngineTask

    return EngineTask(
        body="写报告",
        output_path=runs_root / "r-ledger/goals/goal-3/report.md",
        output_format="markdown",
        research_id="r-ledger",
        goal_id="goal-3",
        agent_id="report-writing",
        agent_kind="report",
        validators=validators,
        capability=Capability(
            tools=("fs.write",),
            fs=FileSystemScope(write=("goals/goal-3/**",)),
        ),
    )


def _adapter(
    sec2_errors: list[str | None],
    calls: list[str],
    sec1_error=None,
    *,
    clock=None,
    failure_step: float = 0.0,
):
    """sec-1 默认恒成功；sec-2 依次按 sec2_errors 给结果（None = 成功）。"""

    from app.adapters import validation
    from app.adapters.contracts import EngineRunResult, OwliResult

    class Adapter:
        async def run(self, task, ctx, on_event=None):
            del on_event
            calls.append(task.output_path.name)
            engine_error = sec1_error
            if task.output_path.name == "sec-2.md":
                index = min(len([c for c in calls if c == "sec-2.md"]), len(sec2_errors))
                engine_error = sec2_errors[index - 1]
            if engine_error is not None:
                if clock is not None:
                    clock[0] += timedelta(seconds=failure_step)
                return EngineRunResult(
                    conclusion=None, conclusion_error=None,
                    validation=validation.ValidationReport(validation.Verdict.PASS, []),
                    events=[], permission_denials=[], engine_error=engine_error,
                )
            task.output_path.parent.mkdir(parents=True, exist_ok=True)
            task.output_path.write_text(SECTION_BODY, encoding="utf-8")
            return EngineRunResult(
                conclusion=OwliResult(
                    "done", str(task.output_path), "完成", [], [], [], None,
                ),
                conclusion_error=None,
                validation=validation.validate(ctx, task.validators),
                events=[], permission_denials=[],
            )

    return Adapter()


def _run(tmp_path, *, per_round, sec2_errors, store=None, calls=None,
         validators=None, events=None, delays=None, sec1_error=None,
         section_wall_clock=None, failure_step=0.0):
    from app.orchestrator.sectioning import run_sectioned_task

    store = store if store is not None else _store(tmp_path)
    runs_root = tmp_path / "runs"
    calls = calls if calls is not None else []
    events = events if events is not None else []
    delays = delays if delays is not None else []
    clock = [datetime(2026, 8, 22, tzinfo=timezone.utc)]

    async def on_event(event):
        events.append(event)

    def timer(delay, callback):
        delays.append(delay)
        clock[0] += timedelta(seconds=delay)
        callback()

    result = asyncio.run(run_sectioned_task(
        plan=_plan(per_round),
        agent=SimpleNamespace(chapter={"chapter_id": "ch-1", "opening": {"inputs": []}}),
        context=SimpleNamespace(
            goal_id="goal-3",
            engine="claude",
            section_deadline_seconds=section_wall_clock,
        ),
        base_task=_task(runs_root, validators or ["file_exists"]),
        adapter=_adapter(
            sec2_errors,
            calls,
            sec1_error,
            clock=clock,
            failure_step=failure_step,
        ),
        store=store,
        runs_root=runs_root,
        now_iso=lambda: "2026-08-22T00:00:03Z",
        on_event=on_event,
        timer=timer,
        now=lambda: clock[0],
    ))
    rows = {r["chapter_id"]: r for r in store.list_chapters("r-ledger")}
    return SimpleNamespace(result=result, rows=rows, calls=calls,
                           events=events, delays=delays, store=store)


def test_节级传输断连退避重试后成功_不落missing也不换引擎(tmp_path):
    run = _run(tmp_path, per_round=3, sec2_errors=[TRANSPORT_ERROR, None])

    assert run.calls == ["sec-1.md", "sec-2.md", "sec-2.md"]
    assert run.rows["ch-1/sec-2"]["status"] == "done"
    assert run.rows["ch-1/sec-2"]["reason"] is None
    assert run.rows["ch-1/sec-2"]["attempts"] == 2
    assert run.rows["ch-1/sec-2"]["engine"] == "claude"
    # 中途断连只发可观测的 section_retry，不发 section_error
    assert [e["type"] for e in run.events] == ["section_retry"]
    assert run.events[0]["data"]["resume"] is False
    assert run.events[0]["data"]["session_id"] is None
    # 退避沿用章级口径：fast = 5s
    assert run.delays == [5.0]
    assert run.result.succeeded is True


def test_连续断连在剩余预算不足时落timeout并保留重试事件协议(tmp_path):
    run = _run(
        tmp_path,
        per_round=3,
        sec2_errors=[TRANSPORT_ERROR],
        section_wall_clock=330,
        failure_step=100,
    )

    assert run.calls.count("sec-2.md") == 2
    assert run.rows["ch-1/sec-2"]["status"] == "missing"
    assert run.rows["ch-1/sec-2"]["reason"] == "timeout"
    assert run.rows["ch-1/sec-2"]["attempts"] == 2
    assert run.rows["ch-1/sec-2"]["engine"] == "claude"
    assert run.delays == [5.0]
    assert [e["type"] for e in run.events] == [
        "section_retry", "section_error",
    ]
    assert run.events[0]["data"] == {
        "goal_id": "goal-3",
        "chapter_id": "ch-1/sec-2",
        "attempt": 2,
        "resume": False,
        "session_id": None,
    }


def test_真429不走传输重试_仍归quota_exhausted(tmp_path):
    run = _run(tmp_path, per_round=3, sec2_errors=[RATE_LIMIT_ERROR])

    assert run.calls.count("sec-2.md") == 1
    assert run.rows["ch-1/sec-2"]["status"] == "missing"
    assert run.rows["ch-1/sec-2"]["reason"] == "quota_exhausted"
    assert run.rows["ch-1/sec-2"]["attempts"] == 1
    assert run.delays == []


def test_章级下一轮重新派活传输耗尽的节(tmp_path):
    store = _store(tmp_path)
    first = _run(tmp_path, per_round=1, sec2_errors=[TRANSPORT_ERROR], store=store)
    assert first.rows["ch-1/sec-2"]["reason"] == "retry_exhausted"
    section_file = tmp_path / "runs/r-ledger/goals/goal-3/report/sec-2.md"
    assert "此处缺失" in section_file.read_text(encoding="utf-8")

    second = _run(tmp_path, per_round=1, sec2_errors=[None], store=store)

    # sec-1 已 done 仍跳过；sec-2 被复位重派并写成
    # 第一轮已按独立常量 SECTION_RETRY_MAX_ATTEMPTS 退避耗尽，第二轮再派一次即 done
    from app.orchestrator.sectioning import SECTION_RETRY_MAX_ATTEMPTS

    assert second.calls == ["sec-2.md"]
    assert second.rows["ch-1/sec-2"]["status"] == "done"
    assert second.rows["ch-1/sec-2"]["attempts"] == SECTION_RETRY_MAX_ATTEMPTS + 1
    assert "此处缺失" not in section_file.read_text(encoding="utf-8")
    assert second.result.succeeded is True


def test_非传输原因的missing节不被复位_下一轮仍跳过(tmp_path):
    store = _store(tmp_path)
    first = _run(tmp_path, per_round=1, sec2_errors=[RATE_LIMIT_ERROR], store=store)
    assert first.rows["ch-1/sec-2"]["reason"] == "quota_exhausted"

    second = _run(tmp_path, per_round=1, sec2_errors=[None], store=store)

    assert second.calls == []
    assert second.rows["ch-1/sec-2"]["status"] == "missing"
    assert second.rows["ch-1/sec-2"]["reason"] == "quota_exhausted"
    assert second.rows["ch-1/sec-2"]["attempts"] == 1


def test_节全丢且全为传输耗尽时不直接定终态_交回章级重试(tmp_path):
    run = _run(tmp_path, per_round=2, sec2_errors=[TRANSPORT_ERROR],
               sec1_error=TRANSPORT_ERROR)

    assert run.rows["ch-1/sec-1"]["reason"] == "retry_exhausted"
    assert run.rows["ch-1/sec-2"]["reason"] == "retry_exhausted"
    assert run.result.succeeded is False
    # 不写 chapter_status → Scheduler 走正常章级重试，而不是立刻落 missing
    assert run.result.chapter_status is None
    assert run.result.reason == "retry_exhausted"


def test_节全丢但原因混杂时仍立刻定终态missing(tmp_path):
    run = _run(tmp_path, per_round=2, sec2_errors=[TRANSPORT_ERROR],
               sec1_error=RATE_LIMIT_ERROR)

    assert run.rows["ch-1/sec-1"]["reason"] == "quota_exhausted"
    assert run.rows["ch-1/sec-2"]["reason"] == "retry_exhausted"
    assert run.result.chapter_status == "missing"


def test_reset_retry_exhausted_chapters只放行retry_exhausted(tmp_path):
    store = _store(tmp_path)
    ids = ["ch-1/sec-1", "ch-1/sec-2", "ch-1/sec-3"]
    store.ensure_chapters(
        "r-ledger", [{"goal_id": "goal-3", "chapter_id": cid} for cid in ids],
        updated_at="2026-08-22T00:00:00Z",
    )
    for cid, status, reason in (
        ("ch-1/sec-1", "done", None),
        ("ch-1/sec-2", "missing", "retry_exhausted"),
        ("ch-1/sec-3", "missing", "tool_unavailable"),
    ):
        store.start_chapter("r-ledger", "goal-3", cid,
                            engine="claude", updated_at="2026-08-22T00:00:01Z")
        store.finish_chapter("r-ledger", "goal-3", cid, status=status, reason=reason,
                             actual_output_path="/tmp/x", actual_count=0,
                             updated_at="2026-08-22T00:00:02Z")

    reset = store.reset_retry_exhausted_chapters(
        "r-ledger", "goal-3", ids, updated_at="2026-08-22T00:00:03Z")

    rows = {r["chapter_id"]: r for r in store.list_chapters("r-ledger")}
    assert reset == ["ch-1/sec-2"]
    assert rows["ch-1/sec-1"]["status"] == "done"
    assert rows["ch-1/sec-2"]["status"] == "pending"
    assert rows["ch-1/sec-2"]["reason"] is None
    # attempts 不清零：耗尽历史要留痕
    assert rows["ch-1/sec-2"]["attempts"] == 1
    assert rows["ch-1/sec-3"]["status"] == "missing"
