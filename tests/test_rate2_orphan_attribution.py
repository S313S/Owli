"""§RATE-2 货 2：给没有章归属的库行推断归属（用户拍板 A + 历史行推断）。

底料实证（`r-28c6d778f810` goal-2，三个小红书采集章 02:38:18 **同时**起跑）：
209 行 `agent_name` 为空，时间区间分不开（章执行区间彼此重叠、调用交错），
但「这条是用什么词搜出来的」（`source_keyword`）与章的 `entity` 逐条对得上：
云鲸 105 行 → data-collection-6、石头 104 行 → data-collection-4。
另一把独立的尺（源调用次数事件按章 flush：1 / 7 / 7 次）算出 dc-4 还差 3 批、
dc-6 还差 6 批，与关键词规则给出的批次数逐批对上——所以「归错 0」是可核实的。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from tests.test_c1_claims import make_store

_T4 = "2026-08-30T02:40:11.806895+00:00"
_T6 = "2026-08-30T02:39:15.894654+00:00"


def _collector(agent_id: str, entity: str, sources: list[str]) -> SimpleNamespace:
    return SimpleNamespace(
        agent_id=agent_id, entity=entity,
        capability={"profile": "web-collector", "sources": sources},
        output={"path": f"goals/goal-2/{agent_id}.json"}, depends_on=[],
    )


def _rating(collector_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        agent_id=f"reliability-audit-{collector_id[-1]}",
        capability={"profile": "readonly-analyst", "sources": []},
        output={
            "path": f"goals/goal-2/audit-{collector_id[-1]}.json", "format": "json",
            "validators": ["file_exists", "no_item_missing_rating"],
        },
        depends_on=[collector_id],
    )


def _add(store, evidence_id: str, **kwargs) -> None:
    payload = {
        "id": evidence_id, "report_id": "r-rate2", "goal_id": "goal-2",
        "platform": "xhs", "permalink": f"https://xhs.example.com/{evidence_id}",
        "fetched_at": _T6, "title": "笔记", "fetch_method": "third_party_api",
    }
    payload.update(kwargs)
    store.add_evidence(**payload)


def _fixture(tmp_path: Path):
    from app.orchestrator.runtime import RuntimeCoordinator

    store = make_store(tmp_path, "r-rate2")
    # 投影覆盖过的行：有归属，且和一批无归属行同一次源调用（同 fetched_at）
    _add(store, "ev-owned", agent_name="data-collection-4", fetched_at=_T4,
         source_keyword="石头扫地机器人 售后")
    _add(store, "ev-batch", fetched_at=_T4, source_keyword="")          # 跟随批次
    _add(store, "ev-keyword", source_keyword="云鲸扫地机器人 耗材更换")   # 按 entity
    # 判不出：它自己一批（没有同批次的已归属行），关键词也对不上任何 entity
    _add(store, "ev-blind", fetched_at="2026-08-30T02:39:33.683881+00:00",
         source_keyword="扫地机器人 售后")
    _add(store, "ev-web", platform="web_search", fetch_method="search_index",
         source_keyword="扫地机器人 口碑")                               # 平台只此一章

    events: list[dict] = []

    async def publish(research_id, payload):
        events.append(payload)

    coordinator = RuntimeCoordinator(
        store=store, event_buffer=SimpleNamespace(publish=publish), researches={},
        cards={}, runs_root=tmp_path / "runs",
        routing_utc_clock=lambda: datetime(2026, 8, 30, tzinfo=timezone.utc),
    )
    agents = [
        _collector("data-collection-4", "石头扫地机器人", ["xhs"]),
        _collector("data-collection-6", "云鲸扫地机器人", ["xhs"]),
        _collector("data-collection-7", "扫地机器人", ["web_search"]),
        _rating("data-collection-4"),
    ]
    goal = SimpleNamespace(
        goal_id="goal-2", agents=agents,
        deliverable={"path": "goals/goal-2/result.md"},
    )
    plan = SimpleNamespace(research_id="r-rate2", goals=[goal])
    return coordinator, store, plan, agents[-1], events


def test_三条规则各归各的_判不出的留空不猜(tmp_path: Path) -> None:
    coordinator, store, plan, rating, events = _fixture(tmp_path)

    asyncio.run(coordinator._materialize_rating_rows(plan, "goal-2", rating))

    rows = {row["id"]: row for row in store.list_evidence("r-rate2")}
    assert len(rows) == 5, "推断只改 agent_name，不许插新行（D-015 的教训）"
    assert rows["ev-web"]["agent_name"] == "data-collection-7"
    assert rows["ev-web"]["extra"]["agent_name_inferred"] == "sole_collector"
    # 一次源调用的整批必属同一章：同 fetched_at 里已有归属行就跟随它
    assert rows["ev-batch"]["agent_name"] == "data-collection-4"
    assert rows["ev-batch"]["extra"]["agent_name_inferred"] == "same_batch"
    assert rows["ev-keyword"]["agent_name"] == "data-collection-6"
    assert rows["ev-keyword"]["extra"]["agent_name_inferred"] == "entity_keyword"
    # 两个 xhs 章的 entity 都对不上这条关键词 → 留空，不猜
    assert rows["ev-blind"]["agent_name"] is None
    assert "agent_name_inferred" not in (rows["ev-blind"]["extra"] or {})
    # 已有归属的行不被改写
    assert rows["ev-owned"]["agent_name"] == "data-collection-4"
    assert "agent_name_inferred" not in (rows["ev-owned"]["extra"] or {})

    data = next(
        e["data"] for e in events if e["type"] == "evidence_chapter_inferred"
    )
    assert data["inferred"] == 3
    assert data["rules"] == {
        "sole_collector": 1, "same_batch": 1, "entity_keyword": 1,
    }


def test_补上归属的行随即被物化进那一章(tmp_path: Path) -> None:
    import json

    coordinator, _, plan, rating, _ = _fixture(tmp_path)

    written = asyncio.run(
        coordinator._materialize_rating_rows(plan, "goal-2", rating)
    )

    path = (
        coordinator.runs_root / "r-rate2" / "goals" / "goal-2"
        / "data-collection-4.rows.json"
    )
    ids = {row["permalink"] for row in json.loads(path.read_text("utf-8"))}
    assert written == 2, "ev-owned + 跟随批次补上的 ev-batch"
    assert ids == {
        "https://xhs.example.com/ev-owned", "https://xhs.example.com/ev-batch",
    }


def test_推断是幂等的_跑两遍不重复计数(tmp_path: Path) -> None:
    coordinator, store, plan, rating, events = _fixture(tmp_path)

    asyncio.run(coordinator._materialize_rating_rows(plan, "goal-2", rating))
    asyncio.run(coordinator._materialize_rating_rows(plan, "goal-2", rating))

    assert len(store.list_evidence("r-rate2")) == 5
    inferred = [e for e in events if e["type"] == "evidence_chapter_inferred"]
    assert len(inferred) == 1, "第二遍已无无归属行可补，不再发事件"
