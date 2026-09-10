"""§PROT-1 货 2：把被回投影抹成帖子的评论行改回 `kind='comment'` 并补回父帖链接。

只改两列，且只改「`source_type='comment'` 却 `kind='post'`」这一批——
`source_type` 早就在保护名单里，所以它是**采集期真值的幸存者**，拿它当判据比拿
标题前缀（「评论 ·」）当判据硬得多。

`parent_permalink` 不是猜出来的：`app/adapters/source_mcp.py:395-410` 造评论链接
的办法就是在**父帖链接**后面挂一个 `owli_comment=` 参数，所以剥掉这个参数再按
`dao.normalize_permalink` 归一，拿到的就是采集期写进去的那个值本身。

⛔ `goal_id` 那一列本脚本不碰——抖音 107 行搬不搬回 goal-1 属范围变更，要用户拍。

用法（默认只演练不落库）：
    python scripts/acceptance/prot1/repair_comment_kind.py --db <库> [--apply]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from app.store.dao import normalize_permalink  # noqa: E402
# 取快照只走这一个函数，别各自 cp——它的 docstring 里写着为什么。
from scripts.acceptance.code1.import_coding import snapshot  # noqa: E402

MARKER = "owli_comment"


def parent_of(permalink: str) -> str:
    """剥掉评论锚点参数，还原父帖链接（与采集期写入同口径归一）。"""

    parts = urlsplit(permalink)
    kept = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key != MARKER
    ]
    stripped = urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment)
    )
    return normalize_permalink(stripped)


def targets(connection: sqlite3.Connection, report_id: str) -> list[tuple[str, str]]:
    """待修行：采集期是评论、现在却挂着 post 的那一批。"""

    rows = connection.execute(
        "SELECT id, permalink FROM evidence "
        "WHERE report_id = ? AND source_type = 'comment' AND kind = 'post' "
        "ORDER BY id",
        (report_id,),
    ).fetchall()
    return [(str(row[0]), str(row[1])) for row in rows]


def verify(before: Path, after: Path, report_id: str) -> dict[str, int]:
    """全表逐行逐列比对备份与现库：只允许该改的两列有差异，其余一格不许动。

    比的是**整张 evidence 表**（不只待修的那 188 行），因为要证的不是「改对了」，
    是「没顺手改别的」。行集合也比——多一行少一行都算越界。
    """

    allowed = {"kind", "parent_permalink"}
    with sqlite3.connect(f"file:{before}?mode=ro", uri=True) as old, \
            sqlite3.connect(f"file:{after}?mode=ro", uri=True) as new:
        old.row_factory = sqlite3.Row
        new.row_factory = sqlite3.Row
        query = "SELECT * FROM evidence WHERE report_id = ? ORDER BY id"
        old_rows = {str(r["id"]): r for r in old.execute(query, (report_id,))}
        new_rows = {str(r["id"]): r for r in new.execute(query, (report_id,))}
    missing = set(old_rows) - set(new_rows)
    added = set(new_rows) - set(old_rows)
    compared = allowed_diff = out_of_bounds = 0
    offenders: list[str] = []
    for row_id, old_row in old_rows.items():
        new_row = new_rows.get(row_id)
        if new_row is None:
            continue
        for column in old_row.keys():
            compared += 1
            if old_row[column] == new_row[column]:
                continue
            if column in allowed:
                allowed_diff += 1
            else:
                out_of_bounds += 1
                if len(offenders) < 5:
                    offenders.append(f"{row_id}.{column}")
    return {
        "compared": compared, "allowed_diff": allowed_diff,
        "out_of_bounds": out_of_bounds, "rows_missing": len(missing),
        "rows_added": len(added), "offenders": offenders,
    }


def recount(connection: sqlite3.Connection, report_id: str) -> dict[str, int]:
    """复量：kind 与 source_type 两把尺子必须量出同一个数。"""

    one = lambda sql: connection.execute(sql, (report_id,)).fetchone()[0]
    return {
        "kind_comment": one(
            "SELECT COUNT(*) FROM evidence WHERE report_id=? AND kind='comment'"),
        "source_type_comment": one(
            "SELECT COUNT(*) FROM evidence WHERE report_id=? "
            "AND source_type='comment'"),
        "mismatch": one(
            "SELECT COUNT(*) FROM evidence WHERE report_id=? "
            "AND (source_type='comment') <> (kind='comment')"),
        "parent_null_comments": one(
            "SELECT COUNT(*) FROM evidence WHERE report_id=? AND kind='comment' "
            "AND (parent_permalink IS NULL OR parent_permalink='')"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="靶子库路径")
    parser.add_argument("--report-id", default="r-3e04f808dffd")
    parser.add_argument("--backup-dir", default=None,
                        help="快照落点，默认 <库同级>/backups")
    parser.add_argument("--apply", action="store_true",
                        help="真写库；不给就只演练")
    args = parser.parse_args()

    database = Path(args.db).resolve()
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as reader:
        pending = targets(reader, args.report_id)
        print("修前：", recount(reader, args.report_id))
    print(f"待修行数：{len(pending)}")
    repairs = [(row_id, parent_of(permalink)) for row_id, permalink in pending]
    stale = [row_id for row_id, parent in repairs if not parent]
    if stale:
        print(f"⛔ 有 {len(stale)} 行剥不出父帖链接，中止", file=sys.stderr)
        return 1
    if not args.apply:
        print("演练模式，未写库。样本：", repairs[:2])
        return 0

    backup_dir = Path(args.backup_dir) if args.backup_dir else database.parent / "backups"
    stamp = datetime.now().strftime("%H%M")
    backup = snapshot(str(database), backup_dir / f"{database.stem}.pre-prot1-{stamp}.db")
    print(f"快照（backup API，非 cp）：{backup}")

    with sqlite3.connect(str(database)) as writer:
        writer.executemany(
            "UPDATE evidence SET kind = 'comment', parent_permalink = ? WHERE id = ?",
            [(parent, row_id) for row_id, parent in repairs],
        )
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as reader:
        print("修后：", recount(reader, args.report_id))
    print("全表逐行逐列比对：", verify(backup, database, args.report_id))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
