"""§CODE-1 货 3：口碑节提示词前置「本节 UGC 聚合摘要」，池位一条不动。"""

from __future__ import annotations

from app.orchestrator.sectioning import (
    UGC_DIGEST_MIN_ROWS,
    _ugc_coding_digest,
)


def _pool(count: int):
    return [{"citation": f"[S{index:02d}]", "evidence_id": f"ev-{index:03d}"}
            for index in range(1, count + 1)]


def _rows(count: int, *, coded: int | None = None, attitude: str = "正"):
    coded = count if coded is None else coded
    rows = {}
    for index in range(1, count + 1):
        extra = {"content_kind": "user_opinion"}
        if index <= coded:
            extra["coding"] = {
                "coding_version": "v1", "audience": "学生", "scenario": "学习",
                "attitude": attitude if index % 2 else "负",
                "topics": ["功能与能力"] if index % 2 else [],
                "quote": f"原声{index}",
            }
        rows[f"ev-{index:03d}"] = {"id": f"ev-{index:03d}", "extra": extra}
    return rows


def test_够数才前置摘要():
    few = UGC_DIGEST_MIN_ROWS - 1
    assert _ugc_coding_digest(_pool(few), _rows(few)) is None
    digest = _ugc_coding_digest(_pool(10), _rows(10))
    assert digest is not None
    assert digest.startswith("【本节 UGC 聚合摘要】本节可见池里 10 条")


def test_没编码的行不进摘要():
    assert _ugc_coding_digest(_pool(10), _rows(10, coded=4)) is None
    digest = _ugc_coding_digest(_pool(10), _rows(10, coded=6))
    assert "本节可见池里 6 条" in digest


def test_摘要给条数与代表原声_并挂角标():
    digest = _ugc_coding_digest(_pool(10), _rows(10))
    assert "态度：" in digest and "正 5 条" in digest and "负 5 条" in digest
    assert "主题：功能与能力 5 条" in digest
    assert "正向代表原声：[S01]「原声1」；[S03]「原声3」" in digest
    assert digest.count("[S") == 4, "每格最多两条原声，正负各一格"


def test_摘要写死了条数口径_禁百分比句式():
    digest = _ugc_coding_digest(_pool(10), _rows(10))
    assert "条数" in digest and "不得写「用户 X% 认为」" in digest
    assert "不得把这段摘要本身当证据" in digest


def test_池外的行取不到():
    """摘要只看本片池里的条目——池位不变是这一货的硬约束。"""

    digest = _ugc_coding_digest(_pool(6), _rows(30))
    assert "本节可见池里 6 条" in digest
