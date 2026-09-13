"""§OBS-7：研究级费用观测——账外模型调用记账 + 读侧费用汇总。

口径一律「标价折算」，不写「实付」（两个引擎都走订阅额度池）。

- 货 2：不经章账本的引擎调用（正式稿、收尾评级回填）套一层 `UsageMeteringAdapter`，
  把终态 usage 按路径名累加进 `reports.extra.llm_usage_offledger`；累加规则与章账本
  共用 `app.store.dao.accumulate_llm_usage`，不另写一套。
"""

from __future__ import annotations

import inspect
import logging
from typing import Any

from app.observability.pricing import engine_key, priced_usage

logger = logging.getLogger(__name__)

OFFLEDGER_KEY = "llm_usage_offledger"


def record_offledger_usage(
    store: Any,
    research_id: str,
    path_name: str,
    usage: dict[str, Any],
    *,
    engine: str | None,
    cost_source: str | None,
) -> dict[str, Any]:
    """一次账外调用并进 `reports.extra.llm_usage_offledger[path_name]`。"""
    from app.export.registry import _update_extra   # 沿用导出登记的 extra 写法（含扩展键登记）
    from app.store.dao import accumulate_llm_usage

    def mutate(extra: dict[str, Any]) -> dict[str, Any]:
        ledger = dict(extra.get(OFFLEDGER_KEY) or {})
        ledger[path_name] = accumulate_llm_usage(
            ledger.get(path_name), usage, engine=engine, cost_source=cost_source
        )
        extra[OFFLEDGER_KEY] = ledger
        return {OFFLEDGER_KEY: ledger}

    return _update_extra(store, research_id, mutate)


class UsageMeteringAdapter:
    """透明包一层适配器：只截终态 usage 记账，事件照原样转给调用方。

    `app/adapters/` 是禁区，所以在调用方套壳；记账失败只落日志，不影响引擎调用本身。
    """

    def __init__(self, inner: Any, *, store: Any, research_id: str, path_name: str) -> None:
        self._inner = inner
        self._store = store
        self._research_id = research_id
        self._path_name = path_name

    def __getattr__(self, name: str) -> Any:       # timeout_seconds 等一律透传
        return getattr(self._inner, name)

    async def run(self, task: Any, ctx: Any, on_event: Any = None) -> Any:
        async def metered(event: Any) -> Any:
            usage = None if isinstance(event, dict) else getattr(event, "usage", None)
            if isinstance(usage, dict):
                try:
                    engine = engine_key(getattr(event, "engine", None))
                    ledger_usage, cost_source = priced_usage(
                        engine, usage, model=getattr(task, "model", None)
                    )
                    record_offledger_usage(
                        self._store, self._research_id, self._path_name, ledger_usage,
                        engine=engine, cost_source=cost_source,
                    )
                except Exception:  # noqa: BLE001 — 计量失败不改变调用结果
                    logger.exception("账外 LLM usage 计量失败：%s/%s",
                                     self._research_id, self._path_name)
            if on_event is None:
                return None
            result = on_event(event)
            return await result if inspect.isawaitable(result) else result

        result = self._inner.run(task, ctx, on_event=metered)
        return await result if inspect.isawaitable(result) else result


# ── 货 3：读侧费用汇总 ──────────────────────────────────────────────────────
#
# 源费不写 source_usage 两表（那是 X 源的日读数配额表，没有研究号也没有金额列）。
# 各源每轮收尾发一条 `source_usage_reconciled`，`data.calls` 是按端点的调用次数，
# 按研究号聚合即得「这轮打了几次、标价折算多少钱」。
COST_BASIS = "标价折算"
#: 供应商 → (计价方式, 单价美元/次)。count_only = 订阅制只计次不计价；pool = 预采集池零边际费。
SOURCE_PRICES: dict[str, tuple[str, float | None]] = {
    "tikhub": ("per_call", 0.001),
    "prowlo": ("count_only", None),
    "media_crawler": ("pool", 0.0),
}
PAID_PROVIDERS = frozenset({"tikhub", "prowlo"})
_FAILURE_EVENT_TYPES = ("source_unavailable", "source_empty")


def _rows(connection_or_store: Any, sql: str, params: tuple[Any, ...]) -> list[Any]:
    connect = getattr(connection_or_store, "_connect", None)
    if connect is None:
        return list(connection_or_store.execute(sql, params))
    with connect() as connection:
        return list(connection.execute(sql, params))


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def source_fee_summary(connection_or_store: Any, research_id: str) -> dict[str, Any]:
    """按研究号聚合源调用次数与标价折算。接受 sqlite 连接或 Store。"""
    import json

    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for (payload,) in _rows(
        connection_or_store,
        "SELECT payload FROM events WHERE research_id = ? AND type = 'source_usage_reconciled' "
        "ORDER BY sequence",
        (research_id,),
    ):
        data = (json.loads(payload) or {}).get("data") or {}
        platform = str(data.get("source") or data.get("platform") or "unknown")
        provider = str(data.get("provider") or ("x_api" if platform == "x" else "unknown"))
        row = groups.setdefault((platform, provider), {
            "platform": platform, "provider": provider, "rounds": 0, "failed_rounds": 0,
            "calls": 0, "by_endpoint": {},
        })
        row["rounds"] += 1
        if data.get("outcome") in {"unavailable", "empty"}:
            row["failed_rounds"] += 1
        calls = data.get("calls")
        if isinstance(calls, dict):
            for endpoint, count in calls.items():
                row["calls"] += _int(count)
                row["by_endpoint"][endpoint] = row["by_endpoint"].get(endpoint, 0) + _int(count)
        elif platform == "x":
            # X 源口径是「新计费读数」，价格由源自己按配置单价算好。
            row["calls"] += _int(data.get("newly_billed"))
            try:
                row["x_charged_usd"] = row.get("x_charged_usd", 0.0) + float(data.get("charged_usd") or 0)
            except (TypeError, ValueError):
                pass

    rows = []
    total_cost = 0.0
    for row in groups.values():
        mode, unit = SOURCE_PRICES.get(row["provider"], ("count_only", None))
        if row["platform"] == "x" and "x_charged_usd" in row:
            mode, cost = "source_reported", row.pop("x_charged_usd")
        else:
            cost = row["calls"] * unit if unit is not None else None
        row.update({"pricing": mode, "unit_price_usd": unit, "cost_usd": cost})
        total_cost += cost or 0.0
        rows.append(row)
    rows.sort(key=lambda item: (item["platform"], item["provider"]))

    # 老数据（或漏补的分支）：付费源失败/空轮没带调用次数，只能标「计数缺失」。
    missing = 0
    for event_type, payload in _rows(
        connection_or_store,
        "SELECT type, payload FROM events WHERE research_id = ? AND type IN (?, ?)",
        (research_id, *_FAILURE_EVENT_TYPES),
    ):
        data = (json.loads(payload) or {}).get("data") or {}
        providers = {data.get("provider")} if data.get("provider") else {
            item.get("provider") for item in data.get("failures") or [] if isinstance(item, dict)
        }
        if providers & PAID_PROVIDERS and not isinstance(data.get("calls"), dict):
            missing += 1
    return {
        "basis": COST_BASIS,
        "rows": rows,
        "total_cost_usd": total_cost,
        "calls": sum(row["calls"] for row in rows),
        "rounds_without_calls": missing,
    }


def llm_cost_summary(store: Any, research_id: str) -> dict[str, Any]:
    """章账本 + 账外路径合成研究级模型费；引擎报价与标价折算分列。"""
    from app.store.dao import _empty_usage_bucket, _merge_usage_bucket

    chapters = store.aggregate_research_usage(research_id)
    report = store.get_report(research_id) or {}
    offledger = dict(((report.get("extra") or {}).get(OFFLEDGER_KEY)) or {})
    total = _empty_usage_bucket()
    total["cost_usd"] = 0.0
    by_engine: dict[str, dict[str, Any]] = {}
    for bucket in (chapters, *offledger.values()):
        if not isinstance(bucket, dict):
            continue
        _merge_usage_bucket(total, bucket)
        for engine, sub in (bucket.get("by_engine") or {}).items():
            if isinstance(sub, dict):
                _merge_usage_bucket(by_engine.setdefault(str(engine), _empty_usage_bucket()), sub)
    for sub in by_engine.values():
        sub["cost_usd"] = float(sub["cost_usd"] or 0.0)
    uncosted = total["calls"] - total["costed_calls"] - total["estimated_calls"]
    return {
        "basis": COST_BASIS,
        "calls": total["calls"],
        "reported_calls": total["costed_calls"],
        "reported_cost_usd": total["cost_usd"],
        "estimated_calls": total["estimated_calls"],
        "estimated_cost_usd": total["estimated_cost_usd"],
        "uncosted_calls": uncosted,
        "cost_usd": total["cost_usd"] + total["estimated_cost_usd"],
        "by_engine": by_engine,
        "chapters": chapters,
        "offledger": offledger,
    }


def research_cost_summary(store: Any, research_id: str) -> dict[str, Any]:
    """研究级费用一张表：模型费 + 源费，口径「标价折算」。"""
    llm = llm_cost_summary(store, research_id)
    sources = source_fee_summary(store, research_id)
    return {
        "research_id": research_id,
        "basis": COST_BASIS,
        "llm": llm,
        "sources": sources,
        "total_cost_usd": llm["cost_usd"] + sources["total_cost_usd"],
    }
