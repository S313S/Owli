"""§OBS-7 货 3：源费读侧汇总 + 研究级费用接口（口径「标价折算」）。"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "app" / "store" / "schema.sql"


def _store(tmp_path: Path):
    from app.store.dao import Store

    database = tmp_path / "owli.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(SCHEMA.read_text(encoding="utf-8"))
    store = Store(database)
    store.create_report(id="r-fee", title="源费", research_question="源费汇总",
                        created_at="2026-09-13T00:00:00Z")
    return store


def _event(store, event_type: str, data: dict) -> None:
    store.append_event("r-fee", event_type=event_type,
                       payload={"type": event_type, "data": data},
                       created_at="2026-09-13T00:00:01Z")


def _seed(store) -> None:
    _event(store, "source_usage_reconciled", {
        "source": "xhs", "provider": "tikhub",
        "calls": {"search_notes": 2, "get_image_note_detail": 10}, "returned": 25})
    _event(store, "source_usage_reconciled", {
        "source": "douyin", "provider": "tikhub",
        "calls": {"video_search_v5": 1, "video_search_v4": 3, "video_comments": 6}})
    _event(store, "source_usage_reconciled", {
        "source": "douyin", "provider": "tikhub", "outcome": "unavailable",
        "calls": {"video_search_v5": 1, "video_search_v4": 1, "video_comments": 0}})
    _event(store, "source_usage_reconciled", {
        "source": "reddit", "provider": "prowlo",
        "calls": {"dataset_search": 1, "dataset_get_record": 0, "live_read": 1}})
    _event(store, "source_usage_reconciled", {
        "source": "weibo", "provider": "media_crawler", "calls": {"precollect_pool_read": 10}})
    # 老数据：付费源失败轮没带次数 → 计数缺失；池源失败不算
    _event(store, "source_unavailable", {"source": "xhs", "provider": "tikhub", "reason": "tool_unavailable"})
    _event(store, "source_unavailable", {"source": "weibo", "provider": "media_crawler"})


def test_源费按平台供应商聚合_TikHub按次计价_Prowlo只计次_池源零(tmp_path: Path) -> None:
    from app.observability.cost import source_fee_summary

    store = _store(tmp_path)
    _seed(store)
    summary = source_fee_summary(store, "r-fee")
    rows = {(row["platform"], row["provider"]): row for row in summary["rows"]}

    assert summary["basis"] == "标价折算"
    assert rows[("xhs", "tikhub")]["calls"] == 12
    assert rows[("xhs", "tikhub")]["cost_usd"] == pytest.approx(0.012)
    douyin = rows[("douyin", "tikhub")]
    assert (douyin["calls"], douyin["rounds"], douyin["failed_rounds"]) == (12, 2, 1)
    assert douyin["by_endpoint"] == {"video_search_v5": 2, "video_search_v4": 4, "video_comments": 6}
    assert rows[("reddit", "prowlo")]["calls"] == 2
    assert rows[("reddit", "prowlo")]["cost_usd"] is None
    assert rows[("weibo", "media_crawler")]["cost_usd"] == 0.0
    assert summary["total_cost_usd"] == pytest.approx(0.024)
    assert summary["rounds_without_calls"] == 1

    # 同一函数也吃裸 sqlite 连接（读数尺子走这条）
    with sqlite3.connect(tmp_path / "owli.db") as connection:
        assert source_fee_summary(connection, "r-fee")["calls"] == summary["calls"]


def test_研究费用接口合出模型费与源费(tmp_path: Path) -> None:
    from app.api.main import create_app

    application = create_app(tmp_path / "owli.db", SCHEMA, engine_probe=lambda: {},
                             runs_root=tmp_path / "runs")

    async def scenario():
        async with application.router.lifespan_context(application):
            store = application.state.store
            store.create_report(id="r-fee", title="源费", research_question="源费汇总",
                                created_at="2026-09-13T00:00:00Z")
            store.ensure_chapters("r-fee", [{"goal_id": "goal-1", "chapter_id": "ch-1"}],
                                  updated_at="2026-09-13T00:00:01Z")
            store.record_chapter_usage("r-fee", "goal-1", "ch-1", {"output_tokens": 1000, "cost_usd": 0.02},
                                       engine="codex", cost_source="estimated")
            _seed(store)
            transport = httpx.ASGITransport(app=application)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                ok = await client.get("/api/researches/r-fee/cost")
                missing = await client.get("/api/researches/r-none/cost")
            return ok, missing

    ok, missing = asyncio.run(scenario())
    assert missing.status_code == 404
    data = ok.json()["data"]
    assert data["basis"] == "标价折算"
    assert data["llm"]["estimated_calls"] == 1 and data["llm"]["uncosted_calls"] == 0
    assert data["llm"]["by_engine"]["codex"]["estimated_cost_usd"] == pytest.approx(0.02)
    assert data["sources"]["total_cost_usd"] == pytest.approx(0.024)
    assert data["total_cost_usd"] == pytest.approx(0.044)
