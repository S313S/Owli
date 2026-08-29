"""§RATE-1 货 4：写手候选池按真实评级挑证据（D 级不进池、同 goal 内按分降序）。"""

from __future__ import annotations

from app.orchestrator.sectioning import _evidence_index


def _row(index: int, *, grade: str | None, total: int | None,
         goal_id: str = "goal-1", platform: str = "web_search") -> dict:
    return {
        "id": f"ev-{index:02d}", "goal_id": goal_id, "platform": platform,
        "permalink": f"https://example.com/{index}", "title": f"标题{index}",
        "content_excerpt": "正文", "author_name": "作者",
        "fetched_at": "2026-08-29T00:00:00Z",
        "grade": grade, "score_total": total,
        "rated_by": None if grade is None else "agent:reliability-audit",
    }


def _pool(rows, goal_ids=("goal-1",), section_goal_id="goal-1"):
    index, _ = _evidence_index(
        rows, set(goal_ids), section_goal_id=section_goal_id,
    )
    return index["items"]


def test_D级不进池_未评行照常保留() -> None:
    rows = [
        _row(1, grade="D", total=1),
        _row(2, grade="A", total=9),
        _row(3, grade=None, total=None),
    ]
    ids = [item["evidence_id"] for item in _pool(rows)]
    assert ids == ["ev-02", "ev-03"], "D 级要挡住；空等级 = 还没评到，必须留着"


def test_同goal内按score_total降序_等级次之() -> None:
    rows = [
        _row(1, grade="C", total=4),
        _row(2, grade="A", total=9),
        _row(3, grade="B", total=7),
        _row(4, grade=None, total=None),
    ]
    assert [item["evidence_id"] for item in _pool(rows)] == [
        "ev-02", "ev-03", "ev-01", "ev-04",
    ]


def test_出池字段带上score_total_grade_rated_by() -> None:
    item = _pool([_row(1, grade="A", total=9)])[0]
    assert item["score_total"] == 9
    assert item["grade"] == "A"
    assert item["rated_by"] == "agent:reliability-audit"
    # 未评行不凭空补字段（下游据此分得清「评了最低」与「没法评」）
    bare = _pool([_row(2, grade=None, total=None)])[0]
    assert "grade" not in bare and "score_total" not in bare


def test_全是D级时回退全池_不把写手饿死() -> None:
    rows = [_row(1, grade="D", total=1), _row(2, grade="D", total=0)]
    assert [item["evidence_id"] for item in _pool(rows)] == ["ev-01", "ev-02"]
