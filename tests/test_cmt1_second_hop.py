"""§CMT-1 货 2：采集工具的评论二跳（with_comments）。"""

from __future__ import annotations

import asyncio
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from app.adapters.capability import Capability
from app.adapters.source_mcp import (
    DEFAULT_COMMENT_PLAN, SourceToolAdapter, resolve_comment_plan,
)
from app.sources.comments import Comment, CommentBatch
from app.store.dao import Store
from app.store.schema import initialize_database_if_empty


@contextmanager
def tempfile_dir():
    with tempfile.TemporaryDirectory() as directory:
        yield Path(directory)


SCHEMA_PATH = Path(__file__).resolve().parents[1] / "app" / "store" / "schema.sql"
REPORT = "r-cmt1"


def _capability() -> Capability:
    return Capability(
        tools=("source.xhs",), sources=("xhs",), network="sources_only",
    )


def _store(tmp_path: Path) -> Store:
    database = tmp_path / "owli.db"
    initialize_database_if_empty(database, SCHEMA_PATH)
    store = Store(database)
    store.create_report(
        id=REPORT, title="标题", research_question="问题",
        created_at="2026-09-03T00:00:00Z",
    )
    return store


def _post(index: int, *, likes: int) -> dict[str, Any]:
    return {
        "platform": "xhs", "source_type": "post",
        "platform_item_id": f"note-{index}",
        "permalink": f"https://www.xiaohongshu.com/explore/note-{index}?xsec_token=t",
        "title": f"笔记 {index}", "content_excerpt": "正文",
        "author_name": "作者", "source_keyword": "workbuddy",
        "fetch_method": "third_party_api", "published_at": None,
        "fetched_at": "2026-09-03T00:00:00Z",
        "raw_metrics": {"liked_count": likes},
    }


def _batch(note_id: str, parent: str, count: int) -> CommentBatch:
    return CommentBatch(
        comments=[
            Comment(
                parent_permalink=parent, permalink="", author=f"读者{i}",
                text=f"{note_id} 第 {i} 条读者反应，说得挺具体的",
                likes=i, published_at=None, platform="xhs",
                comment_id=f"{note_id}-c{i}",
            )
            for i in range(count)
        ],
        dropped_short=1, calls=2,
    )


def _run(
    tmp_path: Path, *, posts: int, with_comments: Any = None,
    per_post_returned: int = 20,
) -> tuple[Store, list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    store = _store(tmp_path)
    events: list[dict[str, Any]] = []
    asked: list[str] = []
    rows = [_post(i, likes=i * 10) for i in range(posts)]

    def source(
        query: str, window: str, *, store: Any = None, report_id: str = "",
        goal_id: str = "", agent_name: str = "", on_event: Any = None,
    ) -> list[dict[str, Any]]:
        # 和真源同构：一跳自己直落库（xhs/reddit/douyin 都是这么写的）
        if store is not None:
            store.upsert_evidence_batch([
                {**row, "id": f"ev-{report_id}-xhs-{row['platform_item_id']}",
                 "report_id": report_id, "goal_id": goal_id,
                 "agent_name": agent_name}
                for row in rows
            ])
        return rows

    def fetch(item_id: str, *, parent_permalink: str, limit: int) -> CommentBatch:
        asked.append(item_id)
        assert limit == (
            with_comments.get("per_post") if isinstance(with_comments, dict)
            and "per_post" in with_comments else DEFAULT_COMMENT_PLAN["per_post"]
        )
        return _batch(item_id, parent_permalink, per_post_returned)

    adapter = SourceToolAdapter(
        {"source.xhs": source, "source.xhs.comments": fetch}, store=store
    )
    result = asyncio.run(adapter.call(
        "source.xhs", "workbuddy", "30d", research_id=REPORT, goal_id="goal-1",
        agent_id="data-collection-xhs", capability=_capability(),
        on_event=events.append, with_comments=with_comments,
    ))
    return store, result, events, asked


def test_默认按_top_k_5_取前五帖并把评论并入本节_rows(tmp_path: Path) -> None:
    store, result, events, asked = _run(tmp_path, posts=8)

    # 按互动量降序取前 5 帖，不用上游 sort
    assert asked == [f"note-{i}" for i in (7, 6, 5, 4, 3)]
    comment_rows = [row for row in result if row.get("kind") == "comment"]
    assert len(result) == 8 + 5 * 20
    assert len(comment_rows) == 100

    stored = store.list_evidence(REPORT)
    comments = [row for row in stored if row["kind"] == "comment"]
    posts = {row["permalink"] for row in stored if row["kind"] == "post"}
    assert len(comments) >= 20
    assert all(row["parent_permalink"] in posts for row in comments)

    summary = [e for e in events if e["type"] == "source_yield_summary"]
    assert len(summary) == 1
    assert summary[0]["data"]["dropped_short"] == 5  # 每帖 1 条
    assert summary[0]["data"]["comments_kept"] == 100


def test_配额上限_调用数不超过一跳数加_top_k(tmp_path: Path) -> None:
    """判据 5：一次采集节的评论调用只针对 top_k 条帖子，不随帖数增长。"""

    _store_, _result, events, asked = _run(tmp_path, posts=30)
    summary = [e for e in events if e["type"] == "source_yield_summary"][0]["data"]
    one_hop_calls = 1  # 桩源一次搜索
    assert len(asked) == DEFAULT_COMMENT_PLAN["top_k"] == 5
    assert summary["parents_requested"] <= one_hop_calls + DEFAULT_COMMENT_PLAN["top_k"]


def test_帖子不足_top_k_时只按实有帖数发二跳(tmp_path: Path) -> None:
    _store_, result, _events, asked = _run(tmp_path, posts=2)
    assert len(asked) == 2
    assert len([row for row in result if row.get("kind") == "comment"]) == 40


def test_关掉_with_comments_就零评论行(tmp_path: Path) -> None:
    for switch in (False, "off", {"top_k": 0}, {"per_post": 0}):
        with tempfile_dir() as directory:
            store, result, events, asked = _run(
                directory, posts=5, with_comments=switch
            )
            assert asked == []
            assert [row for row in result if row.get("kind") == "comment"] == []
            assert [e for e in events if e["type"] == "source_yield_summary"] == []
            assert all(row["kind"] == "post" for row in store.list_evidence(REPORT))


def test_单帖评论失败只记账不掐整节(tmp_path: Path) -> None:
    store = _store(tmp_path)
    events: list[dict[str, Any]] = []
    rows = [_post(i, likes=i) for i in range(3)]

    def source(
        query: str, window: str, *, store: Any = None, report_id: str = "",
        goal_id: str = "", agent_name: str = "", on_event: Any = None,
    ) -> list[dict[str, Any]]:
        store.upsert_evidence_batch([
            {**row, "id": f"ev-x-{row['platform_item_id']}",
             "report_id": report_id, "goal_id": goal_id}
            for row in rows
        ])
        return rows

    def fetch(item_id: str, *, parent_permalink: str, limit: int) -> CommentBatch:
        if item_id == "note-1":
            raise RuntimeError("上游 429")
        return _batch(item_id, parent_permalink, 3)

    adapter = SourceToolAdapter(
        {"source.xhs": source, "source.xhs.comments": fetch}, store=store
    )
    result = asyncio.run(adapter.call(
        "source.xhs", "workbuddy", "30d", research_id=REPORT, goal_id="goal-1",
        agent_id="data-collection-xhs", capability=_capability(),
        on_event=events.append,
    ))
    partial = [e for e in events if e["type"] == "source_comment_partial"]
    summary = [e for e in events if e["type"] == "source_yield_summary"][0]["data"]
    assert len(partial) == 1 and partial[0]["data"]["reason"] == "RuntimeError"
    assert summary["parents_failed"] == 1 and summary["comments_kept"] == 6
    assert len([row for row in result if row.get("kind") == "comment"]) == 6


@pytest.mark.parametrize("value,expected", [
    (None, DEFAULT_COMMENT_PLAN),
    (True, DEFAULT_COMMENT_PLAN),
    ("on", DEFAULT_COMMENT_PLAN),
    ({"top_k": 2}, {"top_k": 2, "per_post": 20}),
    (False, None),
    ("off", None),
    ({}, None),
])
def test_with_comments_写法折算(value: Any, expected: Any) -> None:
    assert resolve_comment_plan(value) == expected


@pytest.mark.parametrize("bad", ["maybe", {"top_k": -1}, {"per_post": "20"}, 3])
def test_with_comments_非法写法当场报错(bad: Any) -> None:
    with pytest.raises((ValueError, TypeError)):
        resolve_comment_plan(bad)
