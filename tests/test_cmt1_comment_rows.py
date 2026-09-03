"""§CMT-1 货 3：evidence 的 kind / parent_permalink 两列、迁移与回退去重键。"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from app.store.dao import Store
from app.store.schema import initialize_database_if_empty


SCHEMA_PATH = Path(__file__).resolve().parents[1] / "app" / "store" / "schema.sql"
PARENT = "https://www.xiaohongshu.com/explore/note-1?xsec_token=aaa"


def _database(tmp_path: Path) -> Path:
    database = tmp_path / "owli.db"
    initialize_database_if_empty(database, SCHEMA_PATH)
    return database


def _report(store: Store) -> str:
    store.create_report(
        id="r-1", title="标题", research_question="问题",
        created_at="2026-09-03T00:00:00Z",
    )
    return "r-1"


def _row(**overrides: Any) -> dict[str, Any]:
    payload = {
        "id": "ev-1", "report_id": "r-1", "platform": "xhs",
        "permalink": PARENT, "fetched_at": "2026-09-03T00:00:00Z",
    }
    payload.update(overrides)
    return payload


def test_默认_kind_是_post_且父帖链接为空(tmp_path: Path) -> None:
    store = Store(_database(tmp_path))
    _report(store)
    store.upsert_evidence_batch([_row()])
    row = store.list_evidence("r-1")[0]
    assert row["kind"] == "post"
    assert row["parent_permalink"] is None


def test_评论行必须带父帖链接且_kind_是闭集(tmp_path: Path) -> None:
    store = Store(_database(tmp_path))
    _report(store)
    with pytest.raises(ValueError, match="parent_permalink"):
        store.upsert_evidence_batch([_row(kind="comment")])
    with pytest.raises(ValueError, match="kind 不在闭集"):
        store.upsert_evidence_batch([_row(kind="reply", parent_permalink=PARENT)])


def _comment(index: int, *, permalink: str, **overrides: Any) -> dict[str, Any]:
    payload = _row(
        id=f"ev-c{index}", permalink=permalink, kind="comment",
        parent_permalink=PARENT, source_type="comment",
        author_name=f"读者{index}", content_excerpt=f"第{index}条读者反应，写得挺长的",
    )
    payload.update(overrides)
    return payload


def test_无原生_ID_的评论重复入库不翻倍_即使合成链接漂了(tmp_path: Path) -> None:
    """小红书评论的 permalink 是拿父帖链接合成的，父帖签名一换它就变。

    只靠 permalink 认行会在下一轮重放里再插一遍；回退键按
    父帖 + 作者 + 正文前 64 字认出是同一条。
    """
    store = Store(_database(tmp_path))
    _report(store)
    store.upsert_evidence_batch([
        _comment(1, permalink=f"{PARENT}&owli_comment=c1"),
    ])
    store.upsert_evidence_batch([
        # 父帖签名换了 → 合成链接跟着变，但仍是同一条评论
        _comment(
            1, permalink=(
                "https://www.xiaohongshu.com/explore/note-1"
                "?xsec_token=bbb&owli_comment=c1"
            ),
            id="ev-c1-again",
        ),
    ])
    rows = store.list_evidence("r-1")
    assert len(rows) == 1
    assert rows[0]["id"] == "ev-c1"
    assert rows[0]["kind"] == "comment"
    assert rows[0]["parent_permalink"] == PARENT


def test_有原生_ID_的评论仍走既有身份键_回退键不越位(tmp_path: Path) -> None:
    store = Store(_database(tmp_path))
    _report(store)
    base = "https://www.reddit.com/r/x/comments/1/y"
    store.upsert_evidence_batch([
        _comment(2, permalink=f"{base}/aaa", platform="reddit",
                 platform_item_id="t1_aaa", parent_permalink=f"{base}"),
        # 同一父帖、同一作者、同一正文，但是**另一条**评论（原生 ID 不同）：
        # 回退键不得把它们并成一行。
        _comment(2, permalink=f"{base}/bbb", platform="reddit",
                 platform_item_id="t1_bbb", parent_permalink=f"{base}",
                 id="ev-c2b"),
    ])
    assert len(store.list_evidence("r-1")) == 2


def test_post_行不吃回退键(tmp_path: Path) -> None:
    store = Store(_database(tmp_path))
    _report(store)
    store.upsert_evidence_batch([
        _row(id="ev-p1", permalink="https://example.com/a", author_name="甲",
             content_excerpt="同样的正文"),
        _row(id="ev-p2", permalink="https://example.com/b", author_name="甲",
             content_excerpt="同样的正文"),
    ])
    assert len(store.list_evidence("r-1")) == 2


def test_真_v9_库迁移到_v10_补齐两列且既有证据不丢(tmp_path: Path) -> None:
    """既有的版本迁移用例都是「用新 schema 建库再改 user_version」，
    列本来就在，走不到 ALTER 那一路。这里把两列真删掉再迁。"""

    database = _database(tmp_path)
    store = Store(database)
    _report(store)
    store.upsert_evidence_batch([_row(title="迁移前就在的行")])
    with sqlite3.connect(database) as connection:
        connection.execute("DROP INDEX IF EXISTS idx_evidence_parent")
        connection.execute("ALTER TABLE evidence DROP COLUMN kind")
        connection.execute("ALTER TABLE evidence DROP COLUMN parent_permalink")
        connection.execute("PRAGMA user_version = 9")
        columns = {row[1] for row in connection.execute("PRAGMA table_xinfo(evidence)")}
    assert "kind" not in columns and "parent_permalink" not in columns

    initialize_database_if_empty(database, SCHEMA_PATH)

    with sqlite3.connect(database) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        columns = {row[1] for row in connection.execute("PRAGMA table_xinfo(evidence)")}
        indexes = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
    assert version == 10
    assert {"kind", "parent_permalink"} <= columns
    assert "idx_evidence_parent" in indexes
    rows = store.list_evidence("r-1")
    assert [row["title"] for row in rows] == ["迁移前就在的行"]
    assert rows[0]["kind"] == "post"  # 迁移给历史行补的默认值

    # 迁移完还能照常写评论行
    store.upsert_evidence_batch([_comment(9, permalink=f"{PARENT}&owli_comment=c9")])
    assert {row["kind"] for row in store.list_evidence("r-1")} == {"post", "comment"}


def test_迁移幂等_重复初始化不重跑_ALTER(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version = 9")
    initialize_database_if_empty(database, SCHEMA_PATH)
    initialize_database_if_empty(database, SCHEMA_PATH)
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 10
