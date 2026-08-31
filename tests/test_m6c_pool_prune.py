"""§M6-c 货 5：池批次定容清理——成功批留 5、失败批留 1、只删目录不碰库。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from tests.test_m6b_precollect_pool import _row, _store, _write_batch
from tests.test_m6c_pool_login_signal import LOGIN_MANIFEST


def _seven_batches(pool: Path) -> None:
    """5 成功 + 2 失败共 7 批；batch_id 前缀即时间序，越大越新。"""

    for hour in range(10, 14):  # 1000/1100/1200/1300 四个成功批
        _write_batch(pool, f"20260901-{hour}00-茶叶", [_row(f"n{hour}")])
    _write_batch(pool, "20260901-1400-茶叶", [], manifest=LOGIN_MANIFEST)
    _write_batch(pool, "20260901-1500-茶叶", [_row("n15")])  # 第 5 个成功批
    _write_batch(pool, "20260901-1600-茶叶", [], manifest=LOGIN_MANIFEST)


def test_七批清后剩五成功一失败(tmp_path: Path) -> None:
    from app.precollect import iter_batches, prune_batches

    _seven_batches(tmp_path)
    removed = prune_batches("weibo", root=tmp_path)

    assert removed == ["20260901-1400-茶叶"]  # 老的失败批被清，成功批一个不动
    left = iter_batches("weibo", root=tmp_path)
    assert len(left) == 6
    assert sum(1 for batch in left if batch.status == "failed") == 1
    assert left[0].batch_id == "20260901-1600-茶叶"  # 最新失败批保住（登录卡输入）


def test_成功批超五个时清最老的(tmp_path: Path) -> None:
    from app.precollect import iter_batches, prune_batches

    for hour in range(10, 17):  # 7 个成功批
        _write_batch(tmp_path, f"20260901-{hour}00-茶叶", [_row(f"n{hour}")])
    removed = prune_batches("weibo", root=tmp_path)

    assert removed == ["20260901-1100-茶叶", "20260901-1000-茶叶"]
    assert [batch.batch_id for batch in iter_batches("weibo", root=tmp_path)] == [
        f"20260901-{hour}00-茶叶" for hour in range(16, 11, -1)
    ]


def test_partial按成功档保留(tmp_path: Path) -> None:
    from app.precollect import iter_batches, prune_batches

    _write_batch(tmp_path, "20260901-1000-茶叶", [_row("n1")], manifest={
        "platform": "weibo", "status": "partial", "item_count": 1,
    })
    assert prune_batches("weibo", root=tmp_path) == []
    assert len(iter_batches("weibo", root=tmp_path)) == 1


def test_导入时顺手清且库行不受影响(tmp_path: Path) -> None:
    from app.precollect import iter_batches
    from app.precollect_import import run

    pool = tmp_path / "pool"
    _seven_batches(pool)
    store, database = _store(tmp_path, "r-m6c-prune")

    code = run([
        "--platform", "weibo", "--report-id", "r-m6c-prune",
        "--goal-id", "goal-1", "--agent-name", "微博数据抓取·茶叶",
        "--pool-root", str(pool), "--database", str(database),
    ])

    assert code == 0
    assert len(iter_batches("weibo", root=pool)) == 6  # 5 成功 + 1 失败
    with sqlite3.connect(database) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM evidence WHERE report_id = 'r-m6c-prune'"
        ).fetchone()[0]
    assert count == 5  # 五个成功批各一行，全在库里——清理只删目录不碰库


def test_no_prune旗标与dry_run都不清(tmp_path: Path) -> None:
    from app.precollect import iter_batches
    from app.precollect_import import run

    pool = tmp_path / "pool"
    _seven_batches(pool)
    store, database = _store(tmp_path, "r-m6c-keep")

    assert run([
        "--platform", "weibo", "--report-id", "r-m6c-keep",
        "--goal-id", "goal-1", "--agent-name", "微博数据抓取·茶叶",
        "--pool-root", str(pool), "--database", str(database), "--dry-run",
    ]) == 0
    assert len(iter_batches("weibo", root=pool)) == 7  # dry-run 不清

    assert run([
        "--platform", "weibo", "--report-id", "r-m6c-keep",
        "--goal-id", "goal-1", "--agent-name", "微博数据抓取·茶叶",
        "--pool-root", str(pool), "--database", str(database), "--no-prune",
    ]) == 0
    assert len(iter_batches("weibo", root=pool)) == 7  # 显式关掉也不清
