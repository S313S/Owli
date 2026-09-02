"""§D-033：片墙钟到点被当成节终态，不走节级重试、已成片白丢。

真跑证据（r-f59fdba77cd7）：goal-2 ch-6/sec-2 四片写成三片，只因第 4 片跑满
自己那份墙钟就整节落 missing/timeout，节 attempts 只用了 1/3；而同一节里
retry_exhausted 却能走节级重试 + 已成片 `write_shard_skipped`，一次只补坏片。
"""

from __future__ import annotations

from tests.test_d031_write_sharding import _shard_run


def _expire_shard(monkeypatch, marker: str, *, times: int | None = 1) -> dict:
    """让指定片的前 times 次尝试撞片墙钟（times=None 表示每次都撞）。"""
    import app.orchestrator.sectioning as sectioning

    original = sectioning._run_before_section_deadline
    seen = {"n": 0}

    async def spy(adapter, task, ctx, on_event, deadline):
        if marker in task.output_path.name:
            seen["n"] += 1
            if times is None or seen["n"] <= times:
                raise sectioning.SectionWallClockExpired(f"{marker} 跑满片墙钟")
        return await original(adapter, task, ctx, on_event, deadline)

    monkeypatch.setattr(sectioning, "_run_before_section_deadline", spy)
    return seen


def test_d033_片墙钟到点走节级重试_只补坏片_节最终done(tmp_path, monkeypatch):
    _expire_shard(monkeypatch, ".part.2.")
    result, store, bodies, events, _ = _shard_run(
        tmp_path, evidence=30, wall_clock=330.0,
    )

    # 节 attempts 未满、剩余节墙钟够一次 resume：发 section_retry 而不是直接作废。
    retries = [e["data"] for e in events if e["type"] == "section_retry"]
    assert [(item["attempt"], item["resume"]) for item in retries] == [(2, True)]
    finished = [e["data"] for e in events if e["type"] == "write_shard_finished"]
    assert [(item["shard"], item["succeeded"]) for item in finished] == [
        (1, True), (2, False), (3, True), (2, True),
    ]
    assert finished[1]["reason"] == "timeout"
    # 第 1/3 片的字不重写：第二次节尝试只起了第 2 片一次会话。
    assert [
        e["data"]["shard"] for e in events if e["type"] == "write_shard_skipped"
    ] == [1, 3]
    assert list(bodies) == [f"sec-1.part.{k}.md" for k in (1, 3, 2)]
    assert result.succeeded is True
    row = store.list_chapters("r-ledger")[0]
    assert (row["status"], row["reason"]) == ("done", None)


def test_d033_guard_节attempts用尽后仍落timeout(tmp_path, monkeypatch):
    """闸不能变成无限重试：SECTION_RETRY_MAX_ATTEMPTS 用尽照旧 missing/timeout。"""
    _expire_shard(monkeypatch, ".part.2.", times=None)
    result, store, _, events, _ = _shard_run(
        tmp_path, evidence=30, wall_clock=330.0,
    )

    assert result.succeeded is False
    row = store.list_chapters("r-ledger")[0]
    assert (row["status"], row["reason"]) == ("missing", "timeout")
    errors = [e["data"] for e in events if e["type"] == "section_error"]
    assert errors and errors[-1]["timeout_kind"] == "wall_clock"


def test_d033_guard_剩余节墙钟不足一次resume成本时仍落timeout(tmp_path, monkeypatch):
    """不加时间：节墙钟（按片数放大后）剩不下 136 s 就如实 timeout，不重试。"""
    _expire_shard(monkeypatch, ".part.2.")
    result, store, _, events, _ = _shard_run(
        tmp_path, evidence=30, wall_clock=40.0,
    )

    assert not [e for e in events if e["type"] == "section_retry"]
    assert result.succeeded is False
    row = store.list_chapters("r-ledger")[0]
    assert (row["status"], row["reason"]) == ("missing", "timeout")
