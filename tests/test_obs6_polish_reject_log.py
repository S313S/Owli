"""§OBS-6 货 1：正式稿每被打回一次，落一行台账 + 发一条 `polish_reject` 事件。

**这一包只加读数，不加闸**：打回的判词、重试次数、早停线一个字都没动。
所以每条用例除了查「多写的那一份」，都同时钉住「正稿结果没变」。

背景：打回原因原先只拼进下一轮 prompt，而 prompt 不进转录；账本又只在一格跑完
才落盘。09-11 那条早停线「同一条判词连续打回 3 次就停」因此无处可读，
包终端自造的尺子把写手正确遵守规则的限定句数成了「被打回」。
"""

from __future__ import annotations

import asyncio
import json

from app.report.polish.run import REJECT_EVENT_TYPE, polish, rejects_path
from tests.test_write1_polish_gates import (GATE_WORK, ORIGINAL, QUOTED_ALTERED, QUOTED_OK,
                                            _GateStore, _quoting_adapter)


class _RecordingStore(_GateStore):
    """真 Store 的 `append_event` 形状（关键字参数一致），把事件留在内存里查。"""

    def __init__(self, boom: bool = False) -> None:
        self.events: list[dict] = []
        self._boom = boom

    def append_event(self, research_id, *, event_type, payload, created_at):
        if self._boom:
            raise RuntimeError("库是只读副本")
        row = {"research_id": research_id, "type": event_type,
               "payload": payload, "created_at": created_at}
        self.events.append(row)
        return {**row, "sequence": len(self.events)}


def _run(tmp_path, quote_block, store=None):
    store = store if store is not None else _RecordingStore()
    outcome = asyncio.run(polish(store, "r-gate", tmp_path / "runs", GATE_WORK,
                                 template="consulting",
                                 adapter=_quoting_adapter(quote_block)))
    return store, outcome


def _lines(tmp_path):
    path = rejects_path(tmp_path / "runs", "r-gate", "consulting")
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# —— 判据 1：打回几次就有几行、几条事件 ————————————————————


def test_每次打回各落一行且各发一条事件(tmp_path):
    """改过字的原声被闸退回两次（MAX_ATTEMPTS=2），就该有两行、两条事件。"""
    store, outcome = _run(tmp_path, QUOTED_ALTERED)
    assert outcome["status"] == "failed"
    rows = _lines(tmp_path)
    assert len(rows) == 2, rows
    assert len(store.events) == 2
    assert {e["type"] for e in store.events} == {REJECT_EVENT_TYPE}
    # payload 与 jsonl 是同一份内容，查事件不必再去翻文件。
    assert [e["payload"]["errors"] for e in store.events] == [r["errors"] for r in rows]


def test_行数等于_attempts_减成功次数(tmp_path):
    """提货单判据 1 的那条等式。这里没有一节写成，所以成功次数 = 0。"""
    _, outcome = _run(tmp_path, QUOTED_ALTERED)
    assert len(_lines(tmp_path)) == outcome["attempts"] - 0 == 2


def test_一行里认得出是哪一节哪一片被哪道闸打回的(tmp_path):
    """光有条数不够——早停线要问的是「**同一条判词**连续打回几次」。"""
    _, _ = _run(tmp_path, QUOTED_ALTERED)
    first = _lines(tmp_path)[0]
    assert first["gate"] == "quote"
    assert first["template"] == "consulting"
    assert first["section"], "得说得出是哪一节"
    assert first["shard"] is None, "这一轮没分片"
    assert first["attempt"] == 1
    assert any("与原文对不上" in e for e in first["errors"]), first["errors"]
    assert first["ts"], "没有时间戳就排不出「连续」"


def test_越池角标那道闸也留痕(tmp_path):
    """六个打回口各自带自己的 gate，少接一处读数就会比 attempts 少。"""
    block = f"> {ORIGINAL}\n> —— 微博 · 等级 A [S01]\n\n越池的那一处 [S99]。\n"
    _, outcome = _run(tmp_path, block)
    assert outcome["status"] == "failed"
    assert [r["gate"] for r in _lines(tmp_path)] == ["offpool", "offpool"]


# —— 判据 2：只多写一份，不改 polish 的结果 ——————————————————


def test_一路写到底的稿一行都不落(tmp_path):
    """没被打回就没有台账文件——别把「正常跑完」也记成打回（协议坑 28/29）。"""
    store, outcome = _run(tmp_path, QUOTED_OK)
    assert outcome["status"] == "ok", outcome.get("errors")
    assert _lines(tmp_path) == []
    assert store.events == []


def test_写不进事件也不许把正稿带挂(tmp_path):
    """观测不许反噬正稿：库是只读副本时，`append_event` 抛异常要被吞掉。"""
    store, outcome = _run(tmp_path, QUOTED_OK, store=_RecordingStore(boom=True))
    assert outcome["status"] == "ok", outcome.get("errors")
    _, failed = _run(tmp_path, QUOTED_ALTERED, store=_RecordingStore(boom=True))
    assert failed["status"] == "failed" and failed["attempts"] == 2


def test_不带_append_event_的_store_照跑(tmp_path):
    """老的测试替身与只读 store 没有这个方法，不许因此炸掉。"""
    _, outcome = _run(tmp_path, QUOTED_OK, store=_GateStore())
    assert outcome["status"] == "ok", outcome.get("errors")
    assert len(_lines(tmp_path)) == 0


def test_打回记录是追加不是覆盖(tmp_path):
    """同一格跑两轮，第二轮不许把第一轮的行冲掉——早停线要看的正是跨轮的连续。"""
    _run(tmp_path, QUOTED_ALTERED)
    assert len(_lines(tmp_path)) == 2
    _run(tmp_path, QUOTED_ALTERED)
    assert len(_lines(tmp_path)) == 4


# —— 货 2：早停线读它 ——————————————————————————————————


import pytest  # noqa: E402

from app.report.polish import run as run_mod  # noqa: E402


@pytest.fixture
def _four_attempts(monkeypatch):
    """把重试上限抬到 4，好让「连着打回 3 次」这条线真的够得着。

    ⚠️ 生产的 `MAX_ATTEMPTS` 是 2，一个目标最多被打回两次——**早停线在单轮里
    根本触发不了**。所以造红必须先抬上限，否则守卫 on/off 两侧读数一模一样，
    那是假绿（判据落在「存在」上而不落在「生效」上）。
    """
    monkeypatch.setattr(run_mod, "MAX_ATTEMPTS", 4)


def _streak_run(tmp_path, monkeypatch, limit):
    if limit is None:
        monkeypatch.delenv(run_mod.REJECT_STREAK_ENV, raising=False)
    else:
        monkeypatch.setenv(run_mod.REJECT_STREAK_ENV, str(limit))
    return _run(tmp_path, QUOTED_ALTERED)


def test_守卫关着时跑满四次(_four_attempts, tmp_path, monkeypatch):
    """off 侧的非零读数：一道闸连打 4 次，4 次全付、4 行全落。"""
    _, outcome = _streak_run(tmp_path, monkeypatch, None)
    assert outcome["status"] == "failed"
    assert outcome["attempts"] == 4
    assert [r["streak"] for r in _lines(tmp_path)] == [1, 2, 3, 4]


def test_守卫开着时第三次就停(_four_attempts, tmp_path, monkeypatch):
    """on 侧：同一条判词连打到第 3 次即止，第 4 次那轮引擎不许付。"""
    _, outcome = _streak_run(tmp_path, monkeypatch, 3)
    assert outcome["status"] == "failed"
    assert outcome["attempts"] == 3, "第 4 轮引擎是省下来的那一轮"
    assert [r["streak"] for r in _lines(tmp_path)] == [1, 2, 3]
    assert any("早停线" in e for e in outcome["errors"]), outcome["errors"]


def test_早停走的是现成的片失败路(_four_attempts, tmp_path, monkeypatch):
    """D-051 语义：节不判 done，半份稿不许留在树上。"""
    _, outcome = _streak_run(tmp_path, monkeypatch, 3)
    assert outcome["failed_section"], "得说得出是哪一节倒的"
    assert outcome["missing_sections"], "倒了的那一节不许算写成"


def test_换了一道闸就从头数(_four_attempts, tmp_path, monkeypatch):
    """「引语退两次 + 越池一次」不是同一条判词连打三次，早停线不该被它触发。"""
    monkeypatch.setenv(run_mod.REJECT_STREAK_ENV, "3")

    blocks = [QUOTED_ALTERED, QUOTED_ALTERED,
              f"> {ORIGINAL}\n> —— 微博 · 等级 A [S01]\n\n越池 [S99]。\n", QUOTED_ALTERED]

    class _Rotating:
        def __init__(self):
            self.calls = 0

        async def run(self, task, ctx, on_event=None):
            block = blocks[min(self.calls, len(blocks) - 1)]
            self.calls += 1
            task.output_path.write_text(
                "这一节的正文[S01]。" * 40 + "\n\n" + block, encoding="utf-8")
            return type("R", (), {"succeeded": True, "engine_error": None})()

    outcome = asyncio.run(polish(_RecordingStore(), "r-gate", tmp_path / "runs", GATE_WORK,
                                 template="consulting", adapter=_Rotating()))
    assert outcome["attempts"] == 4, "闸换过，连续被打断，4 次跑满"
    rows = _lines(tmp_path)
    assert [r["gate"] for r in rows] == ["quote", "quote", "offpool", "quote"]
    assert [r["streak"] for r in rows] == [1, 2, 1, 1]


def test_开关读不懂就当关(monkeypatch):
    """写错一个字母就把正稿跑法改掉，比没有这个开关更坏。"""
    for raw in ("", "  ", "0", "-1", "三", "yes please"):
        monkeypatch.setenv(run_mod.REJECT_STREAK_ENV, raw)
        assert run_mod.reject_streak_limit() == 0, raw
    monkeypatch.delenv(run_mod.REJECT_STREAK_ENV, raising=False)
    assert run_mod.reject_streak_limit() == 0
    for raw, want in (("on", 3), ("true", 3), ("YES", 3), ("2", 2), ("5", 5)):
        monkeypatch.setenv(run_mod.REJECT_STREAK_ENV, raw)
        assert run_mod.reject_streak_limit() == want, raw
