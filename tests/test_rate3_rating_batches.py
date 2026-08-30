"""§RATE-3 货 1：切片发生在物化那一刻——每片 ≤50 行写成 `x.rows.<n>.json`。

RATE-2 实测：fast 档 330 s 墙钟下 130 行的评级章算术上过不去（4.4 s/条），
本机代理还把单次流式响应掐在 5 分钟——「一次调用评 130 条」这条路本身是死的。
判据：135 行 → 3 片（50/50/35）；片数与每片行数进 `rating_rows_materialized` 事件。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from tests.test_c1_claims import make_store
from tests.test_rate2_materialize_rows import _collector, _rating


def test_按批大小切片_135行切成50_50_35() -> None:
    from app.plan.model import rating_batch_output_path, rating_batch_path, rating_batches

    assert rating_batches(135) == [50, 50, 35]
    assert rating_batches(50) == [50]
    assert rating_batches(25) == [25]
    assert rating_batches(0) == []
    assert rating_batches(130, 30) == [30, 30, 30, 30, 10]
    assert rating_batch_path("goals/goal-1/data-collection.rows.json", 2) == (
        "goals/goal-1/data-collection.rows.2.json"
    )
    assert rating_batch_output_path("goals/goal-1/reliability-audit.json", 3) == (
        "goals/goal-1/reliability-audit.part.3.json"
    )


def _fixture(tmp_path: Path, rows: int, collector_id: str = "data-collection-2"):
    from app.orchestrator.runtime import RuntimeCoordinator

    store = make_store(tmp_path, "r-rate3")
    for index in range(rows):
        store.add_evidence(
            id=f"ev-{index}", report_id="r-rate3", goal_id="goal-1",
            agent_name=collector_id, platform="web_search",
            permalink=f"https://example.com/{index}",
            fetched_at="2026-08-31T00:00:00+00:00",
            published_at="2026-08-20T00:00:00+00:00",
            title=f"标题{index}", content_excerpt="可复核正文",
            author_name=f"作者{index}", fetch_method="search_index",
        )
    events: list[dict] = []

    async def publish(research_id, payload):
        events.append(payload)

    coordinator = RuntimeCoordinator(
        store=store, event_buffer=SimpleNamespace(publish=publish), researches={},
        cards={}, runs_root=tmp_path / "runs",
        routing_utc_clock=lambda: datetime(2026, 8, 31, tzinfo=timezone.utc),
    )
    collector, rating = _collector(collector_id), _rating(collector_id)
    goal = SimpleNamespace(
        goal_id="goal-1", agents=[collector, rating],
        deliverable={"path": "goals/goal-1/result.md"},
    )
    plan = SimpleNamespace(research_id="r-rate3", goals=[goal])
    artifact = tmp_path / "runs" / "r-rate3" / "goals" / "goal-1"
    return coordinator, plan, rating, events, artifact


def test_物化时切片_135行写成3片_事件带片数与每片行数(tmp_path: Path) -> None:
    coordinator, plan, rating, events, artifact = _fixture(tmp_path, 135)

    written = asyncio.run(coordinator._materialize_rating_rows(plan, "goal-1", rating))

    assert written == 135
    sizes = [
        len(json.loads((artifact / f"data-collection-2.rows.{n}.json").read_text("utf-8")))
        for n in (1, 2, 3)
    ]
    assert sizes == [50, 50, 35]
    assert not (artifact / "data-collection-2.rows.4.json").exists()
    # 片是整文件的顺序切分：拼回去就是整文件，不去重、不改写。
    whole = json.loads((artifact / "data-collection-2.rows.json").read_text("utf-8"))
    pieces = [
        item for n in (1, 2, 3)
        for item in json.loads(
            (artifact / f"data-collection-2.rows.{n}.json").read_text("utf-8")
        )
    ]
    assert pieces == whole
    data = next(e["data"] for e in events if e["type"] == "rating_rows_materialized")
    assert data["batches"] == 3 and data["batch_rows"] == [50, 50, 35]
    assert data["batch_size"] == 50 and data["rows"] == 135


def test_重物化时行数变少_多出来的旧片要删掉(tmp_path: Path) -> None:
    coordinator, plan, rating, _, artifact = _fixture(tmp_path, 60)
    artifact.mkdir(parents=True, exist_ok=True)
    for n in (3, 4):
        (artifact / f"data-collection-2.rows.{n}.json").write_text("[]", "utf-8")
        (artifact / f"reliability-audit.part.{n}.json").write_text("[]", "utf-8")

    asyncio.run(coordinator._materialize_rating_rows(plan, "goal-1", rating))

    assert (artifact / "data-collection-2.rows.2.json").is_file()
    for n in (3, 4):
        assert not (artifact / f"data-collection-2.rows.{n}.json").exists()
        assert not (artifact / f"reliability-audit.part.{n}.json").exists()


def test_批大小可由环境变量下调_非法值回默认(monkeypatch) -> None:
    from app.orchestrator.runtime import RuntimeCoordinator

    monkeypatch.setenv("OWLI_RATING_BATCH_ROWS", "30")
    assert RuntimeCoordinator._rating_batch_rows() == 30
    monkeypatch.setenv("OWLI_RATING_BATCH_ROWS", "abc")
    assert RuntimeCoordinator._rating_batch_rows() == 50
    monkeypatch.setenv("OWLI_RATING_BATCH_ROWS", "0")
    assert RuntimeCoordinator._rating_batch_rows() == 50
