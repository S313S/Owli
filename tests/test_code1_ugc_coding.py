"""§CODE-1 货 1：UGC 编码闭集、原文子串闸与只编该编的行。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.reliability.coding import (
    CODING_VERSION,
    coding_errors,
    coding_targets,
    engine_input,
    is_coded,
    source_text,
)

_TEXT = "豆包写作文真的好用，比我自己憋一晚上强多了"


def _row(index: int, *, grade: str = "B", kind: str = "user_opinion",
         text: str = _TEXT, coded: bool = False, title: str = "作业救星"):
    extra = {"content_kind": kind, "authority_kind": "anonymous_or_unverifiable"}
    if coded:
        extra["coding"] = {"coding_version": CODING_VERSION, "audience": "学生",
                           "scenario": "学习", "attitude": "正", "topics": [],
                           "quote": ""}
    return {
        "id": f"ev-{index:03d}", "goal_id": "goal-1", "platform": "xhs",
        "title": title, "content_excerpt": text, "grade": grade, "extra": extra,
    }


def _label(row, **overrides):
    label = {"id": row["id"], "audience": "学生", "scenario": "学习",
             "attitude": "正", "topics": ["功能与能力"], "quote": "真的好用"}
    label.update(overrides)
    return label


def _check(rows, labels):
    inputs = [engine_input(row) for row in rows]
    sources = {str(row["id"]): source_text(row) for row in rows}
    return coding_errors(labels, inputs, sources)


def test_闭集内的一批全过():
    rows = [_row(i) for i in range(3)]
    assert _check(rows, [_label(row) for row in rows]) == []


def test_quote_必须逐字出现在原文里():
    rows = [_row(0)]
    改写 = _label(rows[0], quote="非常好用")          # 原文没有这四个字
    assert any("不是原文子串" in e for e in _check(rows, [改写]))
    换行重排 = _label(rows[0], quote="豆包写作文\n真的好用")  # 只差空白，放行
    assert _check(rows, [换行重排]) == []
    空摘录 = _label(rows[0], quote="")
    assert _check(rows, [空摘录]) == []


def test_闭集越界与主题重复都拦下():
    rows = [_row(0)]
    for field, bad in (("audience", "老板"), ("scenario", "炒股"), ("attitude", "好")):
        assert any(field in e for e in _check(rows, [_label(rows[0], **{field: bad})]))
    assert any("topics" in e for e in _check(rows, [_label(rows[0], topics=["八卦"])]))
    assert any("重复" in e for e in _check(
        rows, [_label(rows[0], topics=["功能与能力", "功能与能力"])]))


def test_条数与顺序对不上整批退回():
    rows = [_row(0), _row(1)]
    assert _check(rows, [_label(rows[0])])[0].startswith("编码条数应为 2")
    swapped = [_label(rows[1]), _label(rows[0])]
    assert any("id 与输入不一致" in e for e in _check(rows, swapped))


def test_quote_超过四十字被拦():
    long_text = "好" * 60
    rows = [_row(0, text=long_text, title="")]
    assert any("超过 40 字" in e for e in _check(rows, [_label(rows[0], quote="好" * 41)]))


def test_只编该编的行():
    rows = [
        _row(0),                              # 该编
        _row(1, grade="D"),                   # D 级不进池，编了也没人看
        _row(2, kind="industry_view"),        # 不是 UGC
        _row(3, grade=None),                  # 还没评级
        _row(4, text="", title=""),           # 没正文，摘不出 quote
        _row(5, coded=True),                  # 已编过
    ]
    assert [row["id"] for row in coding_targets(rows)] == ["ev-000"]
    assert [row["id"] for row in coding_targets(rows, force=True)] == ["ev-000", "ev-005"]
    assert is_coded(rows[5]) and not is_coded(rows[0])


def test_引擎输入带正文_评级那条路不带():
    from app.reliability.backfill import _engine_input as rating_input

    row = _row(0)
    assert engine_input(row)["text"] == _TEXT
    assert "text" not in rating_input(row) and "content_excerpt" not in rating_input(row)


class _CodingEngine:
    """按闸门要求回一份合法编码；`bad_quote_once` 用来验「退回重打」这条路。"""

    def __init__(self, *, bad_quote_once: bool = False, fail_all: bool = False) -> None:
        self.calls = 0
        self.bad_quote_once = bad_quote_once
        self.fail_all = fail_all
        self.batch_sizes: list[int] = []

    async def run(self, task, ctx, on_event=None):
        import json as _json

        del ctx, on_event
        self.calls += 1
        assert str(task.output_path) in task.body
        assert task.agent_kind == "ugc_coding"
        inputs, _ = _json.JSONDecoder().raw_decode(task.body.split("输入证据：", 1)[1])
        self.batch_sizes.append(len(inputs))
        bad = self.fail_all or (self.bad_quote_once and self.calls == 1)
        payload = [{
            "id": item["id"], "audience": "学生", "scenario": "学习",
            "attitude": "正", "topics": ["功能与能力"],
            "quote": "凭空捏造的一句" if bad else "真的好用",
        } for item in inputs]
        task.output_path.write_text(
            _json.dumps(payload, ensure_ascii=False), encoding="utf-8",
        )
        return type("R", (), {"succeeded": True, "engine_error": None})()


def _store(tmp_path: Path, count: int = 3):
    from tests.test_m4fork_followup import _database, _evidence

    _, store = _database(tmp_path)
    store.create_report(id="r-cd", title="编码", research_question="国内怎么看",
                        created_at="2026-09-05T00:00:00Z")
    store.upsert_evidence_batch([
        _evidence(
            "r-cd", f"u{index:03d}", permalink=f"https://xhs.example/u{index}",
            platform="xhs", title="作业救星", content_excerpt=_TEXT,
            score_authority=2, score_freshness=2, score_crossref=0,
            score_completeness=2, score_independence=2,
            rating_notes="代表性2:P95 · 时效2:窗内 · 交叉0:孤证 · "
                         "完整2:齐全 · 无关2:无利益",
            extra={"content_kind": "user_opinion",
                   "authority_kind": "anonymous_or_unverifiable",
                   "interest_relation": "arms_length"},
        ) for index in range(count)
    ])
    return store


def _run(store, tmp_path: Path, engine, **kwargs):
    from app.reliability.coding import code_report

    return asyncio.run(code_report(
        store, "r-cd", adapter=engine, runs_root=tmp_path / "runs", **kwargs,
    ))


def test_编码落库并可重复(tmp_path: Path) -> None:
    store = _store(tmp_path)
    engine = _CodingEngine()
    result = _run(store, tmp_path, engine)
    assert (result.targets, result.coded, result.failed) == (3, 3, 0)
    assert result.coverage == 1.0
    row = store.list_evidence("r-cd")[0]
    coding = row["extra"]["coding"]
    assert coding["coding_version"] == CODING_VERSION
    assert (coding["audience"], coding["attitude"]) == ("学生", "正")
    assert coding["quote"] == "真的好用"
    assert row["score_total"] == 8 and row["grade"] == "A", "生成列没被写坏"
    # 再跑一遍：已编过的不再进引擎
    engine2 = _CodingEngine()
    again = _run(store, tmp_path, engine2)
    assert engine2.calls == 0 and again.already == 3 and again.coverage == 1.0


def test_摘错原文会退回重打(tmp_path: Path) -> None:
    store = _store(tmp_path)
    engine = _CodingEngine(bad_quote_once=True)
    result = _run(store, tmp_path, engine)
    assert engine.calls == 2, "第一批摘错原文应退回重打一次"
    assert result.coded == 3 and result.failed == 0


def test_三次都摘错就整批不写库(tmp_path: Path) -> None:
    store = _store(tmp_path)
    engine = _CodingEngine(fail_all=True)
    result = _run(store, tmp_path, engine)
    assert engine.calls == 3 and result.coded == 0 and result.failed == 3
    assert all("coding" not in row["extra"] for row in store.list_evidence("r-cd"))


def test_批量上限不超过四十(tmp_path: Path) -> None:
    store = _store(tmp_path, count=45)
    engine = _CodingEngine()
    _run(store, tmp_path, engine)
    assert engine.batch_sizes == [40, 5]
    with pytest.raises(ValueError):
        _run(store, tmp_path, _CodingEngine(), batch_size=41)
