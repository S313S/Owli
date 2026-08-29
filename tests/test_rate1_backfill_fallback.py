"""§RATE-1 货 5：收尾回填改兜底——写作前评级章评过的行不再重评。"""

from __future__ import annotations

import asyncio
from pathlib import Path

_NOTES = (
    "权威2:具名机构 · 时效2:时间窗内 · 交叉1:弱交叉 · "
    "完整2:字段齐全 · 无关2:无利益关系"
)


def _store(tmp_path: Path, *, agent_rated: int, baseline: int):
    """agent_rated 条已由写作前评级章评过（五维齐全），baseline 条只有源基线。"""
    from tests.test_m4fork_followup import _database, _evidence

    _, store = _database(tmp_path)
    store.create_report(id="r-fb", title="兜底", research_question="只补没评到的",
                        created_at="2026-08-29T00:00:00Z")
    rows = []
    for index in range(agent_rated):
        rows.append(_evidence(
            "r-fb", f"a{index:03d}", permalink=f"https://e.example/a{index}",
            score_authority=2, score_freshness=2, score_crossref=1,
            score_completeness=2, score_independence=2,
            rating_notes=_NOTES, rated_by="agent:reliability-audit",
            extra={"authority_kind": "named_secondary",
                   "content_kind": "industry_view",
                   "interest_relation": "arms_length"},
        ))
    for index in range(baseline):
        rows.append(_evidence(
            "r-fb", f"b{index:03d}", permalink=f"https://e.example/b{index}",
            rated_by="baseline:web_search@v1", extra={},
        ))
    store.upsert_evidence_batch(rows)
    return store


def _run(store, tmp_path: Path, **kwargs):
    from app.reliability.backfill import backfill_report
    from tests.test_m4fork_followup import BackfillEngine

    engine = BackfillEngine()
    seen: list[str] = []
    original = engine.run

    async def traced(task, ctx, on_event=None):
        import json as _json

        inputs, _ = _json.JSONDecoder().raw_decode(
            task.body.split("输入证据：", 1)[1]
        )
        seen.extend(str(item["id"]) for item in inputs)
        return await original(task, ctx, on_event=on_event)

    engine.run = traced
    result = asyncio.run(backfill_report(
        store, "r-fb", adapter=engine, runs_root=tmp_path / "runs", **kwargs,
    ))
    engine.seen_ids = seen
    return engine, result


def test_已被评级章评过的行不再进引擎_只补没评到的(tmp_path: Path) -> None:
    store = _store(tmp_path, agent_rated=10, baseline=2)
    engine, result = _run(store, tmp_path)

    assert engine.seen_ids == [f"ev-b{index:03d}" for index in range(2)]
    assert result.attempted == 2, "回填只应把 2 条基线行圈进来"
    rows = {row["id"]: row for row in store.list_evidence("r-fb")}
    # 写作前评过的行原样保留，不被收尾改写
    for index in range(10):
        row = rows[f"ev-a{index:03d}"]
        assert row["rated_by"] == "agent:reliability-audit"
        assert row["rating_notes"] == _NOTES


def test_force_仍然全量重评(tmp_path: Path) -> None:
    store = _store(tmp_path, agent_rated=10, baseline=2)
    _, result = _run(store, tmp_path, force=True)
    assert result.attempted == 12


def test_五维缺一维的agent行仍要补(tmp_path: Path) -> None:
    store = _store(tmp_path, agent_rated=3, baseline=0)
    rows = store.list_evidence("r-fb")
    partial = dict(rows[0])
    partial["score_crossref"] = None
    partial["rating_notes"] = _NOTES.replace("交叉1:弱交叉", "交叉?:证据不足以判断")
    store.upsert_evidence_batch([partial])

    _, result = _run(store, tmp_path)
    assert result.attempted == 1
