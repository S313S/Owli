"""§RATE-1 货 3：评级章产物按 permalink 贴回已入库的行，评级章 done 即入库。

判据落库：行数不变（不插新行）、`rated_by=agent:<评级章>`、五维非 NULL；
库里没有的 permalink 不插新行、只发事件说清楚（upsert 只认 (report_id, permalink)）。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from tests.test_c1_claims import add_evidence, make_store

_NOTES = (
    "权威2:平台原帖 · 时效2:时间窗内 · 交叉1:弱交叉 · "
    "完整2:字段齐全 · 无关2:无利益关系"
)


def _rating_item(permalink: str) -> dict:
    return {
        "permalink": permalink,
        "score_authority": 2, "score_freshness": 2, "score_crossref": 1,
        "score_completeness": 2, "score_independence": 2,
        "rating_notes": _NOTES,
        "rated_by": "reliability-audit",
        "extra": {
            "authority_kind": "named_secondary",
            "content_kind": "industry_view",
            "interest_relation": "arms_length",
        },
    }


def _fixture(tmp_path: Path, *, extra_permalinks: list[str] | None = None):
    """采集 3 条已入库；评级章产物覆盖它们（可另加库里没有的 permalink）。"""
    from app.orchestrator.runtime import RuntimeCoordinator

    store = make_store(tmp_path, "r-rate")
    urls = [f"https://example.com/{index}" for index in range(3)]
    for index, url in enumerate(urls):
        add_evidence(store, "r-rate", f"ev-{index}", platform="web_search",
                     permalink=url, author=f"作者{index}")
    artifact = tmp_path / "runs" / "r-rate" / "goals" / "goal-1" / "rating.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(json.dumps(
        [_rating_item(url) for url in [*urls, *(extra_permalinks or [])]],
        ensure_ascii=False,
    ), encoding="utf-8")

    events: list[dict] = []

    async def publish(research_id, payload):
        events.append(payload)

    coordinator = RuntimeCoordinator(
        store=store, event_buffer=SimpleNamespace(publish=publish), researches={},
        cards={}, runs_root=tmp_path / "runs",
        routing_utc_clock=lambda: datetime(2026, 8, 29, tzinfo=timezone.utc),
    )
    collector = SimpleNamespace(
        agent_id="data-collection",
        capability={"profile": "web-collector", "sources": ["web_search"]},
        output={"path": "goals/goal-1/data-collection.json"},
        depends_on=[],
    )
    rating = SimpleNamespace(
        agent_id="reliability-audit",
        capability={"profile": "readonly-analyst", "sources": []},
        output={
            "path": "goals/goal-1/rating.json",
            "validators": ["file_exists", "no_item_missing_rating"],
        },
        depends_on=["data-collection"],
    )
    goal = SimpleNamespace(
        goal_id="goal-1", agents=[collector, rating],
        deliverable={"path": "goals/goal-1/result.md"},
    )
    plan = SimpleNamespace(research_id="r-rate", goals=[goal])
    return coordinator, store, plan, rating, events, urls


def test_评级章产物按permalink贴回_行数不变且五维落库(tmp_path: Path) -> None:
    coordinator, store, plan, rating, events, urls = _fixture(tmp_path)

    before = store.list_evidence("r-rate")
    assert len(before) == 3 and all(row["score_authority"] is None for row in before)

    asyncio.run(coordinator._persist_rating_chapter(plan, "goal-1", rating))

    rows = {row["permalink"]: row for row in store.list_evidence("r-rate")}
    assert len(rows) == 3, "评级章不得插新行——upsert 只认 (report_id, permalink)"
    for url in urls:
        row = rows[url]
        assert row["rated_by"] == "agent:reliability-audit"
        assert row["score_authority"] == 2 and row["score_crossref"] == 1
        assert row["rating_notes"] == _NOTES
        assert row["extra"]["authority_kind"] == "named_secondary"
        # 采集期的正文真值不被评级章覆盖（评级章根本不带这些字段）
        assert row["title"] and row["author_name"]
    persisted = [e["data"] for e in events if e["type"] == "rating_chapter_persisted"]
    assert persisted == [{
        "goal_id": "goal-1", "agent_id": "reliability-audit",
        "rated": 3, "unmatched": 0, "samples": [],
    }]


def test_评了库里没有的permalink_不插新行且事件说清楚(tmp_path: Path) -> None:
    coordinator, store, plan, rating, events, _ = _fixture(
        tmp_path, extra_permalinks=["https://example.com/凭空捏造"],
    )

    asyncio.run(coordinator._persist_rating_chapter(plan, "goal-1", rating))

    assert len(store.list_evidence("r-rate")) == 3
    data = next(e["data"] for e in events if e["type"] == "rating_chapter_persisted")
    assert data["rated"] == 3 and data["unmatched"] == 1
    assert data["samples"] == ["https://example.com/凭空捏造"]


def test_非评级章不走这条投影(tmp_path: Path) -> None:
    coordinator, store, plan, rating, events, _ = _fixture(tmp_path)
    collector = plan.goals[0].agents[0]

    asyncio.run(coordinator._persist_rating_chapter(plan, "goal-1", collector))

    assert not events
    assert all(row["rated_by"] is None for row in store.list_evidence("r-rate"))


def test_评级产物过不了通用投影_所以必须走专用路径(tmp_path: Path) -> None:
    """通用投影按 platform/fetched_at/正文 判合法；评级产物本来就没有这些。"""
    from app.store.evidence_artifacts import load_evidence_payloads

    _fixture(tmp_path)
    artifact = tmp_path / "runs" / "r-rate" / "goals" / "goal-1" / "rating.json"
    assert load_evidence_payloads(
        artifact, report_id="r-rate", goal_id="goal-1",
        agent_name="reliability-audit",
    ) == []


def test_goal收尾时先入库采集产物再贴评级(tmp_path: Path) -> None:
    """顺序是硬约束：先 upsert 采集产物、再按刷新后的库贴评分。

    反过来（同一批里一起贴）在「采集产物还没入库」的这一轮里一条都贴不上——
    m2 端到端第一版就是这样，事件里 rated=0 / unmatched=1。
    """
    coordinator, store, plan, rating, events, urls = _fixture(tmp_path)
    goal = plan.goals[0]
    # 库里先清空：模拟「本轮采集产物还没入库」
    for row in store.list_evidence("r-rate"):
        assert row["permalink"] in urls
    ordering: list[str] = []
    original_upsert = store.upsert_evidence_batch

    def traced_upsert(items):
        items = list(items)
        ordering.append(
            "upsert:rating"
            if items and all(
                str(item.get("rated_by") or "").startswith("agent:") for item in items
            )
            else "upsert:collection"
        )
        return original_upsert(items)

    store.upsert_evidence_batch = traced_upsert
    for index, agent in enumerate(goal.agents, start=1):
        agent.chapter = {"chapter_id": f"ch-{index}"}
        agent.output["format"] = "json"
    store.ensure_chapters("r-rate", [
        {"goal_id": "goal-1", "chapter_id": f"ch-{index}"}
        for index in (1, 2)
    ], updated_at="2026-08-29T00:00:00Z")
    for index in (1, 2):
        store.finish_chapter(
            "r-rate", "goal-1", f"ch-{index}", status="done", reason=None,
            actual_output_path=None, actual_count=None,
            updated_at="2026-08-29T00:00:00Z",
        )
    (tmp_path / "runs" / "r-rate" / "goals" / "goal-1"
     / "data-collection.json").write_text(json.dumps([{
        "platform": "web_search", "permalink": "https://example.com/9",
        "fetched_at": "2026-08-29T00:00:00Z", "title": "新采到的一条",
        "content_excerpt": "正文", "source_type": "article",
        "fetch_method": "search_index",
     }], ensure_ascii=False), encoding="utf-8")
    asyncio.run(coordinator._persist_goal_evidence(plan, goal))
    assert ordering == ["upsert:collection", "upsert:rating"]
