"""§RATE-4 货 2：`--rescore-only` 只重算分不重打标签，一次引擎都不过。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

_UGC_NOTES = (
    "权威0:作者不可核验 · 时效2:时间窗内 · 交叉0:该断言仅一簇 · "
    "完整2:字段齐全 · 无关2:无可见利益关系"
)


def _store(tmp_path: Path, *, ugc: int = 40, unlabeled: int = 3):
    from tests.test_m4fork_followup import _database, _evidence

    _, store = _database(tmp_path)
    store.create_report(id="r-rs", title="重算", research_question="换尺子",
                        created_at="2026-09-05T00:00:00Z")
    rows = []
    for index in range(ugc):
        rows.append(_evidence(
            "r-rs", f"u{index:03d}", permalink=f"https://xhs.example/u{index}",
            platform="xhs", raw_metrics={"liked_count": index * 10},
            score_authority=0, score_freshness=2, score_crossref=0,
            score_completeness=2, score_independence=2,
            rating_notes=_UGC_NOTES, rated_by="agent:reliability-audit",
            extra={"authority_kind": "anonymous_or_unverifiable",
                   "content_kind": "user_opinion",
                   "interest_relation": "arms_length",
                   "crossref_verdict": "SINGLE",
                   "crossref_n_clusters": 1,
                   "claim_ids": ["c-1"]},
        ))
    for index in range(unlabeled):
        rows.append(_evidence(
            "r-rs", f"n{index:03d}", permalink=f"https://e.example/n{index}",
            rated_by="baseline:web_search@v1", extra={},
        ))
    store.upsert_evidence_batch(rows)
    return store


def _run(store, tmp_path: Path, **kwargs):
    from app.reliability.backfill import backfill_report
    from tests.test_m4fork_followup import BackfillEngine

    engine = BackfillEngine()
    calls: list[str] = []
    original = engine.run

    async def traced(task, ctx, on_event=None):
        calls.append(str(getattr(task, "agent_kind", "?")))
        return await original(task, ctx, on_event=on_event)

    engine.run = traced
    result = asyncio.run(backfill_report(
        store, "r-rs", adapter=engine, runs_root=tmp_path / "runs", **kwargs,
    ))
    return calls, result


def test_只重算分不过引擎_UGC行换成代表性尺子(tmp_path: Path) -> None:
    store = _store(tmp_path)
    calls, result = _run(store, tmp_path, rescore_only=True)

    assert calls == [], "只重算分的一轮不该有任何引擎调用"
    assert result.attempted == 40, "3 条没标签的行不进 attempted"
    assert result.failed == 0
    rows = {row["id"]: row for row in store.list_evidence("r-rs")}
    top = rows["ev-u039"]
    assert top["rating_notes"].startswith("代表性2:P100")
    assert top["score_authority"] == 2
    assert top["grade"] == "A"
    # 标签一个字没改，来源也不冒充本轮引擎
    assert top["extra"]["authority_kind"] == "anonymous_or_unverifiable"
    assert top["rated_by"] == "agent:reliability-audit"
    bottom = rows["ev-u000"]
    assert bottom["rating_notes"].startswith("代表性0:P0")
    assert bottom["score_authority"] == 0
    # 没标签的行原样不动
    assert rows["ev-n000"]["rated_by"] == "baseline:web_search@v1"


def test_重算两遍逐字段零差异(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _run(store, tmp_path, rescore_only=True)
    first = {row["id"]: dict(row) for row in store.list_evidence("r-rs")}
    _run(store, tmp_path, rescore_only=True)
    second = {row["id"]: dict(row) for row in store.list_evidence("r-rs")}
    assert first == second


def test_rescore_only_与_force_互斥(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        _run(store, tmp_path, rescore_only=True, force=True)


def test_只动第一维_其余四维连分带理由原样(tmp_path: Path) -> None:
    """用户 09-05 拍甲：换尺子那一轮不替别的维重下判断。"""

    store = _store(tmp_path)
    before = {row["id"]: dict(row) for row in store.list_evidence("r-rs")}
    _run(store, tmp_path, rescore_only=True)
    after = {row["id"]: dict(row) for row in store.list_evidence("r-rs")}
    fields = ("score_freshness", "score_crossref",
              "score_completeness", "score_independence")
    for identity, row in after.items():
        for field in fields:
            assert row[field] == before[identity][field], (identity, field)
        # 后四段理由逐字不变（没评过的行 rating_notes 是 None，一并原样）
        assert str(row["rating_notes"] or "").split(" · ", 1)[1:] == \
            str(before[identity]["rating_notes"] or "").split(" · ", 1)[1:]
