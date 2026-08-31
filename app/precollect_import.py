"""把预采集池里的一个平台批次导入某份报告的证据表。

    python -m app.precollect_import --platform weibo --report-id r-xxx \
        --goal-id goal-1 --agent-name data-collection [--query 茶叶] [--dry-run]

§M6-b 货 1。用途是**离线补录与验收取数**——研究期正常路径走
`app/sources/weibo.py` 薄源（同一个 `app.precollect` 映射，不是另一份实现）。
本命令不建报告、不改 schema；报告不存在直接报错，绝不静默造行。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE_PATH = ROOT / "var" / "owli.db"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.precollect_import")
    parser.add_argument("--platform", required=True, help="池目录名，如 weibo")
    parser.add_argument("--report-id", required=True)
    parser.add_argument("--goal-id", required=True)
    parser.add_argument("--agent-name", required=True,
                        help="章归属；为空的行算不到任何章头上（§M6-a 货 2）")
    parser.add_argument("--query", default=None, help="按关键词/正文过滤，不传则全导")
    parser.add_argument("--window", default=None, help="如 30d；只筛拿得到发布时间的行")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--pool-root", default=None)
    parser.add_argument("--database", default=str(DEFAULT_DATABASE_PATH))
    parser.add_argument("--dry-run", action="store_true",
                        help="只读池、只打读数，不落库")
    parser.add_argument("--no-prune", action="store_true",
                        help="跳过导入后的池定容清理（成功批留 5、失败批留 1）")
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    from app.precollect import load_evidence
    from app.reliability.scoring import normalize_evidence_metrics
    from app.store.dao import Store

    args = build_parser().parse_args(argv)
    result = load_evidence(
        args.platform, query=args.query, window=args.window,
        limit=args.limit, root=args.pool_root,
    )
    readout: dict[str, Any] = {
        "platform": args.platform,
        "batches_scanned": result.batches_scanned,
        "rows_seen": result.rows_seen,
        "matched": len(result.items),
        "dropped_by_query": result.dropped_by_query,
        "dropped_by_window": result.dropped_by_window,
        "failure_reasons": list(result.failure_reasons),
    }
    if not result.items:
        # 判据落在「取到几条」上，不落在「命令退没退出码 0」上。
        readout["closed_reason"] = result.closed_reason
        print(json.dumps(readout, ensure_ascii=False, indent=2))
        return 2
    fetched_at = result.items[0]["fetched_at"]
    normalized = normalize_evidence_metrics(
        result.items, computed_at=fetched_at,
        report_id=args.report_id, goal_id=args.goal_id,
        queries=[args.query] if args.query else [],
        filters=f"precollect_pool;{args.platform}",
    )
    readout["normalized"] = len(normalized)
    if args.dry_run:
        readout["dry_run"] = True
        print(json.dumps(readout, ensure_ascii=False, indent=2))
        return 0
    store = Store(args.database)
    store.upsert_evidence_batch([
        {
            **item,
            "id": f"ev-{args.report_id}-{args.platform}-{item['platform_item_id']}",
            "report_id": args.report_id,
            "goal_id": args.goal_id,
            "agent_name": args.agent_name,
        }
        for item in normalized
    ])
    readout["written"] = len(normalized)
    readout["database"] = str(args.database)
    if not args.no_prune:
        # §M6-c 货 5：清理在导入时顺手做——只删池目录不碰库，失败批留 1
        # 是给登录卡当判据输入。dry-run 与「没导成」两条路都不清（见 return 2）。
        from app.precollect import prune_batches

        readout["pruned_batches"] = prune_batches(args.platform, root=args.pool_root)
    print(json.dumps(readout, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover - 入口薄壳
    sys.exit(run())
