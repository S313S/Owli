"""§OBS-7 货 1：模型标价表与「标价折算」。

口径（用户 09-13 拍）：两个引擎都走订阅额度池，本项目**没有实付模型费**。
Claude SDK 回的 `total_cost_usd` 记作「引擎报价」（reported），本模块按 token × 标价
算出来的记作「标价折算」（estimated）。两者分开记账、分开汇总，任何地方都不写「实付」。

- 单价单位：美元 / 百万 token。
- Claude 的 `input_tokens` 不含缓存读写，四项分别计价。
- Codex（OpenAI 口径）的 `cached_input_tokens` 是 `input_tokens` 的子集，
  `reasoning_output_tokens` 已含在 `output_tokens` 里，不重复计价。
- 标定（alloc1 两轮纯 Claude、115/115 与 111/111 次都带引擎报价）：Claude 行按本表
  折算与引擎报价偏差 +2.0% / +5.1%（见 worklog §六）。Codex 行无报价可标定，
  单价取 OpenAI GPT-5 系公开标价，**待用户核**。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

PRICE_TABLE_VERSION = "2026-09-13"


@dataclass(frozen=True)
class ModelPrice:
    input: float
    cached_input: float
    cache_write: float
    output: float
    #: True = cached_input 含在 input 里（OpenAI 口径）；False = 分列（Anthropic 口径）
    cached_within_input: bool


#: 引擎默认标价；`MODEL_PRICES` 里有精确型号就优先用型号。
ENGINE_PRICES: dict[str, ModelPrice] = {
    "claude": ModelPrice(input=5.0, cached_input=0.5, cache_write=10.0, output=25.0,
                         cached_within_input=False),
    "codex": ModelPrice(input=1.25, cached_input=0.125, cache_write=0.0, output=10.0,
                        cached_within_input=True),
}
MODEL_PRICES: dict[str, ModelPrice] = {}

COST_SOURCE_REPORTED = "reported"
COST_SOURCE_ESTIMATED = "estimated"
COST_SOURCES = frozenset({COST_SOURCE_REPORTED, COST_SOURCE_ESTIMATED})


def engine_key(engine: Any) -> str | None:
    """归一引擎名：适配器事件里是 `Claude`/`Codex`，账本列是 `claude`/`codex`。"""
    text = str(engine or "").strip().casefold()
    return text if text in ENGINE_PRICES else None


def _tokens(usage: Mapping[str, Any], key: str) -> int:
    value = usage.get(key, 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def estimate_cost_usd(engine: Any, usage: Mapping[str, Any], *, model: str | None = None) -> float | None:
    """按标价表折算一次调用的美元数；引擎不认识就返回 None（不瞎猜）。"""
    price = MODEL_PRICES.get(str(model)) if model else None
    if price is None:
        key = engine_key(engine)
        if key is None:
            return None
        price = ENGINE_PRICES[key]
    input_tokens = _tokens(usage, "input_tokens")
    cached = _tokens(usage, "cached_input_tokens")
    cache_write = _tokens(usage, "cache_creation_input_tokens") + _tokens(usage, "cache_write_input_tokens")
    output = _tokens(usage, "output_tokens")
    fresh_input = max(input_tokens - cached, 0) if price.cached_within_input else input_tokens
    cost = (
        fresh_input * price.input
        + cached * price.cached_input
        + cache_write * price.cache_write
        + output * price.output
    ) / 1_000_000
    return cost if math.isfinite(cost) and cost >= 0 else None


def priced_usage(engine: Any, usage: Mapping[str, Any], *, model: str | None = None) -> tuple[dict[str, Any], str | None]:
    """引擎报了价就原样用（reported）；没报就补标价折算（estimated）。

    返回 (交给账本的 usage, cost_source)。折算不出来时 cost_usd 仍是 None、来源为 None。
    """
    result = dict(usage)
    if isinstance(usage.get("cost_usd"), (int, float)) and not isinstance(usage.get("cost_usd"), bool):
        return result, COST_SOURCE_REPORTED
    estimated = estimate_cost_usd(engine, usage, model=model)
    result["cost_usd"] = estimated
    return result, (COST_SOURCE_ESTIMATED if estimated is not None else None)
