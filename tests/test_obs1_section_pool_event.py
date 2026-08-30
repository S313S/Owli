"""§OBS-1：只加事件不改语义——货 1 单节组池组成事件 section_pool_composed。"""

from __future__ import annotations

from tests.test_w1_evidence_pool import _add_evidence, _run_sectioned

_D_NOTES = (
    "权威0:佚名转述 · 时效0:无日期 · 交叉1:弱交叉 · "
    "完整0:无摘要 · 无关0:利益相关"
)


def _add_d_evidence(store, *, evidence_id: str, goal_id: str, permalink: str) -> None:
    """五维分 0+0+1+0+0=1 → 生成列 grade='D'，会被组池 D 闸拦下。"""
    store.add_evidence(
        id=evidence_id, report_id="r-ledger", goal_id=goal_id,
        platform="xhs", permalink=permalink,
        fetched_at="2026-08-27T00:00:00+00:00", title="低质来源",
        content_excerpt="正文", author_name="佚名",
        score_authority=0, score_freshness=0, score_crossref=1,
        score_completeness=0, score_independence=0,
        rating_notes=_D_NOTES,
    )


def _seed_mixed_pool(store, goal_id: str) -> None:
    """3 条可用（1 条 A + 2 条未评）+ 1 条 D；跨 goal、双平台。"""
    _add_evidence(store, evidence_id="ev-a1", goal_id="goal-1",
                  permalink="https://evidence.example/a1", scored=True)
    _add_evidence(store, evidence_id="ev-a2", goal_id="goal-1",
                  permalink="https://evidence.example/a2", platform="xhs")
    _add_evidence(store, evidence_id="ev-b1", goal_id="goal-2",
                  permalink="https://evidence.example/b1")
    _add_d_evidence(store, evidence_id="ev-d1", goal_id="goal-1",
                    permalink="https://evidence.example/d1")


def test_货1_每节组池完成发一条组成事件且载荷字段齐(tmp_path):
    seeded = False

    def seed(store, goal_id):
        nonlocal seeded
        if seeded:
            return
        seeded = True
        _seed_mixed_pool(store, goal_id)

    result, _, _, events, _, _ = _run_sectioned(
        tmp_path, goal_ids=["goal-1", "goal-2"], declared_paths=[], seed=seed,
    )

    assert result.succeeded is True
    composed = [e for e in events if e["type"] == "section_pool_composed"]
    # 两节各一条；重试重组池不重复发。
    assert [e["data"]["section_id"] for e in composed] == ["ch-report/sec-1", "ch-report/sec-2"]
    first = composed[0]["data"]
    assert first["research_id"] == "r-ledger"
    assert first["goal_id"] == "goal-1"
    assert first["pool_size"] == 2
    assert first["own_goal_count"] == 2
    assert first["cross_goal_count"] == 0
    assert first["platform_distribution"] == {"web_search": 1, "xhs": 1}
    assert first["grade_distribution"] == {"A": 1, "unrated": 1}
    assert first["d_gate_filtered"] == 1
    second = composed[1]["data"]
    assert second["goal_id"] == "goal-2"
    assert second["own_goal_count"] == 1
    assert second["cross_goal_count"] == 0
    assert composed[0]["is_error"] is False


def _plain_rows(spec):
    return [{
        "id": f"ev-{goal_id}-{index}", "goal_id": goal_id, "platform": platform,
        "permalink": f"https://example.com/{goal_id}/{index}",
        "title": None, "content_excerpt": None, "author_name": None,
        "fetched_at": "2026-08-28T00:00:00+00:00", "grade": grade,
    } for index, (goal_id, platform, grade) in enumerate(spec)]


def test_货1_D闸计数_全D回退全池时计零(tmp_path):
    from app.orchestrator.sectioning import _evidence_index

    mixed, _ = _evidence_index(_plain_rows([
        ("goal-1", "xhs", None), ("goal-1", "xhs", "D"), ("goal-1", "web_search", "B"),
    ]), {"goal-1"}, section_goal_id="goal-1")
    assert mixed["d_gate_filtered"] == 1
    assert len(mixed["items"]) == 2

    all_d, _ = _evidence_index(_plain_rows([
        ("goal-1", "xhs", "D"), ("goal-1", "web_search", "D"),
    ]), {"goal-1"}, section_goal_id="goal-1")
    assert all_d["d_gate_filtered"] == 0
    assert len(all_d["items"]) == 2