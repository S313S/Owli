"""§RPT-3 货 1：UGC 编码进主链路——收尾期回填之后自动编、出稿前置兜底、编不上就拒绝出稿。

09-14 评审：编码只有脚本 `--code-only` 一个入口，没人手动跑的研究 `quotes` 进
omitted_tables，正式稿写「本轮没有可引的原声」，而原话就躺在 content_excerpt 里。
引擎一律用桩，不付调用。
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from types import SimpleNamespace

from tests.test_code1_ugc_coding import _TEXT, _CodingEngine


def _scores(total: int) -> dict:
    """五维分凑出指定总分（grade 是生成列：≥8 A、≥6 B、≥4 C）。"""
    dims = ["score_authority", "score_freshness", "score_crossref",
            "score_completeness", "score_independence"]
    values = {name: 0 for name in dims}
    for index in range(total):
        values[dims[index % 5]] += 1
    labels = ("代表性", "时效", "交叉", "完整", "无关")
    values["rating_notes"] = " · ".join(
        f"{label}{values[name]}:桩" for label, name in zip(labels, dims))
    return values


def _store(tmp_path: Path):
    """六行：池内 A / 池内 B / 池内 C / 池外 A / 池内 A 但不是 UGC / 池内 A 已编码。"""
    from app.reliability.coding import CODING_VERSION
    from tests.test_m4fork_followup import _database, _evidence

    _, store = _database(tmp_path)
    store.create_report(id="r-cd", title="编码", research_question="国内怎么看",
                        created_at="2026-09-05T00:00:00Z")
    ugc = {"content_kind": "user_opinion", "authority_kind": "anonymous_or_unverifiable"}
    spec = [  # (后缀, 总分, 角标, extra)
        ("pa", 8, 1, ugc), ("pb", 6, 2, ugc), ("pc", 4, 3, ugc),
        ("oa", 8, None, ugc), ("ind", 8, 4, {"content_kind": "industry_view"}),
        ("done", 8, 5, {**ugc, "coding": {
            "coding_version": CODING_VERSION, "audience": "学生", "scenario": "学习",
            "attitude": "正", "topics": [], "quote": ""}}),
    ]
    store.upsert_evidence_batch([
        _evidence("r-cd", suffix, permalink=f"https://xhs.example/{suffix}", platform="xhs",
                  title="评论 · 作业救星", content_excerpt=_TEXT, citation_no=mark,
                  extra=extra, **_scores(total))
        for suffix, total, mark, extra in spec
    ])
    return store


def test_引得了的口径只收池内AB级UGC(tmp_path: Path) -> None:
    from app.reliability.coding import pending_quotable, quotable_targets

    store = _store(tmp_path)
    rows = store.list_evidence("r-cd")
    assert sorted(r["id"] for r in quotable_targets(rows)) == ["ev-pa", "ev-pb"]
    assert sorted(r["id"] for r in quotable_targets(rows, force=True)) == [
        "ev-done", "ev-pa", "ev-pb"]
    assert pending_quotable(store, "r-cd") == 2


def test_主链路入口只编引得了的行并记账外费用路径(tmp_path: Path) -> None:
    from app.reliability.coding import code_quotable, is_coded

    store = _store(tmp_path)
    engine = _CodingEngine()
    result = asyncio.run(code_quotable(store, "r-cd", adapter=engine,
                                       runs_root=tmp_path / "runs"))
    assert (result.targets, result.coded, result.failed, result.already) == (3, 2, 0, 1)
    coded = {r["id"] for r in store.list_evidence("r-cd") if is_coded(r)}
    assert coded == {"ev-pa", "ev-pb", "ev-done"}, "池外、C 级、非 UGC 一条都不该编"


def test_批并发真的同时在飞_脚本默认仍串行(tmp_path: Path) -> None:
    from app.reliability.coding import code_report

    class _Slow(_CodingEngine):
        def __init__(self) -> None:
            super().__init__()
            self.live = self.peak = 0

        async def run(self, task, ctx, on_event=None):
            self.live += 1
            self.peak = max(self.peak, self.live)
            await asyncio.sleep(0.02)
            try:
                return await super().run(task, ctx, on_event)
            finally:
                self.live -= 1

    from tests.test_code1_ugc_coding import _store as many

    store = many(tmp_path, count=12)          # 双封顶切成 3 批
    serial, parallel = _Slow(), _Slow()
    asyncio.run(code_report(store, "r-cd", adapter=serial, runs_root=tmp_path / "runs",
                            force=True))
    asyncio.run(code_report(store, "r-cd", adapter=parallel, runs_root=tmp_path / "runs",
                            force=True, concurrency=3))
    assert serial.peak == 1 and parallel.peak == 3


def test_一批抛异常照旧冲出裸异常不是异常组(tmp_path: Path) -> None:
    import pytest

    from app.reliability.coding import code_report
    from tests.test_code1_ugc_coding import _store as many

    class _Boom:
        async def run(self, task, ctx, on_event=None):
            raise OSError("socket closed")

    store = many(tmp_path, count=12)
    with pytest.raises(OSError):
        asyncio.run(code_report(store, "r-cd", adapter=_Boom(), runs_root=tmp_path / "runs",
                                concurrency=3))


# ── 收尾期挂点 ──────────────────────────────────────────────────────────────

def _fake_runtime(store, tmp_path: Path, *, adapter, stopped: bool = False):
    events: list[dict] = []

    async def publish(research_id, payload):
        events.append(payload)

    return SimpleNamespace(
        store=store, runs_root=tmp_path / "runs", events=SimpleNamespace(publish=publish),
        _adapters={"r-cd": adapter}, _backfill_runs={},
        scheduler_for=lambda rid: SimpleNamespace(status="stopped" if stopped else "running"),
    ), events


def test_收尾期回填之后自动编码并发事件(tmp_path: Path) -> None:
    from app.orchestrator.runtime import RuntimeCoordinator
    from app.reliability.coding import pending_quotable

    store = _store(tmp_path)
    fake, events = _fake_runtime(store, tmp_path, adapter=_CodingEngine())
    asyncio.run(RuntimeCoordinator._code_quotes_on_finalize(fake, "r-cd"))
    assert pending_quotable(store, "r-cd") == 0
    types = [e["type"] for e in events]
    assert types[0] == "ugc_coding_started" and types[-1] == "ugc_coding_done"
    assert events[-1]["data"]["coded"] == 2 and not fake._backfill_runs
    # 挂点位置：回填之后、失败清单与 finish_report 之前。
    source = inspect.getsource(RuntimeCoordinator._finalize_if_terminal)
    assert (source.index("_backfill_ratings_on_finalize(research_id)")
            < source.index("_code_quotes_on_finalize(research_id)")
            < source.index("self.store.finish_report("))


def test_收尾期已stop或设了跳过就不编(tmp_path: Path, monkeypatch) -> None:
    from app.orchestrator.runtime import RuntimeCoordinator

    store = _store(tmp_path)
    engine = _CodingEngine()
    fake, events = _fake_runtime(store, tmp_path, adapter=engine, stopped=True)
    asyncio.run(RuntimeCoordinator._code_quotes_on_finalize(fake, "r-cd"))
    monkeypatch.setenv("OWLI_SKIP_UGC_CODING", "1")
    fake, events2 = _fake_runtime(store, tmp_path, adapter=engine)
    asyncio.run(RuntimeCoordinator._code_quotes_on_finalize(fake, "r-cd"))
    assert engine.calls == 0 and events == events2 == []


def test_收尾期编码炸了研究照常收尾只发失败事件(tmp_path: Path) -> None:
    from app.orchestrator.runtime import RuntimeCoordinator

    class _Boom:
        async def run(self, task, ctx, on_event=None):
            raise RuntimeError("引擎不可用")

    store = _store(tmp_path)
    fake, events = _fake_runtime(store, tmp_path, adapter=_Boom())
    asyncio.run(RuntimeCoordinator._code_quotes_on_finalize(fake, "r-cd"))
    assert events[-1]["type"] == "ugc_coding_failed"
    assert "引擎不可用" in events[-1]["data"]["message"]


# ── 出稿前置 ────────────────────────────────────────────────────────────────

def _polish_app(tmp_path: Path, monkeypatch, *, coding_result=None, coding_error=None):
    from app.reliability import coding
    from app.reliability.coding import CodingResult
    from tests.test_rpt1_polished_api import RESEARCH_ID, _app, _route

    app, runs_root, store = _app(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(coding, "pending_quotable", lambda store_, rid: 2)

    async def fake_code(store_, rid, **kwargs):
        calls.append("code")
        if coding_error:
            raise coding_error
        return coding_result or CodingResult(report_id=rid, targets=2, coded=2,
                                             failed=0, already=0)

    async def fake_polish(store_, research_id, runs, text, *, template=None, **kwargs):
        calls.append("polish")
        return {"status": "failed", "template": template, "path": "", "tables_path": "",
                "attempts": 0, "offpool": [], "errors": ["桩"]}

    monkeypatch.setattr(coding, "code_quotable", fake_code)
    monkeypatch.setattr("app.report.polish.run.polish", fake_polish)
    endpoint = _route(app, "/api/researches/{research_id}/export")

    async def go():
        async with app.router.lifespan_context(app):
            await endpoint(RESEARCH_ID, {"kind": "polished"})
            for _ in range(8):
                await asyncio.sleep(0)

    asyncio.run(go())
    exports = (store.get_report(RESEARCH_ID)["extra"] or {}).get("exports") or []
    return calls, exports


def test_出稿前先编码再整理(tmp_path: Path, monkeypatch) -> None:
    calls, _ = _polish_app(tmp_path, monkeypatch)
    assert calls == ["code", "polish"]


def test_一条都没编上就拒绝出稿并登记原因(tmp_path: Path, monkeypatch) -> None:
    from app.reliability.coding import CodingResult

    calls, exports = _polish_app(tmp_path, monkeypatch, coding_result=CodingResult(
        report_id="r", targets=2, coded=0, failed=2, already=0))
    assert calls == ["code"], "编码全失败时不许起写手"
    assert exports and exports[0]["url"] is None
    assert "没有原声" in exports[0]["desc"]


def test_编码抛错也拒绝出稿(tmp_path: Path, monkeypatch) -> None:
    calls, exports = _polish_app(tmp_path, monkeypatch, coding_error=RuntimeError("断流"))
    assert calls == ["code"]
    assert "断流" in exports[0]["desc"] and exports[0]["url"] is None
