#!/usr/bin/env python3
"""S-1 三源最小真实调用；产物和成本账只写 runs 白名单目录。"""

from __future__ import annotations

import argparse
import json
import re
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", choices=("xhs", "douyin", "reddit"), required=True)
    parser.add_argument("--query", required=True)
    parser.add_argument("--window", default="30d")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--research-id", default="s1-real-call")
    parser.add_argument("--goal-id", default="goal-1")
    parser.add_argument("--unit-price-usd", required=True)
    parser.add_argument("--monthly-budget-usd", default="30")
    parser.add_argument("--prior-billable-calls", type=int, default=0)
    parser.add_argument("--force-unavailable", action="store_true")
    return parser.parse_args()


def _money(value: str, label: str) -> Decimal:
    try:
        result = Decimal(value)
    except InvalidOperation as error:
        raise ValueError(f"{label} 必须是十进制金额") from error
    if result < 0:
        raise ValueError(f"{label} 不得为负数")
    return result


def _output_dir(research_id: str, goal_id: str) -> Path:
    if not SAFE_ID.fullmatch(research_id) or not SAFE_ID.fullmatch(goal_id):
        raise ValueError("research-id/goal-id 只能含字母、数字、下划线和连字符")
    runs_root = (ROOT / "runs").resolve()
    output = (runs_root / research_id / "goals" / goal_id).resolve()
    if runs_root not in output.parents:
        raise ValueError("输出目录越过 runs 白名单")
    output.mkdir(parents=True, exist_ok=True)
    return output


def _call_source(args: argparse.Namespace, events: list[dict[str, Any]]) -> list[dict]:
    if args.source == "xhs":
        from app.sources.xhs import search

        return search(
            args.query,
            args.window,
            limit=args.limit or 10,
            on_event=events.append,
            force_unavailable=args.force_unavailable,
        )
    if args.source == "douyin":
        from app.sources.douyin import search

        return search(
            args.query,
            args.window,
            limit=args.limit or 10,
            comment_video_limit=min(3, args.limit or 10),
            on_event=events.append,
            force_unavailable=args.force_unavailable,
        )
    from app.sources.reddit import search

    if args.force_unavailable:
        raise ValueError("force-unavailable 仅用于 TikHub 单路径源")
    return search(
        args.query,
        args.window,
        limit=args.limit or 5,
        on_event=events.append,
    )


def _usage(events: list[dict[str, Any]]) -> dict[str, int]:
    usage_events = [
        event for event in events if event.get("type") == "source_usage_reconciled"
    ]
    if len(usage_events) != 1:
        raise AssertionError("必须且只能产生一个 source_usage_reconciled 事件")
    calls = usage_events[0].get("data", {}).get("calls")
    if not isinstance(calls, dict) or not all(
        isinstance(value, int) and value >= 0 for value in calls.values()
    ):
        raise AssertionError("调用次数必须是非负整数映射")
    return calls


def _billable_calls(source: str, calls: dict[str, int]) -> int:
    if source == "reddit":
        # Dataset/get_record 免费；Prowlo live read 才计日额度。
        return calls.get("live_read", 0)
    return sum(calls.values())


def _assert_evidence(source: str, evidence: list[dict], limit: int) -> None:
    minimum = 5 if source == "reddit" else min(10, limit)
    if len(evidence) < minimum:
        raise AssertionError(f"{source} 真实结果少于 {minimum} 条")
    for item in evidence:
        required = (
            ("title", "content_excerpt", "author_name", "permalink", "raw_metrics")
            if source in {"xhs", "douyin"}
            else ("title", "permalink", "raw_metrics")
        )
        if not all(item.get(name) for name in required):
            raise AssertionError(f"{source} 证据缺字段：{item.get('platform_item_id')}")
        if not str(item["permalink"]).startswith("https://"):
            raise AssertionError("permalink 不是可点 HTTP(S) 链接")
        if item.get("score_crossref") is not None:
            raise AssertionError("交叉维在断言簇生成前必须为 NULL")
        if "交叉?:缺断言血缘簇" not in str(item.get("rating_notes")):
            raise AssertionError("rating_notes 未写交叉维缺口")
    if source == "xhs" and any(item.get("published_at") for item in evidence):
        raise AssertionError("小红书相对发布时间不得写入 published_at")
    if source == "douyin" and not any(
        item.get("score_completeness") == 2
        and item.get("extra", {}).get("comment_texts")
        for item in evidence
    ):
        raise AssertionError("抖音没有评论正文完整度 2 的真实证据")
    if source == "reddit" and not all(
        item.get("norm_method") == "none" and item.get("normalized_score") is None
        for item in evidence
    ):
        raise AssertionError("Reddit 归一化必须为 none/NULL")


def main() -> None:
    args = _arguments()
    unit_price = _money(args.unit_price_usd, "unit-price-usd")
    monthly_budget = _money(args.monthly_budget_usd, "monthly-budget-usd")
    output_dir = _output_dir(args.research_id, args.goal_id)
    events: list[dict[str, Any]] = []
    evidence = _call_source(args, events)

    if args.force_unavailable:
        unavailable = [event for event in events if event.get("type") == "source_unavailable"]
        if evidence or len(unavailable) != 1:
            raise AssertionError("强制不可用必须返回空证据和唯一 unavailable 事件")
        data = unavailable[0].get("data", {})
        if data.get("reason") != "tool_unavailable" or not data.get("task_continues"):
            raise AssertionError("不可用出口必须是闭集 tool_unavailable 且研究继续")
        payload = {"status": "PASS", "source": args.source, "events": events}
        target = output_dir / f"{args.source}-tool-unavailable.json"
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"status": "PASS", "output": str(target)}, ensure_ascii=False))
        return

    effective_limit = args.limit or (5 if args.source == "reddit" else 10)
    diagnostic_path = output_dir / f"{args.source}-latest-diagnostic.json"
    diagnostic_path.write_text(
        json.dumps(
            {"source": args.source, "evidence": evidence, "events": events},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    _assert_evidence(args.source, evidence, effective_limit)
    calls = _usage(events)
    if args.prior_billable_calls < 0:
        raise ValueError("prior-billable-calls 不得为负数")
    current_billable_calls = _billable_calls(args.source, calls)
    billable_calls = current_billable_calls + args.prior_billable_calls
    actual_cost = unit_price * billable_calls
    ledger = {
        "source": args.source,
        "calls": calls,
        "current_billable_calls": current_billable_calls,
        "prior_billable_calls": args.prior_billable_calls,
        "billable_calls": billable_calls,
        "unit_price_usd": str(unit_price),
        "actual_cost_usd": str(actual_cost),
        "formula": f"{billable_calls} * {unit_price}",
        "monthly_budget_usd": str(monthly_budget),
        "within_monthly_budget": actual_cost <= monthly_budget,
    }
    payload = {
        "status": "PASS",
        "source": args.source,
        "query": args.query,
        "window": args.window,
        "evidence_count": len(evidence),
        "evidence": evidence,
        "events": events,
        "cost": ledger,
    }
    result_path = output_dir / f"{args.source}-real-call.json"
    cost_path = output_dir / f"{args.source}-cost-ledger.json"
    result_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    cost_path.write_text(
        json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    stored = json.loads(result_path.read_text(encoding="utf-8"))
    if stored.get("status") != "PASS" or stored.get("evidence_count") != len(evidence):
        raise AssertionError("真实调用产物落盘后校验失败")
    print(json.dumps({
        "status": "PASS",
        "source": args.source,
        "evidence_count": len(evidence),
        "result": str(result_path),
        "cost": str(cost_path),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
