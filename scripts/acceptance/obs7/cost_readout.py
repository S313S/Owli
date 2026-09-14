"""§OBS-7 三判据读数尺子（只读；传库快照路径，不碰原库）。

用法：python scripts/acceptance/obs7/cost_readout.py <db> [--research r-xxx]

判据 1（模型费，量在章账本 chapter_progress.extra.usage + reports.extra.llm_usage_offledger）：
    每次调用都有价 = 引擎报价次数 + 标价折算次数 == 调用次数。红 = 无价调用率 > 50%。
判据 2（源费，量在 events 表 source_usage_reconciled）：
    按研究号×平台×供应商聚合调用次数；与读侧汇总函数（app.observability.cost）逐字对。
判据 3（失败轮次，量在 events 表）：
    TikHub/Prowlo 源的 source_unavailable / source_empty 轮次里，没带调用次数的轮数。红 = > 0。
§OBS-7-fu Codex 标价对照（量在章账本 + 账外路径的 by_engine.codex 分项 token 累计）：
    按旧 GPT-5 系价与现行 gpt-5.6-terra 价各重算一次（折算对 token 线性，桶累计可直接乘价）；
    `ledger_estimated_usd` 是账本里当时记下的折算额，旧码写的行应与 old_usd 对上。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

PAID_PROVIDERS = {"tikhub", "prowlo"}
FAILURE_TYPES = ("source_unavailable", "source_empty")


def _usage_rows(connection: sqlite3.Connection, research_id: str):
    for (extra,) in connection.execute(
        "SELECT extra FROM chapter_progress WHERE research_id = ?", (research_id,)
    ):
        usage = json.loads(extra).get("usage")
        if isinstance(usage, dict):
            yield "chapter", usage
    row = connection.execute("SELECT extra FROM reports WHERE id = ?", (research_id,)).fetchone()
    offledger = (json.loads(row[0]).get("llm_usage_offledger") if row else None) or {}
    for path, usage in offledger.items():
        if isinstance(usage, dict):
            yield f"offledger:{path}", usage


def verdict_llm(connection: sqlite3.Connection, research_id: str) -> dict:
    totals: Counter = Counter()
    by_engine: dict[str, Counter] = defaultdict(Counter)
    for origin, usage in _usage_rows(connection, research_id):
        kind = "offledger" if origin.startswith("offledger") else "chapter"
        calls = int(usage.get("calls", 0) or 0)
        costed = int(usage.get("costed_calls", 0) or 0)
        estimated = int(usage.get("estimated_calls", 0) or 0)
        totals["calls"] += calls
        totals["reported_calls"] += costed
        totals["estimated_calls"] += estimated
        totals[f"{kind}_calls"] += calls
        totals["reported_usd"] += float(usage.get("cost_usd") or 0.0)
        totals["estimated_usd"] += float(usage.get("estimated_cost_usd") or 0.0)
        for engine, bucket in (usage.get("by_engine") or {}).items():
            by_engine[engine]["calls"] += int(bucket.get("calls", 0) or 0)
            by_engine[engine]["costed_calls"] += int(bucket.get("costed_calls", 0) or 0)
            by_engine[engine]["estimated_calls"] += int(bucket.get("estimated_calls", 0) or 0)
            by_engine[engine]["reported_usd"] += float(bucket.get("cost_usd") or 0.0)
            by_engine[engine]["estimated_usd"] += float(bucket.get("estimated_cost_usd") or 0.0)
    uncosted = totals["calls"] - totals["reported_calls"] - totals["estimated_calls"]
    rate = uncosted / totals["calls"] if totals["calls"] else 0.0
    # OBS-7 之后落的调用都带 by_engine；只看这一层才是「新码下每次调用都有价」的判据，
    # 重放导入的旧章账本行（没有 by_engine）不拿来判绿。
    new_calls = sum(bucket["calls"] for bucket in by_engine.values())
    new_costed = sum(bucket["costed_calls"] + bucket["estimated_calls"] for bucket in by_engine.values())
    return {
        "new_code_calls": new_calls,
        "new_code_uncosted_calls": new_calls - new_costed,
        "calls": totals["calls"],
        "chapter_calls": totals["chapter_calls"],
        "offledger_calls": totals["offledger_calls"],
        "reported_calls": totals["reported_calls"],
        "estimated_calls": totals["estimated_calls"],
        "uncosted_calls": uncosted,
        "uncosted_rate": round(rate, 4),
        "reported_usd": round(totals["reported_usd"], 4),
        "estimated_usd": round(totals["estimated_usd"], 4),
        "by_engine": {k: {kk: round(vv, 4) if isinstance(vv, float) else vv for kk, vv in v.items()}
                      for k, v in by_engine.items()},
        "red": rate > 0.5,
        "green": totals["calls"] > 0 and uncosted == 0,
    }


def raw_source_calls(connection: sqlite3.Connection, research_id: str) -> dict[str, int]:
    counts: Counter = Counter()
    for (payload,) in connection.execute(
        "SELECT payload FROM events WHERE research_id = ? AND type = 'source_usage_reconciled'",
        (research_id,),
    ):
        data = json.loads(payload).get("data") or {}
        key = f"{data.get('source') or data.get('platform')}/{data.get('provider')}"
        calls = data.get("calls")
        if isinstance(calls, dict):
            counts[key] += sum(v for v in calls.values() if isinstance(v, int) and not isinstance(v, bool))
        elif isinstance(data.get("requests"), int):
            counts[key] += data["requests"]
    return dict(counts)


def verdict_failure_rounds(connection: sqlite3.Connection, research_id: str) -> dict:
    rows = connection.execute(
        "SELECT type, payload FROM events WHERE research_id = ? AND type IN (?, ?)",
        (research_id, *FAILURE_TYPES),
    ).fetchall()
    missing: Counter = Counter()
    total: Counter = Counter()
    for event_type, payload in rows:
        data = json.loads(payload).get("data") or {}
        provider = data.get("provider")
        providers = {provider} if provider else {
            # Reddit 全挂时 failures 是 ["prowlo_unavailable", ...] 这种字符串
            (str(item.get("provider")) if isinstance(item, dict) else str(item).split("_")[0])
            for item in data.get("failures") or []
        }
        if not providers & PAID_PROVIDERS:
            continue
        key = f"{data.get('source') or data.get('platform')}/{event_type}"
        total[key] += 1
        if not isinstance(data.get("calls"), dict):
            missing[key] += 1
    return {"failure_rounds": dict(total), "rounds_without_calls": dict(missing),
            "red": sum(missing.values()) > 0}


TOKEN_FIELDS = ("input_tokens", "cached_input_tokens", "cache_creation_input_tokens",
                "cache_write_input_tokens", "output_tokens", "reasoning_output_tokens")


def verdict_codex_reprice(connection: sqlite3.Connection, research_id: str) -> dict:
    from app.observability.pricing import ENGINE_PRICES, ModelPrice, estimate_cost_usd  # noqa: PLC0415
    import app.observability.pricing as pricing  # noqa: PLC0415

    tokens: Counter = Counter()
    calls = 0
    ledger_usd = 0.0
    for _origin, usage in _usage_rows(connection, research_id):
        bucket = (usage.get("by_engine") or {}).get("codex")
        if not isinstance(bucket, dict):
            continue
        calls += int(bucket.get("calls", 0) or 0)
        ledger_usd += float(bucket.get("estimated_cost_usd") or 0.0)
        for key in TOKEN_FIELDS:
            tokens[key] += int(bucket.get(key, 0) or 0)
    new_usd = estimate_cost_usd("codex", tokens)
    # OBS-7 原表：GPT-5 系 1.25 / 0.125 / 10，无缓存写价
    old_price = ModelPrice(input=1.25, cached_input=0.125, cache_write=0.0, output=10.0,
                           cached_within_input=True)
    current = ENGINE_PRICES["codex"]
    try:
        pricing.ENGINE_PRICES["codex"] = old_price
        old_usd = estimate_cost_usd("codex", tokens)
    finally:
        pricing.ENGINE_PRICES["codex"] = current
    return {
        "codex_calls": calls,
        "tokens": dict(tokens),
        "ledger_estimated_usd": round(ledger_usd, 4),
        "old_usd": round(old_usd or 0.0, 4),
        "new_usd": round(new_usd or 0.0, 4),
        "delta_pct": round((new_usd - old_usd) / old_usd * 100, 1) if old_usd else None,
        "price_model": current.model,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("--research", action="append")
    args = parser.parse_args()
    connection = sqlite3.connect(f"file:{args.database}?mode=ro", uri=True)
    research_ids = args.research or [
        row[0] for row in connection.execute(
            "SELECT DISTINCT research_id FROM chapter_progress ORDER BY research_id")
    ]
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
        from app.observability.cost import source_fee_summary  # noqa: PLC0415
    except ImportError:
        source_fee_summary = None
    try:
        import app.observability.pricing  # noqa: F401, PLC0415
        has_pricing = True
    except ImportError:
        has_pricing = False
    report = {}
    for research_id in research_ids:
        raw = raw_source_calls(connection, research_id)
        entry = {
            "judge1_llm": verdict_llm(connection, research_id),
            "judge2_source_calls_raw": raw,
            "judge3_failure_rounds": verdict_failure_rounds(connection, research_id),
        }
        if source_fee_summary is not None:
            summary = source_fee_summary(connection, research_id)
            reader = {f"{row['platform']}/{row['provider']}": row["calls"] for row in summary["rows"]}
            entry["judge2_reader"] = summary
            entry["judge2_match"] = {k: v for k, v in raw.items() if v > 0} == \
                {k: v for k, v in reader.items() if v > 0}
        if has_pricing:
            entry["codex_reprice"] = verdict_codex_reprice(connection, research_id)
        report[research_id] = entry
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
