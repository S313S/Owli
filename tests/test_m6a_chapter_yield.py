"""§M6-a 货 3：给 `closing.expected_count` 加消费点——每采集章「要 N 条 vs 到几条」。

此前它只在 `plan/chapters.py` 校验类型、全项目零读取处，「每源出货 ≥N」没有尺子。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from app.orchestrator.runtime import RuntimeCoordinator
from app.plan.model import Plan
from app.store.dao import Store
from app.store.schema import initialize_database_if_empty
from tests.plan_factory import make_plan_dict

RESEARCH_ID = "r-m6a-yield"


def _collection_plan(*, expected: int) -> Plan:
    raw = make_plan_dict()
    raw["research_id"] = RESEARCH_ID
    agent = raw["goals"][0]["agents"][0]
    agent["capability"]["sources"] = ["xhs"]
    agent["chapter"]["chapter_type"] = "collection"
    agent["chapter"]["closing"]["entities"] = ["竞品甲"]
    agent["chapter"]["closing"]["expected_count"] = expected
    return Plan.from_dict(raw)


def _coordinator(tmp_path: Path, published: list) -> tuple[RuntimeCoordinator, Store]:
    database = tmp_path / "owli.db"
    initialize_database_if_empty(database, Path("app/store/schema.sql"))
    store = Store(database)
    store.create_report(
        id=RESEARCH_ID, title="出货对账", research_question="?",
        use_case="product_competitor", status="running",
        created_at="2026-09-01T00:00:00+00:00",
    )

    async def publish(research_id, payload):
        published.append(payload)

    return RuntimeCoordinator(
        store=store, event_buffer=SimpleNamespace(publish=publish), researches={},
        cards={}, runs_root=tmp_path / "runs", auto_confirm=True,
        routing_utc_clock=lambda: datetime(2026, 9, 1, tzinfo=timezone.utc),
    ), store


def _seed(store: Store, count: int, *, agent_name: str | None) -> None:
    for index in range(count):
        store.add_evidence(
            id=f"ev-{index}", report_id=RESEARCH_ID, goal_id="goal-1",
            platform="xhs", permalink=f"https://xhs.example/{index}",
            fetched_at="2026-09-01T00:00:00+00:00", title=f"笔记 {index}",
            content_excerpt="正文", author_name="作者", agent_name=agent_name,
        )


def test_采集章实采不足计划数时对账出缺口(tmp_path):
    coordinator, store = _coordinator(tmp_path, [])
    _seed(store, 3, agent_name="agent-1")

    summary = coordinator._source_yield_summary(
        RESEARCH_ID, _collection_plan(expected=5),
    )

    assert summary["chapters"] == [{
        "goal_id": "goal-1", "agent_id": "agent-1", "sources": ["xhs"],
        "entity": None, "expected": 5, "yielded": 3, "gap": 2,
    }]


def test_采集章足量时缺口为零(tmp_path):
    coordinator, store = _coordinator(tmp_path, [])
    _seed(store, 5, agent_name="agent-1")

    summary = coordinator._source_yield_summary(
        RESEARCH_ID, _collection_plan(expected=5),
    )

    assert [item["gap"] for item in summary["chapters"]] == [0]


def test_章归属为空的行算不到章头上(tmp_path):
    """货 2 之前的直落库源写不下 agent_name；这些行不静默、表现为缺口。"""
    coordinator, store = _coordinator(tmp_path, [])
    _seed(store, 5, agent_name=None)

    summary = coordinator._source_yield_summary(
        RESEARCH_ID, _collection_plan(expected=5),
    )

    assert summary["yielded"] == {"xhs": 5}
    assert summary["chapters"][0]["yielded"] == 0
    assert summary["chapters"][0]["gap"] == 5


def test_只有缺口的章发事件且载荷带缺口(tmp_path):
    published: list = []
    coordinator, store = _coordinator(tmp_path, published)
    chapters = [
        {"goal_id": "goal-1", "agent_id": "agent-1", "sources": ["xhs"],
         "entity": None, "expected": 5, "yielded": 3, "gap": 2},
        {"goal_id": "goal-2", "agent_id": "agent-2", "sources": ["web_search"],
         "entity": None, "expected": 3, "yielded": 3, "gap": 0},
    ]

    asyncio.run(coordinator._publish_chapter_yield_shortfalls(RESEARCH_ID, chapters))

    assert [item["type"] for item in published] == ["chapter_yield_shortfall"]
    assert published[0]["data"] == {"research_id": RESEARCH_ID, **chapters[0]}


def test_非采集章与未声明期望条数的章不进对账(tmp_path):
    coordinator, store = _coordinator(tmp_path, [])
    plan = _collection_plan(expected=5)
    plan.goals[0].agents[0].chapter["closing"]["expected_count"] = None

    assert coordinator._source_yield_summary(RESEARCH_ID, plan)["chapters"] == []
