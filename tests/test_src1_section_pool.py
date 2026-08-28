"""§SRC-1 货 6：单节池位不再按平台平分，本节 goal 先占位。"""

from __future__ import annotations

from typing import Any


def _rows(spec: list[tuple[str, str, int]]) -> list[dict[str, Any]]:
    """(goal_id, platform, 条数) → 证据行；id 稳定，便于断言顺序。"""

    rows: list[dict[str, Any]] = []
    for goal_id, platform, count in spec:
        for index in range(count):
            rows.append({
                "id": f"ev-{goal_id}-{platform}-{index:03d}",
                "goal_id": goal_id,
                "platform": platform,
                "permalink": f"https://example.com/{goal_id}/{platform}/{index}",
                "title": None,
                "content_excerpt": None,
                "author_name": None,
                "fetched_at": "2026-08-28T00:00:00+00:00",
            })
    return rows


# ── 货 6：单节 30 个池位，本节 goal 先占位 ───────────────────────

def test_本节goal先占位而不是三个平台各分十个() -> None:
    """诊断根因：原实现只按平台轮转，sec(goal-1) 的 30 个位里只有 14 个可用。"""

    from app.orchestrator.sectioning import (
        SECTION_EVIDENCE_POOL_LIMIT, SECTION_GOAL_FLOOR, _section_evidence_rows,
    )

    rows = _rows([
        ("goal-1", "web_search", 4), ("goal-1", "xhs", 30),
        ("goal-2", "web_search", 10), ("goal-2", "xhs", 28),
        ("goal-3", "douyin", 27),
    ])

    selected = _section_evidence_rows(rows, "goal-1")

    assert len(selected) == SECTION_EVIDENCE_POOL_LIMIT
    own = [row for row in selected if row["goal_id"] == "goal-1"]
    assert len(own) >= SECTION_GOAL_FLOOR
    # 货 5 要用的跨 goal 对照证据仍进得来，不是把别的 goal 全挤掉。
    assert len(selected) > len(own)


def test_抖音那一节终于能看见自己名下全部抖音() -> None:
    """D-013 那轮 sec(goal-3) 名下 27 条抖音只进得去 10 条。"""

    from app.orchestrator.sectioning import _section_evidence_rows

    rows = _rows([
        ("goal-1", "web_search", 4), ("goal-1", "xhs", 30),
        ("goal-2", "web_search", 10), ("goal-2", "xhs", 28),
        ("goal-3", "douyin", 27),
    ])

    selected = _section_evidence_rows(rows, "goal-3")
    douyin = [row for row in selected if row["platform"] == "douyin"]

    # 先占位拿满 20，余额轮转时抖音又分到几个；旧口径固定只有 10。
    assert len(douyin) == 24
    assert all(row["goal_id"] == "goal-3" for row in douyin)
    # 余额里仍留了跨 goal 对照的位子（货 5 要用）。
    assert {row["goal_id"] for row in selected} > {"goal-3"}


def test_本节goal证据不足时不留空位() -> None:
    from app.orchestrator.sectioning import (
        SECTION_EVIDENCE_POOL_LIMIT, _section_evidence_rows,
    )

    rows = _rows([("goal-1", "xhs", 3), ("goal-2", "web_search", 40)])
    selected = _section_evidence_rows(rows, "goal-1")

    assert len(selected) == SECTION_EVIDENCE_POOL_LIMIT
    assert sum(1 for row in selected if row["goal_id"] == "goal-1") == 3


def test_没有本节goal时退回老的平台轮转() -> None:
    from app.orchestrator.sectioning import _section_evidence_rows

    rows = _rows([("goal-1", "xhs", 20), ("goal-2", "douyin", 20)])
    selected = _section_evidence_rows(rows, None, limit=10)

    platforms = {row["platform"] for row in selected}
    assert platforms == {"xhs", "douyin"}
    assert len(selected) == 10


def test_选出的行不重复() -> None:
    from app.orchestrator.sectioning import _section_evidence_rows

    rows = _rows([("goal-1", "xhs", 25), ("goal-2", "douyin", 25)])
    selected = _section_evidence_rows(rows, "goal-1")

    assert len({row["id"] for row in selected}) == len(selected)
