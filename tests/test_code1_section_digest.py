"""§CODE-1 货 3：口碑节提示词前置「本节 UGC 聚合摘要」，池位一条不动。"""

from __future__ import annotations

from app.orchestrator.sectioning import (
    UGC_DIGEST_MARKS_PER_BUCKET,
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


def test_每一格连角标清单一起给():
    """只给条数写不出挂 ≥3 角标的聚合断言——第一轮重放实测过。"""

    digest = _ugc_coding_digest(_pool(10), _rows(10))
    assert "正 5 条 [S01][S03][S05][S07][S09]" in digest
    assert "主题：功能与能力 5 条 [S01][S03][S05][S07][S09]" in digest
    assert "正向代表原声：[S01]「原声1」；[S03]「原声3」" in digest


def test_一格角标过多时截断并标省略():
    digest = _ugc_coding_digest(_pool(30), _rows(30, attitude="正"))
    marks = UGC_DIGEST_MARKS_PER_BUCKET
    assert f"正 15 条 " in digest and "…" in digest
    attitude_line = next(l for l in digest.splitlines() if l.startswith("- 态度："))
    assert attitude_line.count("[S") == marks * 2, "两格各截到上限"


def test_明确要求写聚合断言():
    digest = _ugc_coding_digest(_pool(10), _rows(10))
    assert "至少写 3 条**聚合断言**" in digest
    assert "挂满 3 个以上" in digest


def test_摘要写死了条数口径_禁百分比句式():
    digest = _ugc_coding_digest(_pool(10), _rows(10))
    assert "条数" in digest and "不得写「用户 X% 认为」" in digest
    assert "不得把这段摘要本身当证据" in digest


def test_池外的行取不到():
    """摘要只看本片池里的条目——池位不变是这一货的硬约束。"""

    digest = _ugc_coding_digest(_pool(6), _rows(30))
    assert "本节可见池里 6 条" in digest
