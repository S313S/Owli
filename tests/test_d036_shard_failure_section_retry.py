"""§D-036：非片墙钟的片级失败不走 D-033 补坏片路，一片坏整节作废。

真跑证据（r-e74541583a05，2026-09-03 夜补跑 §g #3/#4/#5，均在 FAILOVER→codex 之后）：
- #4 ch-4/sec-2：片 1 单次 300.0 s 撞 codex 硬顶（timeout_kind=engine_timeout），
  节剩余预算 ~580 s、attempts=1，却整节作废 21.8 KB（含 3 片好稿）；
- #3 ch-3/sec-1：片 3 codex MCP `Transport channel closed`，reason 落 empty_result，
  `_is_transport_failure` 首行即 False，片内/节级都不重试，整节作废 21 KB；
- #5 ch-4/sec-3：片 1「Codex 最终消息未落盘」无产物，merged 3/4 后整节作废 17.4 KB。

D-033 只接 `SectionWallClockExpired`；这三种片级失败各走自己的落 missing 分支。
"""

from __future__ import annotations

import pytest

from tests.test_d031_write_sharding import _shard_run


#: 引擎硬顶：codex 单次调用跑满 300 s（reason 归 timeout）。
ENGINE_TIMEOUT_ERROR = "engine call timed out after 300.0s"
#: codex MCP 传输断的原文形态（socket 词表认不出，reason 落 empty_result）。
CODEX_TRANSPORT_ERROR = (
    "Transport channel closed: https://chatgpt.com/backend-api/ps/mcp"
)
#: codex 进程无产物即退出（reason 落 empty_result）。
CODEX_NO_ARTIFACT_ERROR = (
    "Codex 最终消息未落盘："
    "runs/r-ledger/goals/goal-1/report/sec-1.part-2-codex-last-message.json 不存在"
)


def _fail_shard(monkeypatch, marker: str, engine_error: str, *, times: int | None = 1):
    """让指定片的前 times 次尝试以给定引擎报错失败、且不落片产物。

    times=None 表示每次都失败。拦在 `_run_before_section_deadline` 上，
    假引擎那一层不动（夹具与断言禁改）。
    """
    import app.orchestrator.sectioning as sectioning
    from app.adapters import validation
    from app.adapters.contracts import EngineRunResult

    original = sectioning._run_before_section_deadline
    seen = {"n": 0}

    async def spy(adapter, task, ctx, on_event, deadline):
        if marker in task.output_path.name:
            seen["n"] += 1
            if times is None or seen["n"] <= times:
                return EngineRunResult(
                    conclusion=None,
                    conclusion_error=None,
                    validation=validation.ValidationReport(
                        validation.Verdict.FAIL,
                        [validation.Result(
                            validation.Verdict.FAIL, "file_exists", "missing", [],
                        )],
                    ),
                    events=[], permission_denials=[],
                    engine_error=engine_error,
                )
        return await original(adapter, task, ctx, on_event, deadline)

    monkeypatch.setattr(sectioning, "_run_before_section_deadline", spy)
    return seen


def test_d036_引擎硬顶超时走节级重试补坏片(tmp_path, monkeypatch):
    """#4：片撞 300 s 硬顶 ≠ 节预算用完；attempts 未满就该只补坏片。"""
    _fail_shard(monkeypatch, ".part.2.", ENGINE_TIMEOUT_ERROR)
    result, store, bodies, events, _ = _shard_run(
        tmp_path, evidence=30, wall_clock=330.0,
    )

    retries = [e["data"] for e in events if e["type"] == "section_retry"]
    assert [(item["attempt"], item["resume"]) for item in retries] == [(2, True)]
    finished = [e["data"] for e in events if e["type"] == "write_shard_finished"]
    assert [(item["shard"], item["succeeded"]) for item in finished] == [
        (1, True), (2, False), (3, True), (2, True),
    ]
    assert finished[1]["reason"] == "timeout"
    # 已成片不重写：第二次节尝试只起了第 2 片一次会话。
    assert [
        e["data"]["shard"] for e in events if e["type"] == "write_shard_skipped"
    ] == [1, 3]
    assert list(bodies) == [f"sec-1.part.{k}.md" for k in (1, 3, 2)]
    assert result.succeeded is True
    row = store.list_chapters("r-ledger")[0]
    assert (row["status"], row["reason"]) == ("done", None)


def test_d036_codex传输断判为传输失败并重试(tmp_path, monkeypatch):
    """#3：`Transport channel closed` 是断连，不是「这一片问不出来」。"""
    import app.orchestrator.sectioning as sectioning

    result_stub = type("R", (), {
        "engine_error": CODEX_TRANSPORT_ERROR, "conclusion_error": None,
    })()
    # 这个形态的 reason 归到 empty_result（socket 词表认不出它），
    # 旧实现首行就 return False，片内/节级都不重试。
    assert sectioning._is_transport_failure(result_stub, "empty_result") is True

    _fail_shard(monkeypatch, ".part.2.", CODEX_TRANSPORT_ERROR)
    result, store, _, events, _ = _shard_run(
        tmp_path, evidence=30, wall_clock=330.0,
    )

    # 片内原地退避重试（不占节级 attempts），第 2 次就写成。
    shard_retries = [e["data"] for e in events if e["type"] == "write_shard_retry"]
    assert [(item["shard"], item["attempt"]) for item in shard_retries] == [(2, 2)]
    finished = [e["data"] for e in events if e["type"] == "write_shard_finished"]
    assert [(item["shard"], item["succeeded"]) for item in finished] == [
        (1, True), (2, True), (3, True),
    ]
    assert result.succeeded is True
    row = store.list_chapters("r-ledger")[0]
    assert (row["status"], row["reason"]) == ("done", None)


def test_d036_codex无产物走节级重试(tmp_path, monkeypatch):
    """#5：codex 无产物即退出，reason 落 empty_result，也该只补坏片。"""
    _fail_shard(monkeypatch, ".part.2.", CODEX_NO_ARTIFACT_ERROR)
    result, store, bodies, events, _ = _shard_run(
        tmp_path, evidence=30, wall_clock=330.0,
    )

    retries = [e["data"] for e in events if e["type"] == "section_retry"]
    assert [(item["attempt"], item["resume"]) for item in retries] == [(2, True)]
    finished = [e["data"] for e in events if e["type"] == "write_shard_finished"]
    assert finished[1]["reason"] == "empty_result"
    assert [
        e["data"]["shard"] for e in events if e["type"] == "write_shard_skipped"
    ] == [1, 3]
    assert list(bodies) == [f"sec-1.part.{k}.md" for k in (1, 3, 2)]
    assert result.succeeded is True
    row = store.list_chapters("r-ledger")[0]
    assert (row["status"], row["reason"]) == ("done", None)


@pytest.mark.parametrize(
    "engine_error, expected_reason, expected_timeout_kind",
    [
        (ENGINE_TIMEOUT_ERROR, "timeout", "engine_timeout"),
        (CODEX_NO_ARTIFACT_ERROR, "empty_result", None),
    ],
)
def test_d036_attempts已满仍落原reason(
    tmp_path, monkeypatch, engine_error, expected_reason, expected_timeout_kind,
):
    """闸不能变成无限重试，也不许把原因改写掉：attempts 用尽照旧落原 reason。"""
    _fail_shard(monkeypatch, ".part.2.", engine_error, times=None)
    result, store, _, events, _ = _shard_run(
        tmp_path, evidence=30, wall_clock=330.0,
    )

    retries = [e["data"] for e in events if e["type"] == "section_retry"]
    assert [item["attempt"] for item in retries] == [2, 3]
    assert result.succeeded is False
    row = store.list_chapters("r-ledger")[0]
    assert (row["status"], row["reason"]) == ("missing", expected_reason)
    errors = [e["data"] for e in events if e["type"] == "section_error"]
    assert errors and errors[-1]["timeout_kind"] == expected_timeout_kind
    assert errors[-1]["original_reason"] == expected_reason


def test_d036_guard_节余量不足时不重试(tmp_path, monkeypatch):
    """不加时间：节墙钟（按片数放大后）剩不下 136 s 就如实落终态。"""
    _fail_shard(monkeypatch, ".part.2.", ENGINE_TIMEOUT_ERROR, times=None)
    result, store, _, events, _ = _shard_run(
        tmp_path, evidence=30, wall_clock=40.0,
    )

    assert not [e for e in events if e["type"] == "section_retry"]
    assert result.succeeded is False
    row = store.list_chapters("r-ledger")[0]
    assert (row["status"], row["reason"]) == ("missing", "timeout")
