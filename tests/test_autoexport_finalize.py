"""§AUTO-EXP 货 3：completed 自动出 Excel / 推飞书；失败只发事件不拖垮 completed。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from tests.test_dlv1_delivery import NOTES
from tests.test_m3h_finalize import _plan, _write
from tests.test_m3h_ledger import _store

REPORT_MD = (
    "# 结论\n\n- 自动导出语义 [S01]\n\n# 信息源\n\n"
    "1. [标题](https://example.com/a)\n\n# 缺失清单\n\n- 无\n"
)


def _coordinator(tmp_path: Path, monkeypatch):
    from app.orchestrator import runtime as runtime_module
    from app.orchestrator.runtime import RuntimeCoordinator

    plan = _plan(report_format="markdown")
    _write(tmp_path / "runs" / "r-ledger" / "goals" / "goal-3" / "report.md", REPORT_MD)
    store = _store(tmp_path)
    store.add_evidence(
        id="ev-1", report_id="r-ledger", platform="xhs", permalink="https://example.com/a",
        fetched_at="2026-08-28T08:22:50Z", title="标题", goal_id="goal-1",
        fetch_method="third_party_api", content_excerpt="摘要",
        score_authority=1, score_freshness=2, score_crossref=None,
        score_completeness=1, score_independence=1, rating_notes=NOTES,
        rated_by="baseline:xhs@v1", raw_metrics={"digg_count": 3}, citation_no=1,
    )
    events: list[dict] = []

    async def publish(research_id, payload):
        events.append(payload)

    monkeypatch.setattr(runtime_module, "load_plan", lambda store_, rid: plan)
    coordinator = RuntimeCoordinator(
        store=store, event_buffer=SimpleNamespace(publish=publish), researches={}, cards={},
        runs_root=tmp_path / "runs", auto_confirm=True,
        routing_utc_clock=lambda: datetime(2026, 8, 31, tzinfo=timezone.utc),
    )
    coordinator.researches["r-ledger"] = coordinator._state_from_plan(plan)
    coordinator._schedulers["r-ledger"] = SimpleNamespace(
        status="completed",
        goal_statuses={"goal-1": "done", "goal-2": "done", "goal-3": "done"},
    )
    return coordinator, store, events


def _run_finalize(coordinator) -> None:
    async def scenario() -> None:
        await coordinator._finalize_if_terminal("r-ledger")
        if coordinator._drive_watchers:  # 飞书后台任务要等它落完事件再断言
            await asyncio.gather(*list(coordinator._drive_watchers))

    asyncio.run(scenario())


def test_completed_自动出excel_八项过_且事件在完成之后(tmp_path: Path, monkeypatch) -> None:
    from app.export.excel_check import check_workbook

    monkeypatch.setenv("OWLI_ENV_FILE", str(tmp_path / "no.env"))
    monkeypatch.delenv("OWLI_AUTO_EXPORT", raising=False)
    coordinator, store, events = _coordinator(tmp_path, monkeypatch)
    _run_finalize(coordinator)

    xlsx = tmp_path / "runs" / "r-ledger" / "exports" / "r-ledger.xlsx"
    assert xlsx.is_file() and check_workbook(xlsx) == []
    exports = store.get_report("r-ledger")["extra"]["exports"]
    assert len(exports) == 1 and exports[0]["kind"] == "excel"
    types = [e.get("type") for e in events]
    completed_at = max(i for i, e in enumerate(events)
                       if e.get("type") == "research_update" and e["data"]["status"] == "completed")
    assert types.index("export_done") > completed_at  # finish 先落、导出在后（判据 1）
    assert "feishu_sync_started" not in types  # 默认开关只有 excel


def test_excel导出失败_只发事件_completed不回退(tmp_path: Path, monkeypatch) -> None:
    import app.export.excel as excel_module

    monkeypatch.setenv("OWLI_ENV_FILE", str(tmp_path / "no.env"))
    monkeypatch.delenv("OWLI_AUTO_EXPORT", raising=False)

    def boom(*args, **kwargs):
        raise RuntimeError("openpyxl 崩了")

    monkeypatch.setattr(excel_module, "export_excel", boom)
    coordinator, store, events = _coordinator(tmp_path, monkeypatch)
    _run_finalize(coordinator)

    failed = [e for e in events if e.get("type") == "export_failed"]
    assert failed and failed[0]["data"]["kind"] == "excel" and failed[0]["is_error"]
    assert store.get_report("r-ledger")["status"] == "completed"  # 判据 4


def test_feishu走后台线程_落synced事件与四列(tmp_path: Path, monkeypatch) -> None:
    import app.export.feishu as feishu_module

    monkeypatch.setenv("OWLI_ENV_FILE", str(tmp_path / "no.env"))
    monkeypatch.setenv("OWLI_AUTO_EXPORT", "excel,feishu")
    calls: list[tuple] = []

    def fake_push(store, research_id, report_text):
        calls.append((research_id, report_text))
        return {"kind": "feishu", "status": "synced", "message": "已推送"}

    monkeypatch.setattr(feishu_module, "push_to_feishu", fake_push)
    coordinator, store, events = _coordinator(tmp_path, monkeypatch)
    _run_finalize(coordinator)

    assert calls and calls[0][0] == "r-ledger"
    types = [e.get("type") for e in events]
    assert types.index("feishu_sync_started") < types.index("feishu_sync_finished")
    finished = next(e for e in events if e.get("type") == "feishu_sync_finished")
    assert finished["data"]["status"] == "synced" and not finished["is_error"]
    assert store.get_report("r-ledger")["status"] == "completed"


def test_feishu线程炸了_finished事件记failed_不冒泡(tmp_path: Path, monkeypatch) -> None:
    import app.export.feishu as feishu_module

    monkeypatch.setenv("OWLI_ENV_FILE", str(tmp_path / "no.env"))
    monkeypatch.setenv("OWLI_AUTO_EXPORT", "feishu")

    def boom(store, research_id, report_text):
        raise RuntimeError("传输层意外")

    monkeypatch.setattr(feishu_module, "push_to_feishu", boom)
    coordinator, store, events = _coordinator(tmp_path, monkeypatch)
    _run_finalize(coordinator)  # 不抛 = 异常没冒泡到收尾调用方（判据 4）

    finished = next(e for e in events if e.get("type") == "feishu_sync_finished")
    assert finished["data"]["status"] == "failed" and finished["is_error"]
    assert store.get_report("r-ledger")["status"] == "completed"
