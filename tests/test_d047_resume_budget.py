"""§D-047：续写轮沿用第一轮的节墙钟，最后一片没时间跑，整节好稿被判死。

真机 `r-8532b5c2c026`（8956 补 goal-3/ch-6/sec-1）：节切四片，第一轮只合上
2/4；第二轮 resume 正确跳过前两片、第 3 片 181 s 写成，轮到第 4 片时整节墙钟
只剩 80 s，被 `wall_clock` 判死，已合的 19 075 B 三片好稿整节作废成占位。
resume 省下了前几片的钱，却没把时间还回来。
"""

from __future__ import annotations

import pytest

from tests.test_d031_write_sharding import _shard_run
from tests.test_d033_shard_wall_clock_section_retry import _shift_loop_clock


def _budget_events(events: list[dict]) -> list[dict]:
    return [e["data"] for e in events if e["type"] == "section_resume_budget"]


def test_d047_四片中三片已落盘_续写轮给最后一片整片时长并合成节(tmp_path, monkeypatch):
    """判据一：原 deadline 只剩 80 s → 本轮预算 ≥ 330 s，第 4 片真被调起并合并。"""
    import app.orchestrator.sectioning as sectioning

    # 节池上限 30 条，真机那一节是按**字节**切成 4 片的（证据条正文长）；
    # 切法不是本用例要验的，直接按 8/8/8/6 切出同样的 4 片。
    monkeypatch.setattr(
        sectioning, "write_shard_sizes", lambda items, **kw: [8, 8, 8, 6],
    )
    offset = {"s": 0.0}
    _shift_loop_clock(monkeypatch, sectioning, offset)
    original = sectioning._run_before_section_deadline
    calls: list[tuple[str, float, float]] = []
    seen = {"n": 0}

    async def spy(adapter, task, ctx, on_event, deadline):
        now = sectioning.asyncio.get_running_loop().time()
        calls.append((task.output_path.name, now, deadline))
        if ".part.4." in task.output_path.name:
            seen["n"] += 1
            if seen["n"] == 1:
                # 前三片烧掉 1240 s（节预算 330×4=1320），第 4 片只分到 80 s。
                offset["s"] = 1240.0
                raise sectioning.SectionWallClockExpired("第 4 片被节墙钟掐死")
        return await original(adapter, task, ctx, on_event, deadline)

    monkeypatch.setattr(sectioning, "_run_before_section_deadline", spy)
    result, store, _, events, _ = _shard_run(
        tmp_path, evidence=30, wall_clock=330.0,
    )

    budget = _budget_events(events)
    assert [(b["shards_left"], b["shards"], b["extended"]) for b in budget] == [
        (1, 4, True),
    ]
    assert budget[0]["remaining_seconds"] == pytest.approx(80.0, abs=2.0)
    assert budget[0]["budget_s"] >= 330.0
    # 第 4 片这一次拿到的是**一整片**的时长，不是被剩余预算截断的 80 s。
    retry_name, retry_now, retry_deadline = calls[-1]
    assert ".part.4." in retry_name
    assert retry_deadline - retry_now == pytest.approx(330.0, abs=2.0)
    # 判据落在库与产物上：第 4 片真被调起、四片合成一节、节 done。
    assert [
        e["data"]["shard"] for e in events if e["type"] == "write_shard_skipped"
    ] == [1, 2, 3]
    merged = [e["data"] for e in events if e["type"] == "write_shards_merged"]
    assert [item["done"] for item in merged] == [4]
    assert result.succeeded is True
    row = store.list_chapters("r-ledger")[0]
    assert (row["status"], row["reason"]) == ("done", None)


def test_d047_guard_零片落盘不放大_本轮预算等于原剩余(tmp_path, monkeypatch):
    """判据一的对照：一片都没落盘时 resume 什么也没省下，一秒不多给。"""
    import app.orchestrator.sectioning as sectioning

    monkeypatch.setattr(
        sectioning, "write_shard_sizes", lambda items, **kw: [8, 8, 8, 6],
    )
    original = sectioning._run_before_section_deadline
    calls: list[float] = []

    async def spy(adapter, task, ctx, on_event, deadline):
        calls.append(deadline)
        return await original(adapter, task, ctx, on_event, deadline)

    monkeypatch.setattr(sectioning, "_run_before_section_deadline", spy)
    _, store, _, events, _ = _shard_run(
        tmp_path, evidence=30, wall_clock=330.0, fail_shards=(1, 2, 3, 4),
    )

    budget = _budget_events(events)
    assert budget, "续写轮的预算事件要照发，extended=false 也发"
    for item in budget:
        assert (item["shards_left"], item["shards"]) == (4, 4)
        assert item["extended"] is False
        assert item["budget_s"] == pytest.approx(item["remaining_seconds"])
    # 没放大：所有引擎调用的绝对时刻都还夹在第一轮设下的节预算里。
    assert max(calls) <= min(calls) + 330.0 * 4 + 1.0
    assert store.list_chapters("r-ledger")[0]["status"] == "missing"


def _budget(remaining, *, shards_left, shard_count, wall=330.0):
    from app.orchestrator.sectioning import _section_resume_budget

    return _section_resume_budget(
        remaining, shards_left=shards_left, shard_count=shard_count,
        section_wall_clock=wall,
    )


def test_d047_预算口径_按未落盘片数放大_封在增量上():
    # 三片已落盘、只剩一片：抬到一整片（300 s）+ 合并余量 30 s。
    assert _budget(80.0, shards_left=1, shard_count=4) == 330.0
    # 两片未落盘：想要 630 s，但一轮最多加一个节墙钟 → 80+330。
    assert _budget(80.0, shards_left=2, shard_count=4) == 410.0
    # 原剩余本来就够：一秒不多给，也绝不往下削。
    assert _budget(900.0, shards_left=1, shard_count=4) == 900.0
    # 零片落盘 / 不分片的节：不放大，行为与 D-047 之前逐字相同。
    assert _budget(80.0, shards_left=4, shard_count=4) == 80.0
    assert _budget(80.0, shards_left=1, shard_count=1) == 80.0
    # 节墙钟本身很小时，一片值的也只有那么多（40 s），不凭空放大。
    assert _budget(120.0, shards_left=1, shard_count=3, wall=40.0) == 120.0


def test_d047_门槛按未落盘片数算_默认入参与D047之前逐字相同():
    from app.orchestrator.sectioning import (
        SECTION_RESUME_COST_FLOOR_SECONDS as FLOOR,
        _section_resume_within_deadline as gate,
    )

    def ask(remaining, **kw):
        return gate(
            None, retry_delay=0.0, wall_clock_seconds=remaining,
            wall_clock_started_at=0.0, now=lambda: _Elapsed(0.0), **kw,
        )

    # 默认（不分片）：门槛仍是一次 136 s，预算不放大。
    assert ask(FLOOR) is True
    assert ask(FLOOR - 1) is False
    # 两片未落盘、两片已落盘：门槛翻倍成 272 s，但预算也按片数抬了。
    assert ask(200.0, shards_left=2, shard_count=4, section_wall_clock=330.0) is True
    # 四片全未落盘：不放大，200 s 撑不起 4×136 s，如实不重试。
    assert ask(200.0, shards_left=4, shard_count=4, section_wall_clock=330.0) is False


class _Elapsed:
    """`now() - started` 要能算出 timedelta：这里直接给 0 秒。"""

    def __init__(self, seconds: float) -> None:
        self._seconds = seconds

    def __sub__(self, other):  # noqa: D105 - 只为算 total_seconds
        return _Elapsed(self._seconds)

    def total_seconds(self) -> float:
        return self._seconds
