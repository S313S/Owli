#!/usr/bin/env python3
"""§CODE-1：把已编码的 `extra.coding` 从一个库搬进另一个库，不重新调引擎。

    # 先对副本试跑（默认行为：不带 --commit 只演练，一个字节不写目标库）
    python3 scripts/acceptance/code1/import_coding.py \
        --src <沙盒库> --dst <目标库> --research r-3e04f808dffd

    # 验过了再真写（会先自动 .backup 一份目标库，路径打印出来）
    python3 scripts/acceptance/code1/import_coding.py \
        --src ... --dst ... --research ... --commit

为什么不是一句 UPDATE：两边 `extra` 里除了 `coding` 还有 `authority_kind` /
`claim_ids` / `origin_key` 等，且**两边不一样**。整列覆盖会把目标库的引用关系
写坏，比不导更糟（「投影会覆盖适配器行」「upsert 只覆盖一个唯一键」都是这个坑）。
所以只 `json_set` 这一个键，其余原样不动。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

CODED = "json_extract(extra,'$.coding.coding_version') is not null"


def _ro(path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _count_coded(conn: sqlite3.Connection, research: str) -> int:
    return conn.execute(
        f"select count(*) from evidence where report_id=? and {CODED}", (research,)
    ).fetchone()[0]


def snapshot(src: str, out: Path) -> Path:
    """取一份库快照。**取快照只走这一个函数，别各自 `cp`。**

    `cp` 会漏 `-wal`：开着 WAL 的库，刚写入还没 checkpoint 的行只在 wal 文件里，
    单拷主库文件拿到的是**写入前**的状态。症状很坏——数据明明写进去了，下游
    却读不到，看起来像「功能没生效」，而不是像一个拷贝错误。本包 09-07 就这么
    自摆过一次假故障，而且脚本注释里当时就写着别用 cp：**写规则的人和用规则的
    人是同一个，照样会踩**。所以把它做成函数，不给踩的机会。
    """

    out.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(f"file:{src}?mode=ro", uri=True) as s, \
            sqlite3.connect(str(out)) as t:
        s.backup(t)
    return out


def backup(dst: str, backup_dir: Path) -> Path:
    """写库前留一份底。落在**本包自己的树里**，不往目标库那棵树写文件——
    那是别人的 worktree，除了库文件本身一个字节都不该多出来。"""

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = snapshot(dst, backup_dir / f"{Path(dst).name}.pre-coding-{stamp}.backup")
    print(f"[备份] {out}  ({out.stat().st_size} 字节)")
    return out


def plan(src: str, dst: str, research: str) -> tuple[dict[str, str], list[str]]:
    """算出要写哪些行。返回 (id → coding 的 JSON 文本, 目标库缺的 id)。"""

    with _ro(src) as s:
        coded = {
            row[0]: row[1] for row in s.execute(
                f"select id, json_extract(extra,'$.coding') from evidence "
                f"where report_id=? and {CODED}", (research,))
        }
    with _ro(dst) as d:
        present = {row[0] for row in d.execute(
            "select id from evidence where report_id=?", (research,))}
        already = {row[0] for row in d.execute(
            f"select id from evidence where report_id=? and {CODED}", (research,))}
    missing = sorted(set(coded) - present)
    # 幂等：目标库已经有编码的行不再写。重复跑第二次这里就是全集，写 0 行。
    todo = {eid: text for eid, text in coded.items()
            if eid in present and eid not in already}
    print(f"[盘点] 源库已编码 {len(coded)} 条；目标库对得上 {len(coded) - len(missing)} 条；"
          f"目标库已有编码 {len(already)} 条；本次要写 {len(todo)} 条；"
          f"源有目标无 {len(missing)} 条")
    return todo, missing


def apply(dst: str, todo: dict[str, str], research: str) -> int:
    """只 `json_set` 一个 `coding` 键，extra 其余键原样不动。"""

    written = 0
    with sqlite3.connect(dst) as conn:
        for eid, coding_text in todo.items():
            cur = conn.execute(
                "update evidence set extra=json_set(coalesce(extra,'{}'),'$.coding',json(?)) "
                "where id=? and report_id=?", (coding_text, eid, research))
            written += cur.rowcount
        conn.commit()
    return written


def verify(src: str, dst: str, research: str, expect: int) -> bool:
    """自带校验：条数对得上，且抽一条逐字比对 topics/attitude/scenario/quote。"""

    ok = True
    with _ro(dst) as d, _ro(src) as s:
        got = _count_coded(d, research)
        print(f"[校验] 目标库已编码 {got} 条，期望 {expect} 条 → {'对上' if got == expect else '对不上'}")
        ok &= got == expect
        row = d.execute(
            f"select id, json_extract(extra,'$.coding') from evidence "
            f"where report_id=? and {CODED} order by id limit 1", (research,)).fetchone()
        if row is None:
            print("[校验] 目标库一条编码都没有"); return False
        mine = json.loads(row[1])
        theirs = json.loads(s.execute(
            "select json_extract(extra,'$.coding') from evidence where id=?",
            (row[0],)).fetchone()[0])
        same = mine == theirs
        print(f"[校验] 抽 {row[0]}：attitude={mine.get('attitude')} "
              f"topics={mine.get('topics')} → 与源库逐字{'相同' if same else '不同'}")
        ok &= same
        # extra 的其它键必须还在——整列覆盖那种错法，这里当场就能抓出来。
        keys = json.loads(d.execute(
            "select extra from evidence where id=?", (row[0],)).fetchone()[0])
        print(f"[校验] 该行 extra 顶层键 {len(keys)} 个：{sorted(keys)[:6]}…")
        ok &= len(keys) > 1
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description="把 extra.coding 从源库搬进目标库")
    ap.add_argument("--src", required=True, help="有编码的源库（只读打开）")
    ap.add_argument("--dst", required=True, help="要写入的目标库")
    ap.add_argument("--research", required=True)
    ap.add_argument("--commit", action="store_true",
                    help="真写。不给就是演练：只盘点不写一个字节")
    ap.add_argument("--backup-dir", default="var/backups",
                    help="备份落在本包自己的树里，不往目标库那棵树写文件")
    ap.add_argument("--snapshot-to", metavar="路径",
                    help="只取一份 --dst 的库快照到这里就退出，不导入。"
                         "要拿库去验数时用它，别用 cp（cp 漏 -wal，会读到旧状态）")
    args = ap.parse_args()

    if args.snapshot_to:
        out = snapshot(args.dst, Path(args.snapshot_to))
        print(f"[快照] {out}  ({out.stat().st_size} 字节)")
        return 0

    todo, missing = plan(args.src, args.dst, args.research)
    if missing:
        print(f"[警告] 有 {len(missing)} 条源库编过、目标库里没有，跳过：{missing[:5]}")
    if not args.commit:
        print(f"[演练] 未写入。加 --commit 才真写（会先备份）。待写 {len(todo)} 条。")
        return 0
    if todo:
        backup(args.dst, Path(args.backup_dir))
    written = apply(args.dst, todo, args.research)
    print(f"[写入] {written} 行")
    with _ro(args.src) as s:
        expect = _count_coded(s, args.research) - len(missing)
    return 0 if verify(args.src, args.dst, args.research, expect) else 1


if __name__ == "__main__":
    sys.exit(main())
