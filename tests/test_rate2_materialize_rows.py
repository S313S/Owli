"""§RATE-2 货 1：评级章起跑前把「这一章采到的库行」物化成文件给它读。

RATE-1 整跑的根因：源适配器直落库，采集产物只是模型顺手写下的一小撮
（盘上 10 条 / 库里同章 50 行），评级章读产物 → 只覆盖 15%。
判据：物化文件条数 = 该章库行数；评级章 inputs 指物化文件而不是采集产物。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from tests.test_c1_claims import make_store


def _collector(agent_id: str = "data-collection") -> SimpleNamespace:
    return SimpleNamespace(
        agent_id=agent_id,
        capability={"profile": "web-collector", "sources": ["web_search"]},
        output={"path": f"goals/goal-1/{agent_id}.json"},
        depends_on=[],
        task="采集口碑证据",
    )


def _rating(collector_id: str = "data-collection") -> SimpleNamespace:
    return SimpleNamespace(
        agent_id="reliability-audit",
        capability={"profile": "readonly-analyst", "sources": []},
        output={
            "path": "goals/goal-1/reliability-audit.json",
            "format": "json",
            "validators": ["file_exists", "no_item_missing_rating"],
        },
        depends_on=[collector_id],
        task="逐条给五维可靠度评分",
    )


def test_评级章inputs指物化行文件_不再指采集产物() -> None:
    from app.plan.chapters import rating_chapter_value

    collector, rating = _collector(), _rating()
    goal = SimpleNamespace(
        goal_id="goal-1", agents=[collector, rating],
        deliverable={"path": "goals/goal-1/result.md"},
    )

    value = rating_chapter_value(rating, goal)

    assert value["opening"]["inputs"] == [
        {"path": "goals/goal-1/data-collection.rows.json"}
    ]
    assert value["closing"]["notes"]["rates_rows"] == (
        "goals/goal-1/data-collection.rows.json"
    )
    # 采集产物路径不再是评级章的输入——它只有模型顺手写下的那一小撮。
    assert {"path": collector.output["path"]} not in value["opening"]["inputs"]
    assert value["closing"]["notes"]["rates_chapter"] == "data-collection"


def _fixture(tmp_path: Path):
    """库里 5 行归 data-collection、2 行归别的章；盘上采集产物只有 1 条。"""
    from app.orchestrator.runtime import RuntimeCoordinator

    store = make_store(tmp_path, "r-rate2")
    for index in range(5):
        store.add_evidence(
            id=f"ev-{index}", report_id="r-rate2", goal_id="goal-1",
            agent_name="data-collection", platform="web_search",
            permalink=f"https://example.com/{index}",
            fetched_at="2026-08-30T00:00:00+00:00",
            published_at="2026-08-20T00:00:00+00:00",
            title=f"标题{index}", content_excerpt="可复核正文",
            author_name=f"作者{index}", fetch_method="search_index",
        )
    for index in range(2):
        store.add_evidence(
            id=f"other-{index}", report_id="r-rate2", goal_id="goal-1",
            agent_name="data-collection-2", platform="web_search",
            permalink=f"https://other.example.com/{index}",
            fetched_at="2026-08-30T00:00:00+00:00", title="别的章的行",
            fetch_method="search_index",
        )
    artifact = tmp_path / "runs" / "r-rate2" / "goals" / "goal-1"
    artifact.mkdir(parents=True, exist_ok=True)
    (artifact / "data-collection.json").write_text(
        json.dumps([{"permalink": "https://example.com/0"}], ensure_ascii=False),
        encoding="utf-8",
    )

    events: list[dict] = []

    async def publish(research_id, payload):
        events.append(payload)

    coordinator = RuntimeCoordinator(
        store=store, event_buffer=SimpleNamespace(publish=publish), researches={},
        cards={}, runs_root=tmp_path / "runs",
        routing_utc_clock=lambda: datetime(2026, 8, 30, tzinfo=timezone.utc),
    )
    collector, rating = _collector(), _rating()
    goal = SimpleNamespace(
        goal_id="goal-1", agents=[collector, rating],
        deliverable={"path": "goals/goal-1/result.md"},
    )
    plan = SimpleNamespace(research_id="r-rate2", goals=[goal])
    return coordinator, plan, rating, events, artifact


def test_物化文件按库行写_条数等于该章库行数而不是产物条数(tmp_path: Path) -> None:
    coordinator, plan, rating, events, artifact = _fixture(tmp_path)

    written = asyncio.run(
        coordinator._materialize_rating_rows(plan, "goal-1", rating)
    )

    rows = json.loads((artifact / "data-collection.rows.json").read_text("utf-8"))
    assert written == len(rows) == 5, "盘上产物只有 1 条，物化必须按库行给满 5 条"
    assert [row["permalink"] for row in rows] == [
        f"https://example.com/{index}" for index in range(5)
    ], "别的采集章的行不能混进来"
    assert set(rows[0]) == {
        "permalink", "title", "content_excerpt", "author_name",
        "platform", "published_at", "fetched_at",
    }
    data = next(e["data"] for e in events if e["type"] == "rating_rows_materialized")
    assert data["rows"] == 5 and data["rates_chapter"] == "data-collection"
    assert data["path"] == "goals/goal-1/data-collection.rows.json"


def test_非评级章不物化(tmp_path: Path) -> None:
    coordinator, plan, _, events, artifact = _fixture(tmp_path)
    collector = plan.goals[0].agents[0]

    assert asyncio.run(
        coordinator._materialize_rating_rows(plan, "goal-1", collector)
    ) == 0
    assert not (artifact / "data-collection.rows.json").exists()
    assert not events


def test_物化文件不是任何agent的声明产物_通用投影读不到它(tmp_path: Path) -> None:
    coordinator, plan, rating, _, artifact = _fixture(tmp_path)

    asyncio.run(coordinator._materialize_rating_rows(plan, "goal-1", rating))

    declared = {
        str(agent.output["path"]) for goal in plan.goals for agent in goal.agents
    }
    assert "goals/goal-1/data-collection.rows.json" not in declared
    # 坑 5 的真闸门在**路径**上：`_persist_goal_evidence` 只读各 agent 声明的
    # `output.path`，物化文件另起文件名所以永远不会被喂进去。内容本身是**过得了**
    # 通用投影的（下面这条断言就是证据），所以这条隔离必须靠文件名守住，
    # 别把物化文件写成某个 agent 的声明产物路径。
    from app.store.evidence_artifacts import load_evidence_payloads

    items = load_evidence_payloads(
        artifact / "data-collection.rows.json",
        report_id="r-rate2", goal_id="goal-1", agent_name="data-collection",
        platform_hint="web_search",
    )
    assert len(items) == 5
