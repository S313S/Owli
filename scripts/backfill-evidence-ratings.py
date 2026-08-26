#!/usr/bin/env python3
"""对既有报告执行入库后补评，并输出可解析的验收计数。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.adapters.routing import RoutedAdapter  # noqa: E402
from app.reliability.backfill import backfill_report  # noqa: E402
from app.store.dao import Store  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="按 source-reliability 口径对已入库 evidence 异步补评"
    )
    parser.add_argument("database", type=Path, help="Owli SQLite 数据库路径")
    parser.add_argument(
        "--report-id",
        action="append",
        required=True,
        help="待补评报告 ID；可重复指定",
    )
    parser.add_argument(
        "--runs-root", type=Path, default=ROOT / "runs", help="报告产物根目录"
    )
    parser.add_argument("--batch-size", type=int, default=25, help="单次审计条数（1–50）")
    parser.add_argument("--force", action="store_true", help="已完成五维的证据也重新补评")
    parser.add_argument(
        "--engine",
        choices=("claude", "codex"),
        default="claude",
        help="审计引擎（默认 claude；DIY 覆盖会如实写入 rated_by）",
    )
    return parser


async def _run(args: argparse.Namespace) -> dict:
    database = args.database.resolve()
    if not database.is_file():
        raise FileNotFoundError(f"数据库不存在：{database}")
    store = Store(database)
    adapter = RoutedAdapter()
    results = []
    for report_id in dict.fromkeys(args.report_id):
        result = await backfill_report(
            store,
            report_id,
            adapter=adapter,
            runs_root=args.runs_root.resolve(),
            batch_size=args.batch_size,
            force=args.force,
            engine_preference=args.engine,
        )
        results.append(asdict(result))
    ok = all(
        item["before_rows"] == item["after_rows"] and item["failed"] == 0
        for item in results
    )
    return {"ok": ok, "results": results}


def main() -> int:
    args = _parser().parse_args()
    try:
        payload = asyncio.run(_run(args))
    except Exception as exc:
        print(json.dumps({
            "ok": False,
            "error": {"type": type(exc).__name__, "message": str(exc)},
        }, ensure_ascii=False, separators=(",", ":")))
        return 1
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
