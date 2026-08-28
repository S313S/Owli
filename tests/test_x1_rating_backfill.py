"""§X-1 货 1：收尾无条件跑可靠度回填（评级链必跑）。判据落库：score_crossref / rated_by / events。"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from tests.test_c1_claims import add_evidence, make_store, raw_claim, ref
from tests.test_m3h_finalize import _plan, _write


class LabelAuditor:
    """假评级引擎：按输入逐条写闭集标签，走 agent:reliability-auditor@claude 路径。"""

    def __init__(self) -> None:
        self.calls = 0

    async def run(self, task, ctx, on_event=None):
        del ctx, on_event
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


def test_环境开关可跳过且默认跑(tmp_path, monkeypatch):
    monkeypatch.setenv("OWLI_SKIP_RATING_BACKFILL", "1")
    auditor = LabelAuditor()
    store, events, _ = _finalize(tmp_path, monkeypatch, adapter=auditor)
    assert auditor.calls == 0
    assert _events_of(events, "reliability_backfill_skipped")[0]["reason"] == "env_skip"
    assert store.get_report("r-ledger")["status"] == "completed"
