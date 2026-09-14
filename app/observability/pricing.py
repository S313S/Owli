"""§OBS-7 货 1：模型标价表与「标价折算」。

口径（用户 09-13 拍）：两个引擎都走订阅额度池，本项目**没有实付模型费**。
Claude SDK 回的 `total_cost_usd` 记作「引擎报价」（reported），本模块按 token × 标价
算出来的记作「标价折算」（estimated）。两者分开记账、分开汇总，任何地方都不写「实付」。

- 单价单位：美元 / 百万 token。
- Claude 的 `input_tokens` 不含缓存读写，四项分别计价。
- Codex（OpenAI 口径）的 `cached_input_tokens` 是 `input_tokens` 的子集，
  `reasoning_output_tokens` 已含在 `output_tokens` 里，不重复计价。
- 标定（alloc1 两轮纯 Claude、115/115 与 111/111 次都带引擎报价）：Claude 行按本表
  折算与引擎报价偏差 +2.0% / +5.1%（见 worklog §六）。
- Codex 行（§OBS-7-fu 09-14）：实际跑的是 ~/.codex/config.toml 里的 gpt-5.6-terra，
  按它 2026-07-30 起的标价计（两家第三方价目一致，OpenAI 官方页未能直读）。Codex 事件
  不带型号、折算时传进来的是计划型号，所以型号表只在「型号所属引擎 == 实际引擎」时生效，
  其余一律回落引擎默认价——Codex 未知型号即按 terra 计。无报价可标定。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

PRICE_TABLE_VERSION = "2026-09-14"


@dataclass(frozen=True)
class ModelPrice:
    input: float
    cached_input: float
    cache_write: float
    output: float
    #: True = cached_input 含在 input 里（OpenAI 口径）；False = 分列（Anthropic 口径）
    cached_within_input: bool
    #: 这份标价对应的型号、生效日期与出处（只作说明，不参与计算）
    model: str = ""
    effective_from: str = ""
    sources: tuple[str, ...] = ()


GPT_5_6_TERRA = ModelPrice(
    input=2.0, cached_input=0.2, cache_write=2.5, output=12.0, cached_within_input=True,
    model="gpt-5.6-terra", effective_from="2026-07-30",
    sources=(
        "https://www.requesty.ai/models/openai-responses/gpt-5.6-terra",
        "https://layer3labs.io/guides/gpt-5-6-pricing",
    ),
)


#: 引擎默认标价；`MODEL_PRICES` 里有精确型号就优先用型号。
ENGINE_PRICES: dict[str, ModelPrice] = {
    "claude": ModelPrice(input=5.0, cached_input=0.5, cache_write=10.0, output=25.0,
                         cached_within_input=False),
    "codex": GPT_5_6_TERRA,
}
MODEL_PRICES: dict[str, ModelPrice] = {GPT_5_6_TERRA.model: GPT_5_6_TERRA}
#: 型号 → 所属引擎；型号表只在实际引擎对得上时生效
MODEL_ENGINES: dict[str, str] = {GPT_5_6_TERRA.model: "codex"}

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
    key = engine_key(engine)
    if key is None:
        return None
    price = ENGINE_PRICES[key]
    if model and MODEL_ENGINES.get(str(model)) == key:
        price = MODEL_PRICES[str(model)]
    input_tokens = _tokens(usage, "input_tokens")
    cached = _tokens(usage, "cached_input_tokens")
    cache_write = _tokens(usage, "cache_creation_input_tokens") + _tokens(usage, "cache_write_input_tokens")
    output = _tokens(usage, "output_tokens")
    # OpenAI 口径缓存读写都算在 input_tokens 里，扣掉再按新输入计，避免重复计价
    fresh_input = (
        max(input_tokens - cached - cache_write, 0) if price.cached_within_input else input_tokens
    )
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


def price_table_note() -> dict[str, Any]:
    """标价表说明（版本、各引擎按哪个型号的价、生效日期与出处），给费用接口带出。"""
    return {
        "version": PRICE_TABLE_VERSION,
        "engines": {
            engine: {
                "model": price.model or None,
                "effective_from": price.effective_from or None,
                "sources": list(price.sources),
            }
            for engine, price in ENGINE_PRICES.items()
        },
    }
