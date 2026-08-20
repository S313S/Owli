#!/usr/bin/env python3
"""M3-d X recent search 小额真实验收；只输出脱敏结构化结果。"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.sources.x import XRecentSearch, XSourceConfig, load_bearer_token  # noqa: E402
from app.store.schema import initialize_database_if_empty  # noqa: E402
from app.store.usage import SourceUsageStore  # noqa: E402


def _decimal(value: str) -> Decimal:
    try:
        return Decimal(value)
    except Exception as error:
        raise argparse.ArgumentTypeError("金额必须是十进制数字") from error


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="X recent search 小额真实验收")
    parser.add_argument("--query", required=True)
    parser.add_argument("--lang", default="en")
    parser.add_argument("--max-results", type=int, default=10, choices=range(10, 11))
    parser.add_argument("--min-likes", type=int, default=0)
    parser.add_argument("--min-retweets", type=int, default=0)
    parser.add_argument("--weekly-budget-usd", type=_decimal, required=True)
    parser.add_argument("--balance-usd", type=_decimal, required=True)
    parser.add_argument("--billing-cap-usd", type=_decimal, required=True)
    parser.add_argument("--cycle-spent-usd", type=_decimal, required=True)
    parser.add_argument("--price-per-read-usd", type=_decimal, required=True)
    parser.add_argument("--database", type=Path, default=ROOT / "var" / "owli.db")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    schema_path = ROOT / "app" / "store" / "schema.sql"
    initialize_database_if_empty(args.database, schema_path)
    events: list[dict[str, object]] = []
    source = XRecentSearch(
        config=XSourceConfig(
            api_base_url="https://api.x.com/2",
            weekly_budget_usd=args.weekly_budget_usd,
            balance_usd=args.balance_usd,
            billing_cycle_cap_usd=args.billing_cap_usd,
            billing_cycle_spent_usd=args.cycle_spent_usd,
            price_per_read_usd=args.price_per_read_usd,
        ),
        usage_store=SourceUsageStore(args.database),
        token_loader=load_bearer_token,
        on_event=events.append,
    )
    result = source.search(
        args.query,
        window="7d",
        lang=args.lang,
        max_results=args.max_results,
        min_likes=args.min_likes,
        min_retweets=args.min_retweets,
    )
    metrics = ("like_count", "retweet_count", "reply_count", "quote_count")
    checks = {
        "status_completed": result.conclusion.get("status") == "completed",
        "max_10": result.conclusion.get("before_filter", 0) <= 10,
        "metrics_four": all(
            set(item.get("raw_metrics", {})) == set(metrics)
            for item in result.evidence
        ),
        "permalink_shape": all(
            str(item.get("permalink", "")).startswith("https://x.com/i/status/")
            for item in result.evidence
        ),
    }
    output = {
        "structured_acceptance": "PASS" if all(checks.values()) else "FAIL",
        "checks": checks,
        "conclusion": result.conclusion,
        "evidence": [
            {
                "platform_item_id": item["platform_item_id"],
                "permalink": item["permalink"],
                "raw_metrics": item["raw_metrics"],
            }
            for item in result.evidence
        ],
        "events": events,
        "console_reconciliation": "PENDING_MANUAL_READ",
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
