"""§M6-b 货 1：预采集池契约与导入器。

丁形态下公开仓不含任何抓取手法，唯一接口是池目录；因此这里验的是
「池怎么摆 → Owli 读成什么」，以及「池空/批次失败时说不说得出原因」。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _write_batch(
    root: Path, batch_id: str, rows: list[dict], *,
    platform: str = "weibo", manifest: dict | None = None,
) -> Path:
    directory = root / platform / batch_id
    jsonl_dir = directory / platform / "jsonl"
    jsonl_dir.mkdir(parents=True, exist_ok=True)
    payload = manifest if manifest is not None else {
        "platform": platform, "batch_id": batch_id, "status": "ok",
        "keywords": ["茶叶"], "collected_at": "2026-09-01T10:00:00Z",
        "item_count": len(rows),
    }
    (directory / "manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    with (jsonl_dir / "search_contents_2026-09-01.jsonl").open(
        "w", encoding="utf-8"
    ) as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return directory


def _row(note_id: str, *, content: str = "茶叶好喝", create_time: int = 1788228000):
    return {
        "note_id": note_id, "content": content,
        "create_time": create_time,
        "create_date_time": "2026-09-01 10:00:00",
        "liked_count": "12", "comments_count": "3", "shared_count": "1",
        "last_modify_ts": 1788231600000,
        "note_url": f"https://m.weibo.cn/detail/{note_id}",
        "ip_location": "浙江", "user_id": "u-1", "nickname": "茶友",
        "gender": "", "profile_url": "https://m.weibo.cn/u/1",
        "avatar": "", "source_keyword": "茶叶",
    }


def _store(tmp_path: Path, report_id: str):
    from app.store.dao import Store
    from app.store.schema import initialize_database_if_empty

    database = tmp_path / "owli.db"
    initialize_database_if_empty(database, ROOT / "app/store/schema.sql")
    store = Store(database)
    store.create_report(
        id=report_id, title="M6-b 预采集池导入",
        research_question="池里的行能不能原样入库",
        created_at="2026-09-01T00:00:00Z",
    )
    return store, database


def test_池内三行读成三条证据且字段按契约映射(tmp_path: Path) -> None:
    from app.precollect import load_evidence

    _write_batch(tmp_path, "20260901-1000-茶叶", [_row(f"n{i}") for i in range(3)])
    result = load_evidence("weibo", root=tmp_path)

    assert len(result.items) == 3
    assert result.batches_scanned == 1 and result.rows_seen == 3
    item = result.items[0]
    assert item["platform"] == "weibo"
    assert item["fetch_method"] == "media_crawler"
    assert item["permalink"].startswith("https://m.weibo.cn/detail/")
    assert item["platform_item_id"]
    assert item["source_keyword"] == "茶叶"
    assert item["published_at"] and item["published_at"].endswith("Z")
    assert item["raw_metrics"]["liked_count"] == 12
    # 基线不在源里手抄，问 PLATFORM_BASELINES（微博 1/2/0/1/1 = 5 C）。
    assert item["score_authority"] == 1 and item["score_crossref"] == 0


def test_行缺note_id当场报错不静默丢(tmp_path: Path) -> None:
    from app.precollect import PoolContractError, load_evidence

    _write_batch(tmp_path, "20260901-1001-茶叶", [{"content": "没有 id"}])
    with pytest.raises(PoolContractError):
        load_evidence("weibo", root=tmp_path)


def test_池空与批次失败各自说得出原因(tmp_path: Path) -> None:
    from app.precollect import load_evidence

    empty = load_evidence("weibo", root=tmp_path)
    assert empty.items == [] and empty.closed_reason == "precollect_pool_empty"

    _write_batch(
        tmp_path, "20260901-1002-茶叶", [],
        manifest={
            "platform": "weibo", "status": "failed",
            "failure": {"reason": "login_required", "detail": "扫码超时"},
        },
    )
    failed = load_evidence("weibo", root=tmp_path)
    assert failed.items == []
    assert failed.closed_reason == "login_required"


def test_导入器三行入库_重导不翻倍_agent_name非空(tmp_path: Path) -> None:
    import sqlite3

    from app.precollect_import import run

    pool = tmp_path / "pool"
    _write_batch(pool, "20260901-1003-茶叶", [_row(f"n{i}") for i in range(3)])
    report_id = "r-m6b-import"
    _store(tmp_path, report_id)
    argv = [
        "--platform", "weibo", "--report-id", report_id, "--goal-id", "goal-1",
        "--agent-name", "data-collection", "--pool-root", str(pool),
        "--database", str(tmp_path / "owli.db"),
    ]

    assert run(argv) == 0
    assert run(argv) == 0  # 重导

    with sqlite3.connect(tmp_path / "owli.db") as connection:
        connection.row_factory = sqlite3.Row
        rows = list(connection.execute(
            "SELECT platform, agent_name, fetch_method, grade, permalink "
            "FROM evidence WHERE report_id = ?", (report_id,),
        ))
    assert len(rows) == 3, "重导必须走 upsert，不许翻倍"
    assert {row["platform"] for row in rows} == {"weibo"}
    assert all(row["agent_name"] == "data-collection" for row in rows)
    assert all(row["fetch_method"] == "media_crawler" for row in rows)
    assert {row["grade"] for row in rows} == {"C"}


def test_导入器池空时退非零并说明原因(tmp_path: Path, capsys) -> None:
    from app.precollect_import import run

    report_id = "r-m6b-empty"
    _store(tmp_path, report_id)
    code = run([
        "--platform", "weibo", "--report-id", report_id, "--goal-id", "goal-1",
        "--agent-name", "data-collection", "--pool-root", str(tmp_path / "pool"),
        "--database", str(tmp_path / "owli.db"),
    ])
    assert code == 2
    readout = json.loads(capsys.readouterr().out)
    assert readout["matched"] == 0
    assert readout["closed_reason"] == "precollect_pool_empty"


def test_同note_id重复行去重后只算一条(tmp_path: Path) -> None:
    """真机读数解释：34 行 jsonl 读出 33 条，差的那条是重复博文。

    §M6-b 货 2 首采「茶叶」34 行里有 1 条同 `note_id` 的重复。去重沿用
    evidence 既有唯一键，不另造 key（[[upsert-covers-one-key-only]]）。
    """

    from app.precollect import load_evidence

    rows = [_row("dup"), _row("dup", content="同一条博文的第二次落盘"), _row("other")]
    _write_batch(tmp_path, "20260901-1200-茶叶", rows)
    result = load_evidence("weibo", root=tmp_path)

    assert result.rows_seen == 3
    assert len(result.items) == 2
    # 去重不算「被过滤掉」：那两个计数是给「窗筛/词筛」用的，别混在一起。
    assert result.dropped_by_query == 0 and result.dropped_by_window == 0
