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
