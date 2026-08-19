"""R2 默认路由与适配器选择；编排层不接触引擎分支。"""

from __future__ import annotations

from dataclasses import dataclass
import inspect
from typing import Any, Mapping


_DEFAULT_ENGINES = {
    "planning": "claude",
    # M0 固定链路兼容项：只证明路由生效，不在 M1 改写既有引擎分配。
    "m0_hn_collection": "claude",
    "goal_planning": "claude",
    "plan_arbitration": "claude",
    "audit": "claude",
    "reliability_audit": "claude",
    "cross_validation": "claude",
    "consistency_check": "claude",
    "report": "claude",
    "report_writing": "claude",
    "summary": "claude",
    "tagging": "claude",
    "code_execution": "codex",
    "excel_generation": "codex",
    "data_cleaning": "codex",
    "data_collection": "codex",
    "browser_automation": "codex",
}
_ENGINES = frozenset({"claude", "codex"})


@dataclass(frozen=True)
class EngineSelection:
    engine: str
    origin: str


def pick_engine(agent_kind: str, user_override: str | None) -> EngineSelection:
    """按 R2 选默认引擎；显式覆盖保留 origin=user。"""

    if user_override is not None:
        selected = user_override.strip().casefold()
        if selected not in _ENGINES:
            raise ValueError(f"不支持的用户引擎覆盖：{user_override}")
        return EngineSelection(selected, "user")
    try:
        return EngineSelection(_DEFAULT_ENGINES[agent_kind], "system")
    except KeyError as exc:
        raise ValueError(f"未知 agent_kind：{agent_kind}") from exc


class RoutedAdapter:
    """在适配层内完成路由并把统一结果交回编排层。"""

    def __init__(self, *, adapters: Mapping[str, Any] | None = None) -> None:
        if adapters is None:
            from app.adapters.claude import ClaudeAdapter
            from app.adapters.codex import CodexAdapter

            adapters = {"claude": ClaudeAdapter(), "codex": CodexAdapter()}
        missing = _ENGINES - set(adapters)
        if missing:
            raise ValueError(f"缺少引擎适配器：{','.join(sorted(missing))}")
        self._adapters = dict(adapters)
        self._active: Any = None
        self._future_engine: str | None = None

    @property
    def future_engine(self) -> str | None:
        """限流事件要求后续新任务让路时的适配层覆盖。"""

        return self._future_engine

    async def run(self, task: Any, ctx: Any, on_event: Any = None) -> Any:
        selection = pick_engine(task.agent_kind, task.user_override)
        selected_engine = self._future_engine or selection.engine
        adapter = self._adapters[selected_engine]

        async def routed_event(event: Any) -> None:
            target = getattr(event, "failover_target", None)
            scope = getattr(event, "scope", None)
            if target is not None and scope == "new_tasks":
                if target not in _ENGINES:
                    raise ValueError(f"未知限流让路目标：{target}")
                self._future_engine = target
            if on_event is not None:
                callback_result = on_event(event)
                if inspect.isawaitable(callback_result):
                    await callback_result

        self._active = adapter
        try:
            return await adapter.run(task, ctx, on_event=routed_event)
        finally:
            self._active = None

    async def interrupt(self) -> None:
        if self._active is None:
            raise RuntimeError("当前没有运行中的引擎任务")
        await self._active.interrupt()
