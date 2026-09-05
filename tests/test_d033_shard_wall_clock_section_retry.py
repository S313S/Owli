"""§D-033：片墙钟到点被当成节终态，不走节级重试、已成片白丢。

真跑证据（r-f59fdba77cd7）：goal-2 ch-6/sec-2 四片写成三片，只因第 4 片跑满
自己那份墙钟就整节落 missing/timeout，节 attempts 只用了 1/3；而同一节里
retry_exhausted 却能走节级重试 + 已成片 `write_shard_skipped`，一次只补坏片。
"""

from __future__ import annotations

import pytest

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


def _shift_loop_clock(monkeypatch, sectioning, offset: dict) -> None:
    """让 sectioning 看到的 loop.time() 多走 offset["s"] 秒（虚拟钟）。"""
    import asyncio as real_asyncio

    class _Loop:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def time(self):
            return self._inner.time() + offset["s"]

    class _AsyncioProxy:
        def __getattr__(self, name):
            return getattr(real_asyncio, name)

        def get_running_loop(self):
            return _Loop(real_asyncio.get_running_loop())

    monkeypatch.setattr(sectioning, "asyncio", _AsyncioProxy())


def test_d033_节级重试放行的坏片_按D047续写预算放大且仍封顶(tmp_path, monkeypatch):
    """§D-047 语义更替（原名 `..._片墙钟夹在节剩余时间内`）。

    D-033 原来把重试轮的片墙钟一路夹到「第一轮设下的节绝对时刻」上，理由是
    防止单节最坏耗时翻倍。真机 `r-8532b5c2c026` 证明这把夹子会掐死本该救回来的
    节：3/4 片已写成，最后一片只分到 80 s，19 075 B 好稿整节作废。D-047 改成
    「续写轮按未落盘片数把时间还回来」——本用例锁的因此换成两条：
    ① 未落盘的那一片拿得到**一整片**的时长（而不是被原剩余截断）；
    ② 放大仍有封顶：一轮最多加一个节墙钟，重试前的每次调用照旧夹在原预算内。
    """
    import app.orchestrator.sectioning as sectioning

    offset = {"s": 0.0}
    _shift_loop_clock(monkeypatch, sectioning, offset)
    original = sectioning._run_before_section_deadline
    calls: list[tuple[float, float]] = []
    seen = {"n": 0}

    async def spy(adapter, task, ctx, on_event, deadline):
        now = sectioning.asyncio.get_running_loop().time()
        calls.append((now, deadline))
        if ".part.2." in task.output_path.name:
            seen["n"] += 1
            if seen["n"] == 1:
                # 这一片跑满了自己那份墙钟：节预算随之逼近见底。
                offset["s"] = 800.0
                raise sectioning.SectionWallClockExpired("片 2 跑满片墙钟")
        return await original(adapter, task, ctx, on_event, deadline)

    monkeypatch.setattr(sectioning, "_run_before_section_deadline", spy)
    result, store, _, events, _ = _shard_run(
        tmp_path, evidence=30, wall_clock=330.0,
    )

    assert [e["data"]["attempt"] for e in events if e["type"] == "section_retry"] == [2]
    section_start, _ = calls[0]
    # 节预算 = 330 × 3 片；重试放行时原剩余只有 190 s（不够一整片）。
    budget_end = section_start + 330.0 * 3
    retry_now, retry_deadline = calls[-1]
    assert retry_now - section_start == pytest.approx(800.0, abs=2.0)
    # §D-047：只剩 1 片未落盘 → 本轮预算抬到 300+30 s，这一片拿到整片时长。
    assert retry_deadline - retry_now == pytest.approx(330.0, abs=2.0)
    budget = [e["data"] for e in events if e["type"] == "section_resume_budget"]
    assert [(b["shards_left"], b["extended"]) for b in budget] == [(1, True)]
    assert budget[0]["budget_s"] == pytest.approx(330.0, abs=2.0)
    # 封顶还在：一轮最多加一个节墙钟；重试**之前**的调用一律夹在原预算内。
    assert retry_deadline <= budget_end + 330.0 + 1.0
    assert all(deadline <= budget_end + 1.0 for _, deadline in calls[:-1])
    assert result.succeeded is True
    assert store.list_chapters("r-ledger")[0]["status"] == "done"
