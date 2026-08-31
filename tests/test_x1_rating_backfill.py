"""§X-1 货 1：收尾无条件跑可靠度回填（评级链必跑）。判据落库：score_crossref / rated_by / events。"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from tests.test_c1_claims import (
    add_evidence, answer_firsthand, make_store, raw_claim, ref,
)
from tests.test_m3h_finalize import _plan, _write


class LabelAuditor:
    """假评级引擎：按输入逐条写闭集标签，走 agent:reliability-auditor@claude 路径。"""

    def __init__(self) -> None:
        self.calls = 0

    async def run(self, task, ctx, on_event=None):
        del ctx, on_event
        # §XSEM-1 条 1 的一手性审计走另一条提示词；本类的 calls 只计评级调用，
        # 免得「只有源基线的行必须走引擎」这条断言被审计调用顶成假绿。
        answered = answer_firsthand(task)
        if answered is not None:
            return answered
        self.calls += 1
        inputs, _ = json.JSONDecoder().raw_decode(task.body.split("输入证据：", 1)[1])
        task.output_path.parent.mkdir(parents=True, exist_ok=True)
        task.output_path.write_text(json.dumps([{
            "id": item["id"], "authority_kind": "named_secondary",
            "content_kind": "industry_view", "interest_relation": "arms_length",
            "missing_dimensions": {},
        } for item in inputs], ensure_ascii=False), encoding="utf-8")
        return SimpleNamespace(succeeded=True)


class ExplodingAuditor:
    async def run(self, task, ctx, on_event=None):
        raise RuntimeError("引擎断连：socket connection was closed unexpectedly")


def _prepare(store) -> None:
    """两条证据：ev-a 已有闭集标签（本地重算路），ev-b 只有源基线（要引擎）；一条断言把两条串成簇。"""
    from app.reliability.claims import register_claims

    urls = ["https://example.com/a", "https://example.org/b"]
    add_evidence(store, "r-ledger", "ev-a", platform="web_search", permalink=urls[0], author="甲")
    store.add_evidence(
        id="ev-b", report_id="r-ledger", goal_id="goal-1", platform="xhs",
        permalink=urls[1], fetched_at="2026-08-27T00:00:00+00:00",
        published_at="2026-08-20T00:00:00+00:00", source_type="post",
        fetch_method="official_api", title="ev-b 标题", content_excerpt="正文",
        author_name="乙", extra={}, rated_by="baseline:xhs@v1",
    )
    register_claims(store, "r-ledger", [raw_claim("c-01", [ref(urls[0]), ref(urls[1])])], source="chapter")


def _finalize(tmp_path: Path, monkeypatch, *, adapter, prepare=_prepare):
    from app.orchestrator import runtime as runtime_module
    from app.orchestrator.runtime import RuntimeCoordinator

    plan = _plan(report_format="markdown")
    store = make_store(tmp_path, "r-ledger")
    prepare(store)
    _write(tmp_path / "runs" / "r-ledger" / "goals" / "goal-3" / "report.md",
           "# 结论\n\n- 评级链必跑。\n\n# 信息源\n\n- 无。\n")
    events: list[dict] = []
    finish_calls: list[dict] = []
    original_finish = store.finish_report

    def finish_report(report_id, **kwargs):
        finish_calls.append({"n_events": len(events), **kwargs})
        return original_finish(report_id, **kwargs)

    monkeypatch.setattr(store, "finish_report", finish_report)
    monkeypatch.setattr(runtime_module, "load_plan", lambda store_, rid: plan)

    async def publish(research_id, payload):
        events.append(payload)

    coordinator = RuntimeCoordinator(
        store=store, event_buffer=SimpleNamespace(publish=publish), researches={},
        cards={}, runs_root=tmp_path / "runs", auto_confirm=True,
        routing_utc_clock=lambda: datetime(2026, 8, 29, tzinfo=timezone.utc),
    )
    coordinator.researches["r-ledger"] = coordinator._state_from_plan(plan)
    coordinator._schedulers["r-ledger"] = SimpleNamespace(
        status="completed", goal_statuses={"goal-1": "done", "goal-2": "done", "goal-3": "done"},
    )
    if adapter is not None:
        coordinator._adapters["r-ledger"] = adapter
    asyncio.run(coordinator._finalize_if_terminal("r-ledger"))
    return store, events, finish_calls


def _events_of(events, kind):
    return [event["data"] for event in events if event.get("type") == kind]


def test_收尾无条件回填_交叉维出真值且rated_by带回填标记(tmp_path, monkeypatch):
    auditor = LabelAuditor()
    store, events, finish_calls = _finalize(tmp_path, monkeypatch, adapter=auditor)

    rows = {row["id"]: row for row in store.list_evidence("r-ledger")}
    assert auditor.calls >= 1, "只有源基线的行必须走引擎"
    assert sum(row["score_crossref"] is not None for row in rows.values()) > 0
    assert rows["ev-a"]["rated_by"] == "rule:reliability-backfill@v1"
    assert rows["ev-b"]["rated_by"] == "agent:reliability-auditor@claude"
    done = _events_of(events, "reliability_backfill_done")
    assert len(done) == 1 and not _events_of(events, "reliability_backfill_failed")
    assert done[0]["failed"] == 0 and done[0]["rated"] == 2
    assert done[0]["crossref_rated"] > 0
    assert set(done[0]["provenance"]) == {
        "rule:reliability-backfill@v1", "agent:reliability-auditor@claude",
    }
    assert store.get_report("r-ledger")["status"] == "completed"
    # 先后顺序：finish_report 只由收尾调一次，且晚于回填事件；backfill 没抢先 finish。
    assert len(finish_calls) == 1 and finish_calls[0]["report_path"]
    done_index = next(i for i, e in enumerate(events) if e.get("type") == "reliability_backfill_done")
    assert finish_calls[0]["n_events"] > done_index


def test_回填抛错只发事件_研究仍completed(tmp_path, monkeypatch):
    store, events, finish_calls = _finalize(tmp_path, monkeypatch, adapter=ExplodingAuditor())

    failed = _events_of(events, "reliability_backfill_done") + _events_of(events, "reliability_backfill_failed")
    assert len(failed) == 1
    assert store.get_report("r-ledger")["status"] == "completed"
    assert len(finish_calls) == 1
    # 引擎抛错整包中止：评分保持 NULL、不写假值；研究照常 completed，不判 failed。
    rows = {row["id"]: row for row in store.list_evidence("r-ledger")}
    assert rows["ev-b"]["score_crossref"] is None


def test_rate1货5_未设环境变量时默认跑兜底回填(tmp_path, monkeypatch):
    """§RATE-1 货 5：写作前评级落地后默认改回开——它此时只补没评到的那部分。"""
    monkeypatch.delenv("OWLI_SKIP_RATING_BACKFILL", raising=False)
    auditor = LabelAuditor()
    store, events, _ = _finalize(tmp_path, monkeypatch, adapter=auditor)
    assert not _events_of(events, "reliability_backfill_skipped")
    assert len(_events_of(events, "reliability_backfill_done")) == 1
    assert store.get_report("r-ledger")["status"] == "completed"


def test_rate1货0_显式1仍是env_skip(tmp_path, monkeypatch):
    monkeypatch.setenv("OWLI_SKIP_RATING_BACKFILL", "1")
    auditor = LabelAuditor()
    store, events, _ = _finalize(tmp_path, monkeypatch, adapter=auditor)
    assert auditor.calls == 0
    assert _events_of(events, "reliability_backfill_skipped")[0]["reason"] == "env_skip"
    assert store.get_report("r-ledger")["status"] == "completed"


def test_rate1货5_显式0也跑回填(tmp_path, monkeypatch):
    monkeypatch.setenv("OWLI_SKIP_RATING_BACKFILL", "0")
    auditor = LabelAuditor()
    _, events, _ = _finalize(tmp_path, monkeypatch, adapter=auditor)
    assert auditor.calls >= 1
    assert not _events_of(events, "reliability_backfill_skipped")
    assert len(_events_of(events, "reliability_backfill_done")) == 1


def _backfill_store(tmp_path: Path, *, pending: int, reusable: int):
    from tests.test_m4fork_followup import _database, _evidence

    _, store = _database(tmp_path)
    store.create_report(id="r-batch", title="切批", research_question="引擎调用次数",
                        created_at="2026-08-29T00:00:00Z")
    labels = {"authority_kind": "named_secondary", "content_kind": "industry_view",
              "interest_relation": "arms_length"}
    # 待评行按 id 顺序打散在可复用行之间（重放里就是这种分布）；
    # 待评行若排在一起，旧切法也只调 2 次，测不出问题。
    pending_idx = {i * 7 for i in range(pending)}
    rows = []
    for i in range(pending + reusable):
        extra = {} if i in pending_idx else {
            "claim_ids": ["c-1"], "crossref_n_clusters": 1, "crossref_verdict": "SINGLE", **labels,
        }
        rows.append(_evidence("r-batch", f"{i:03d}", goal_id="goal-1",
                              permalink=f"https://e.example/{i}", extra=extra))
    store.upsert_evidence_batch(rows)
    return store, sorted(pending_idx)


def test_货1b_切批只按要进引擎的行_26待评165可复用最多两次调用(tmp_path: Path) -> None:
    from app.reliability.backfill import backfill_report
    from tests.test_m4fork_followup import BackfillEngine

    store, pending_idx = _backfill_store(tmp_path, pending=26, reusable=165)
    engine = BackfillEngine()
    result = asyncio.run(backfill_report(store, "r-batch", adapter=engine, runs_root=tmp_path / "runs"))

    assert engine.calls <= 2, f"26 条待评不该散成 {engine.calls} 次调用"
    assert result.rated == 191 and result.failed == 0
    rows = {row["id"]: row for row in store.list_evidence("r-batch")}
    assert all(rows[f"ev-{i:03d}"]["rated_by"] == "agent:reliability-auditor@claude" for i in pending_idx)
    assert rows["ev-001"]["rated_by"] == "rule:reliability-backfill@v1"
    assert all(row["score_authority"] is not None for row in rows.values())


def test_货1b_没有待评行时零次引擎调用(tmp_path: Path) -> None:
    from app.reliability.backfill import backfill_report
    from tests.test_m4fork_followup import BackfillEngine

    store, _ = _backfill_store(tmp_path, pending=0, reusable=30)
    engine = BackfillEngine()
    result = asyncio.run(backfill_report(store, "r-batch", adapter=engine, runs_root=tmp_path / "runs"))

    assert engine.calls == 0 and result.rated == 30 and result.failed == 0
