"""D-049：投影不许把收尾补评算好的分盖回评级章旧值。

现场（§RATE-4 货 3 第一轮重放）：沙盒库按新尺子重算 533 行后起重放，runtime 把
`runs/<id>/goals/goal-N/reliability-audit*.json` 这份**评级章当时写下的旧分**
重新投影回 `evidence`，写手池仍是改前的池（`section_pool_composed.d_gate_filtered`
仍 292，库里已是 253），成稿无变化。D-032 同族——当时保护名单只收了身份/来源列，
评分列有意没进（理由「评级章产物会重贴、能复原」在补评之后不成立）。

判法：库里已有分的行，投影一格不动（事件 `kept`）；五维全 NULL 的行，旧产物
照贴回（事件 `filled`）。不拿 `rated_by` 判新旧——`--rescore-only` 的补评保留
原 `agent:*` 标记（`app/reliability/backfill.py:_rating_provenance`）。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from tests.test_c1_claims import add_evidence, make_store

_SCORES = (
    "score_authority", "score_freshness", "score_crossref",
    "score_completeness", "score_independence",
)
_OLD_NOTES = (
    "权威2:平台原帖 · 时效2:时间窗内 · 交叉1:弱交叉 · "
    "完整2:字段齐全 · 无关2:无利益关系"
)
_BACKFILL_NOTES = (
    "代表性0:分位偏低 · 时效1:半年内 · 交叉0:孤证 · "
    "完整2:字段齐全 · 无关1:无利益关系"
)
# 部分维评不出来时库里是 NULL，备注用 `?` 记法占位（两者必须逐格一致）
_PARTIAL_NOTES = (
    "代表性0:分位偏低 · 时效?:无发布时间 · 交叉?:未评 · "
    "完整?:未评 · 无关?:未评"
)
_PARTIAL_SCORES = {
    "score_authority": 0, "score_freshness": None, "score_crossref": None,
    "score_completeness": None, "score_independence": None,
}
# `--rescore-only` 只拿库存标签重算，一次引擎都不过 → 原 agent 标记原样留着
_BACKFILL_RATED_BY = "agent:reliability-audit"
_BACKFILL_SCORES = {
    "score_authority": 0, "score_freshness": 1, "score_crossref": 0,
    "score_completeness": 2, "score_independence": 1,
}


def _old_rating_item(permalink: str) -> dict:
    """评级章当时写下的产物：旧分 + 旧标签。"""

    return {
        "permalink": permalink,
        "score_authority": 2, "score_freshness": 2, "score_crossref": 1,
        "score_completeness": 2, "score_independence": 2,
        "rating_notes": _OLD_NOTES,
        "rated_by": "reliability-audit",
        "extra": {
            "authority_kind": "named_secondary",
            "content_kind": "industry_view",
            "interest_relation": "arms_length",
        },
    }


def _echo_item(permalink: str) -> dict:
    """采集章引擎回显形态：没有平台没有分，全列 UPDATE 会把分抹成 NULL。"""

    return {
        "permalink": permalink, "fetched_at": "2026-09-05T15:00:00Z",
        "author": "引擎回显作者", "text": "引擎回显正文",
        "engagement": {"like_count": 12}, "entity": "workbuddy",
    }


def _fixture(tmp_path: Path):
    """一个 goal：采集章 + 评级章，两章都 done，两份产物都在盘上。"""

    from app.orchestrator.runtime import RuntimeCoordinator

    store = make_store(tmp_path, "r-d049")
    urls = [f"https://example.com/{index}" for index in range(3)]
    for index, url in enumerate(urls):
        add_evidence(store, "r-d049", f"ev-{index}", platform="web_search",
                     permalink=url, author=f"作者{index}")
    goal_dir = tmp_path / "runs" / "r-d049" / "goals" / "goal-1"
    goal_dir.mkdir(parents=True, exist_ok=True)
    (goal_dir / "rating.json").write_text(json.dumps(
        [_old_rating_item(url) for url in urls], ensure_ascii=False,
    ), encoding="utf-8")
    (goal_dir / "data-collection.json").write_text(json.dumps(
        [_echo_item(url) for url in urls], ensure_ascii=False,
    ), encoding="utf-8")

    events: list[dict] = []

    async def publish(research_id, payload):
        events.append(payload)

    coordinator = RuntimeCoordinator(
        store=store, event_buffer=SimpleNamespace(publish=publish), researches={},
        cards={}, runs_root=tmp_path / "runs",
        routing_utc_clock=lambda: datetime(2026, 9, 5, tzinfo=timezone.utc),
    )
    collector = SimpleNamespace(
        agent_id="data-collection", chapter={"chapter_id": "data-collection"},
        capability={"profile": "web-collector", "sources": ["web_search"]},
        output={"format": "json", "path": "goals/goal-1/data-collection.json"},
        depends_on=[],
    )
    rating = SimpleNamespace(
        agent_id="reliability-audit", chapter={"chapter_id": "reliability-audit"},
        capability={"profile": "readonly-analyst", "sources": []},
        output={"format": "json", "path": "goals/goal-1/rating.json",
                "validators": ["file_exists", "no_item_missing_rating"]},
        depends_on=["data-collection"],
    )
    goal = SimpleNamespace(goal_id="goal-1", agents=[collector, rating],
                          deliverable={"path": "goals/goal-1/result.md"})
    for agent in (collector, rating):
        store.ensure_chapters(
            "r-d049", [{"goal_id": "goal-1", "chapter_id": agent.agent_id}],
            updated_at="2026-09-05T00:00:00Z",
        )
        store.finish_chapter(
            "r-d049", "goal-1", agent.agent_id, status="done", reason=None,
            actual_output_path=str(agent.output["path"]), actual_count=len(urls),
            updated_at="2026-09-05T00:00:01Z",
        )
    plan = SimpleNamespace(research_id="r-d049", goals=[goal])
    return coordinator, store, plan, goal, events, urls


def _backfill(
    store, *, scores: dict = _BACKFILL_SCORES, notes: str = _BACKFILL_NOTES,
) -> dict[str, dict]:
    """模拟收尾补评：只改分，`rated_by` 保留原 agent 标记（rescore-only 形态）。"""

    payloads = []
    for row in store.list_evidence("r-d049"):
        payload = dict(row)
        payload.update(scores)
        payload["rating_notes"] = notes
        payload["rated_by"] = _BACKFILL_RATED_BY
        payloads.append(payload)
    store.upsert_evidence_batch(payloads)
    return {row["permalink"]: dict(row) for row in store.list_evidence("r-d049")}


def _rows(store) -> dict[str, dict]:
    return {row["permalink"]: dict(row) for row in store.list_evidence("r-d049")}


def _event(events: list[dict]) -> dict:
    return next(
        item["data"] for item in events
        if item["type"] == "rating_chapter_persisted"
    )


def test_a_补评后的分不被评级章旧产物盖回(tmp_path: Path) -> None:
    """收尾补评改好的分，走一遍 goal 收尾投影（采集产物 + 评级章）后逐字不变。"""

    coordinator, store, plan, goal, events, urls = _fixture(tmp_path)
    before = _backfill(store)

    asyncio.run(coordinator._persist_goal_evidence(plan, goal))

    after = _rows(store)
    assert len(after) == 3, "投影不许插新行"
    for url in urls:
        for column in (*_SCORES, "rating_notes", "rated_by"):
            assert after[url][column] == before[url][column], f"{url} {column}"
        assert after[url]["rated_by"] == _BACKFILL_RATED_BY
        assert after[url]["rating_notes"] == _BACKFILL_NOTES
        # 三个闭集标签同理：库里已有的不许被旧产物顶掉
        assert after[url]["extra"]["authority_kind"] == "community_high_signal"
    data = _event(events)
    assert data["kept"] == 3 and data["filled"] == 0
    assert data["rated"] == 3 and data["failed"] == ""


def test_b_五维全NULL时旧产物照贴回(tmp_path: Path) -> None:
    """「旧产物回贴」这条路不许被本卡改坏——没分的行照旧整份贴。"""

    coordinator, store, plan, goal, events, urls = _fixture(tmp_path)
    assert all(
        row["score_authority"] is None for row in store.list_evidence("r-d049")
    )

    asyncio.run(coordinator._persist_goal_evidence(plan, goal))

    rows = _rows(store)
    for url in urls:
        assert rows[url]["score_authority"] == 2
        assert rows[url]["score_independence"] == 2
        assert rows[url]["rating_notes"] == _OLD_NOTES
        assert rows[url]["rated_by"] == "agent:reliability-audit"
        assert rows[url]["extra"]["authority_kind"] == "named_secondary"
    data = _event(events)
    assert data["kept"] == 0 and data["filled"] == 3


def test_c_采集章产物不把已有的分抹成NULL(tmp_path: Path) -> None:
    """没有评级章那一步兜底时，采集产物的全列 UPDATE 也不许把分写空。"""

    coordinator, store, plan, goal, events, urls = _fixture(tmp_path)
    before = _backfill(store)
    goal.agents = [
        agent for agent in goal.agents if agent.agent_id == "data-collection"
    ]

    asyncio.run(coordinator._persist_goal_evidence(plan, goal))

    after = _rows(store)
    assert len(after) == 3
    for url in urls:
        for column in (*_SCORES, "rating_notes", "rated_by"):
            assert after[url][column] == before[url][column], f"{url} {column}"
    assert not [
        item for item in events if item["type"] == "rating_chapter_persisted"
    ], "本用例没有评级章，不该有评级回写事件"


def test_五维部分为NULL的问号记法_整块不动(tmp_path: Path) -> None:
    """评分块整块判：分与备注必须逐格一致，按列补空会写出被库拒的不一致行。"""

    coordinator, store, plan, goal, events, urls = _fixture(tmp_path)
    before = _backfill(store, scores=_PARTIAL_SCORES, notes=_PARTIAL_NOTES)

    asyncio.run(coordinator._persist_goal_evidence(plan, goal))

    after = _rows(store)
    for url in urls:
        for column in (*_SCORES, "rating_notes"):
            assert after[url][column] == before[url][column], f"{url} {column}"
        assert after[url]["score_freshness"] is None, "空格子不许被产物补出不一致"
        assert after[url]["rating_notes"] == _PARTIAL_NOTES
    data = _event(events)
    assert data["kept"] == 3 and data["filled"] == 0
