"""§RATE-5：平台基线分不算「有分」，评级章真评分整块覆盖基线行。

现场（r-20271e8a5028）：帖子入库带 `rated_by=baseline:<platform>@v1` 的平台基线分，
评级章给 20 条微博帖打了完整五维分，事件 `rating_chapter_persisted` 读
`rated 20 / kept 20 / filled 0`——D-049 的「有分不动」按五维非空判，把基线占位当成
真分整块保留，agent 评分全丢；正式稿 `citation_preflight` 因此拦下 19 条查不到等级。

判法：`baseline:` 开头（含 `:degraded`）的行视为未评，产物整块覆盖（事件
`replaced_baseline`）；agent 已评行照 D-049 一格不动（`kept`）；五维全空行照旧贴回
（`filled`）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from tests.test_d049_projection_keeps_scores import (
    _BACKFILL_NOTES, _BACKFILL_RATED_BY, _BACKFILL_SCORES, _OLD_NOTES, _SCORES,
    _event, _fixture, _rows,
)

_BASELINE_NOTES = (
    "代表性0:P0 · 时效0:发布时间晚于采集 · 交叉?:缺断言血缘簇 · "
    "完整2:正文作者时间齐全 · 无关2:无可见利益关系"
)
_BASELINE_SCORES = {
    "score_authority": 0, "score_freshness": 0, "score_crossref": None,
    "score_completeness": 2, "score_independence": 2,
}


def _seed(store, url: str, *, scores: dict, notes: str, rated_by: str,
          authority_kind: str = "community_high_signal") -> None:
    row = next(r for r in store.list_evidence("r-d049") if r["permalink"] == url)
    payload = dict(row)
    payload.update(scores)
    payload["rating_notes"] = notes
    payload["rated_by"] = rated_by
    payload["extra"] = {**(row.get("extra") or {}), "authority_kind": authority_kind}
    store.upsert_evidence_batch([payload])


def test_基线行被评级章真评分整块覆盖_agent行不动_空行照贴(tmp_path: Path) -> None:
    coordinator, store, plan, goal, events, urls = _fixture(tmp_path)
    baseline_url, agent_url, empty_url = urls
    _seed(store, baseline_url, scores=_BASELINE_SCORES, notes=_BASELINE_NOTES,
          rated_by="baseline:weibo@v1", authority_kind="anonymous_or_unverifiable")
    _seed(store, agent_url, scores=_BACKFILL_SCORES, notes=_BACKFILL_NOTES,
          rated_by=_BACKFILL_RATED_BY)
    before = _rows(store)

    asyncio.run(coordinator._persist_goal_evidence(plan, goal))

    after = _rows(store)
    assert len(after) == 3, "投影不许插新行"
    # 基线行：五维、备注、rated_by、闭集标签全部换成评级章产物
    replaced = after[baseline_url]
    assert [replaced[c] for c in _SCORES] == [2, 2, 1, 2, 2]
    assert replaced["score_crossref"] == 1, "基线里空着的交叉维也要落上"
    assert replaced["rating_notes"] == _OLD_NOTES
    assert replaced["rated_by"] == "agent:reliability-audit"
    assert replaced["extra"]["authority_kind"] == "named_secondary"
    assert replaced["grade"] is not None
    # agent 已评行：D-049 语义不变
    for column in (*_SCORES, "rating_notes", "rated_by"):
        assert after[agent_url][column] == before[agent_url][column], column
    assert after[agent_url]["extra"]["authority_kind"] == "community_high_signal"
    # 五维空行：照旧整份贴回
    assert after[empty_url]["score_authority"] == 2
    assert after[empty_url]["rated_by"] == "agent:reliability-audit"

    data = _event(events)
    assert (data["kept"], data["filled"], data["replaced_baseline"]) == (1, 1, 1)
    assert data["rated"] == 3 and data["failed"] == ""


def test_降级基线同样被覆盖(tmp_path: Path) -> None:
    coordinator, store, plan, goal, events, urls = _fixture(tmp_path)
    for url in urls:
        _seed(store, url, scores=_BASELINE_SCORES, notes=_BASELINE_NOTES,
              rated_by="baseline:xhs@v1:degraded")

    asyncio.run(coordinator._persist_goal_evidence(plan, goal))

    for url, row in _rows(store).items():
        assert row["rated_by"] == "agent:reliability-audit", url
        assert row["rating_notes"] == _OLD_NOTES
    data = _event(events)
    assert (data["kept"], data["filled"], data["replaced_baseline"]) == (0, 0, 3)


def test_产物某维为问号时基线格子不残留(tmp_path: Path) -> None:
    """整块换：产物交叉维评不出（NULL + `?`），基线里那格有分也不许留下配产物备注。"""

    import json

    coordinator, store, plan, goal, events, urls = _fixture(tmp_path)
    full_baseline = {**_BASELINE_SCORES, "score_crossref": 1}
    full_notes = _BASELINE_NOTES.replace("交叉?:缺断言血缘簇", "交叉1:弱交叉")
    for url in urls:
        _seed(store, url, scores=full_baseline, notes=full_notes,
              rated_by="baseline:douyin@v1")
    rating = tmp_path / "runs" / "r-d049" / "goals" / "goal-1" / "rating.json"
    items = json.loads(rating.read_text(encoding="utf-8"))
    for item in items:
        item["score_crossref"] = None
        item["rating_notes"] = _OLD_NOTES.replace("交叉1:弱交叉", "交叉?:未评")
    rating.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")

    asyncio.run(coordinator._persist_goal_evidence(plan, goal))

    for url, row in _rows(store).items():
        assert row["score_crossref"] is None, url
        assert row["score_authority"] == 2
        assert row["rated_by"] == "agent:reliability-audit"
    data = _event(events)
    assert data["failed"] == "" and data["replaced_baseline"] == 3
