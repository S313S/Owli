#!/usr/bin/env python3
"""§POOL-1 第 2 步：把库里的 citation_no 按新工作稿对齐一次。零引擎。

**为什么需要这一步**：`app/replay/section.py` 只跑节——`replace_evidence_citations` /
`report_citations` / `validate` / `assemble` 在它里面全是零命中，沙盒本轮
`report_validation` 事件也是 0 条。所以重放**不回填**，跑完必然「工作稿是新号、
库是旧号」，正式稿起跑前那两条断言（`polish.citation_preflight`）会当场拦下。

**这一步不是 rescore**：rescore 会重跑评分**并顺带**无条件改写工作稿+重排角标
（而且只落在 `--database` 指到的那个库，正是 09-07 分家的成因）。这里只做
「按已有的新工作稿把库里的号对齐」，不碰分数、不碰工作稿。

**D-022 那个坑写死在护栏里**（`report_citations` 的 docstring 记着）：JSON 成稿把
正文以转义 `\\n` 塞在字符串里，解析不出角标时返回 0 条，而
`replace_evidence_citations` 会**反手把全库 citation_no 清成 NULL**。所以解析条数
必须 > 0、且与正文角标数一致，否则一个字都不写库。

    ../Owli/.venv/bin/python3 scripts/pool1_align_citations.py \
        --db var/shard1-serve.db --draft <新工作稿路径> --research r-3e04f808dffd [--apply]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.report.markdown import report_citations          # noqa: E402
from app.store.dao import Store                           # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, required=True)
    ap.add_argument("--draft", type=Path, required=True)
    ap.add_argument("--research", required=True)
    ap.add_argument("--apply", action="store_true", help="不给就是干跑，只报数不写库")
    args = ap.parse_args()

    text = args.draft.read_text(encoding="utf-8")
    citations = report_citations(text)
    body_marks = {int(n) for n in re.findall(r"\[S(\d{2,})\]", text)}

    print(f"工作稿 {args.draft}")
    print(f"  正文角标 {len(body_marks)} 个" + (f"（S{min(body_marks):02d}~S{max(body_marks):02d}）"
                                              if body_marks else ""))
    print(f"  解析出信息源 {len(citations)} 条")

    if not citations:
        print("❌ 解析出 0 条——写库会把全库 citation_no 清成 NULL（D-022）。不写，停下。")
        return 2
    if len(citations) != len(body_marks):
        print(f"❌ 解析 {len(citations)} 条 ≠ 正文角标 {len(body_marks)} 个，两者必须一致。不写，停下。")
        return 3
    if set(citations.values()) != body_marks:
        print("❌ 解析出的号与正文用的号不是同一批。不写，停下。")
        return 4

    if not args.apply:
        print("✅ 三条护栏全过（干跑，未写库）。加 --apply 才写。")
        return 0
    Store(args.db).replace_evidence_citations(args.research, citations)
    print(f"✅ 三条护栏全过，已写库 {len(citations)} 条。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
