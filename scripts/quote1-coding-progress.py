#!/usr/bin/env python3
"""§QUOTE-1 重跑编码的只读进度尺。**盯的是「活有没有在推进」，不是「进程在不在」。**

⛔ 只读：以 `mode=ro` 打开权威库，一个字都不写。可以在编码跑着的时候反复跑。
⛔ 不自己写 SQL 判「编过没有」——直接调生产的 `coding_targets` / `is_coded`，
   自造的尺子和生产算法一旦不一致，量出来的数看着像缺陷，其实是尺子的
   （本项目 09-08 一天现形七次）。

用法：
  ../Owli/.venv/bin/python3 scripts/quote1-coding-progress.py \
      ../Owli-rpt1/var/shard1-serve.db r-3e04f808dffd
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.reliability.coding import (  # noqa: E402
    CODING_VERSION, coding_targets, is_coded,
)


def main() -> int:
    database = Path(sys.argv[1]).resolve()
    report_id = sys.argv[2] if len(sys.argv) > 2 else "r-3e04f808dffd"

    # ⛔ 只读连接：跑批的进程正拿着这个库写，绝不能让这把尺子插一脚。
    uri = f"file:{database}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = [dict(row) for row in connection.execute(
            "SELECT * FROM evidence WHERE report_id = ?", (report_id,)
        )]
    finally:
        connection.close()

    done = sum(1 for row in rows if is_coded(row))
    remaining = len(coding_targets(rows))          # force=False，生产的走法
    total = done + remaining
    percent = (100.0 * done / total) if total else 0.0
    print(f"report_id      {report_id}")
    print(f"编码版本       {CODING_VERSION}")
    print(f"已编码         {done}")
    print(f"还剩           {remaining}")
    print(f"进度           {done}/{total}  {percent:.1f}%")
    # 归零＝跑完。⛔ 判据落在这个数上，不落在日志或进程上。
    print(f"跑完了吗       {'是' if remaining == 0 else '否'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
