"""R2 默认路由与适配器选择；编排层不接触引擎分支。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import inspect
import json
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping

from app.adapters.circuitbreaker import CircuitTransition, ResearchCircuitBreaker
from app.adapters.contracts import PlanningSegmentRequest, PlanningSegmentResult
from app.adapters.events import ItemKind, NormalizedEvent
from app.adapters.source_mcp import (
    SourceToolAdapter,
    prepare_source_events,
    replay_source_events,
)
from app.config import ResilienceConfig, load_resilience_config


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
_PLANNING_KINDS = frozenset({"planning", "goal_planning", "plan_arbitration"})


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

    def __init__(
        self,
        *,
        adapters: Mapping[str, Any] | None = None,
        source_tools: Mapping[str, Any] | None = None,
        resilience_config: ResilienceConfig | None = None,
        probe_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        backoff_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if adapters is None:
            from app.adapters.claude import ClaudeAdapter
            from app.adapters.codex import CodexAdapter

            adapters = {"claude": ClaudeAdapter(), "codex": CodexAdapter()}
        missing = _ENGINES - set(adapters)
        if missing:
            raise ValueError(f"缺少引擎适配器：{','.join(sorted(missing))}")
        self._adapters = dict(adapters)
        self._active: Any = None
        self._route_overrides: dict[str, str] = {}
        self._breakers: dict[str, ResearchCircuitBreaker] = {}
        self._last_research_id: str | None = None
        self._resilience_config = resilience_config or load_resilience_config()
        self._probe_sleep = probe_sleep
        self._backoff_sleep = backoff_sleep
        self._recovery_tasks: set[asyncio.Task[None]] = set()
        self._backoff_tasks: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._backoff_counts: dict[tuple[str, str], int] = {}
        self._quota_gates: dict[str, asyncio.Event] = {}
        self._manual_research_alternates: set[str] = set()
        self._manual_agent_alternates: dict[tuple[str, str], int] = {}
        self._agent_runs: dict[tuple[str, str], int] = {}
        self._source_adapter = SourceToolAdapter(source_tools)

    @property
    def future_engine(self) -> str | None:
        """限流事件要求后续新任务让路时的适配层覆盖。"""

        return self.route_override

    @property
    def route_override(self) -> str | None:
        """返回最近一次 research 的适配层路由覆盖，仅用于观测。"""

        if self._last_research_id is None:
            return None
        return self._route_overrides.get(self._last_research_id)

    def route_override_for(self, research_id: str) -> str | None:
        """按 research 查询覆盖，避免跨 research 共享断路状态。"""

        return self._route_overrides.get(research_id)

    def _breaker(self, research_id: str) -> ResearchCircuitBreaker:
        breaker = self._breakers.get(research_id)
        if breaker is None:
            breaker = ResearchCircuitBreaker(research_id, self._resilience_config)
            self._breakers[research_id] = breaker
        return breaker

    @staticmethod
    def _alternate(engine: str) -> str:
        return next(item for item in _ENGINES if item != engine)

    def request_alternate(
        self,
        research_id: str,
        *,
        agent_id: str | None = None,
        after_attempt: int = 0,
    ) -> None:
        """记录人工让路意图；具体目标仍只由适配层根据当前默认路由推导。"""

        if agent_id is None:
            self._manual_research_alternates.add(research_id)
            return
        self._manual_agent_alternates[(research_id, agent_id)] = max(
            0, int(after_attempt)
        )

    def release_route_gate(self, research_id: str) -> None:
        gate = self._quota_gates.pop(research_id, None)
        if gate is not None:
            gate.set()

    async def _await_route_gates(self, research_id: str, engine: str) -> bool:
        backoff_released = False
        quota_gate = self._quota_gates.get(research_id)
        if quota_gate is not None:
            await quota_gate.wait()
        key = (research_id, engine)
        backoff = self._backoff_tasks.get(key)
        if backoff is not None:
            await backoff
            backoff_released = True
            if self._backoff_tasks.get(key) is backoff:
                self._backoff_tasks.pop(key, None)
        return backoff_released

    @staticmethod
    def _reset_at(event: Any) -> datetime | None:
        raw = getattr(event, "raw", None)
        raw = raw if isinstance(raw, Mapping) else {}
        info = raw.get("rate_limit_info") or raw.get("rateLimitInfo") or raw
        value = next((
            info.get(name)
            for name in (
                "resets_at", "resetsAt", "reset_at", "resetAt",
                "five_hour_resets_at", "fiveHourResetsAt",
            )
            if info.get(name) is not None
        ), None)
        if isinstance(value, datetime):
            return value
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, tz=timezone.utc)
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
        return None

    def _is_rate_limit(self, event: Any) -> bool:
        if self._event_cause(event) == "rate_limit":
            return True
        raw = getattr(event, "raw", None)
        return isinstance(raw, Mapping) and (
            raw.get("api_error_status") == 429
            or "rate_limit_info" in raw
            or "rateLimitInfo" in raw
        )

    def _start_backoff(self, research_id: str, engine: str, event: Any) -> None:
        key = (research_id, engine)
        current = self._backoff_tasks.get(key)
        if current is not None and not current.done():
            return
        count = self._backoff_counts.get(key, 0)
        reset_at = self._reset_at(event)
        if reset_at is None:
            delay = float(self._resilience_config.backoff_seconds(count))
        else:
            delay = max(
                0.0,
                (reset_at - datetime.now(timezone.utc)).total_seconds(),
            )
        self._backoff_counts[key] = count + 1
        self._backoff_tasks[key] = asyncio.create_task(self._backoff_sleep(delay))

    @staticmethod
    def _event_cause(event: Any) -> str | None:
        cause = getattr(event, "cause", None)
        value = getattr(cause, "value", cause)
        return value.casefold() if isinstance(value, str) else None

    async def _emit(self, callback: Any, event: NormalizedEvent) -> None:
        if callback is None:
            return
        callback_result = callback(event)
        if inspect.isawaitable(callback_result):
            await callback_result

    async def _probe(self, engine: str) -> bool:
        probe = getattr(self._adapters[engine], "probe", None)
        if probe is None:
            return False
        try:
            result = probe()
            if inspect.isawaitable(result):
                result = await result
        except Exception:
            return False
        if isinstance(result, bool):
            return result
        for attribute in ("healthy", "ok"):
            value = getattr(result, attribute, None)
            if isinstance(value, bool):
                return value
        return False

    @staticmethod
    def _health_event(transition: CircuitTransition) -> NormalizedEvent:
        is_down = transition.event.value == "ENGINE_DOWN"
        return NormalizedEvent(
            engine=transition.engine,
            thread_id=transition.research_id,
            turn_id=None,
            item_kind=ItemKind.ERROR if is_down else ItemKind.THINKING,
            text=(
                f"{transition.event.value}: {transition.engine}"
                + (f" -> {transition.target}" if transition.target else "")
            ),
            is_error=is_down,
            raw={
                "research_id": transition.research_id,
                "engine": transition.engine,
                "target": transition.target,
                "event": transition.event.value,
            },
            route_state="FAILOVER" if is_down else "CONTINUE",
            failover_target=transition.target if is_down else None,
            scope="new_tasks" if is_down else None,
            outcome=transition.event.value,
            cause="transport",
        )

    def _start_recovery_probe(
        self,
        *,
        research_id: str,
        engine: str,
        on_event: Any,
    ) -> None:
        async def recover() -> None:
            breaker = self._breaker(research_id)
            while breaker.is_down(engine):
                await self._probe_sleep(
                    self._resilience_config.engine_probe_interval_seconds
                )
                healthy = await self._probe(engine)
                transitions = breaker.record_probe(engine, healthy=healthy)
                reset = next(
                    (
                        transition
                        for transition in transitions
                        if transition.event.value == "RESET"
                    ),
                    None,
                )
                if reset is not None:
                    current = self._route_overrides.get(research_id)
                    if current == reset.target:
                        self._route_overrides.pop(research_id, None)
                for transition in transitions:
                    try:
                        await self._emit(on_event, self._health_event(transition))
                    except Exception:
                        # 展示/日志投影失败不得反向破坏已完成的健康复位。
                        continue

        task = asyncio.create_task(recover())
        self._recovery_tasks.add(task)
        task.add_done_callback(self._recovery_tasks.discard)

    async def _trip_if_needed(
        self,
        *,
        task: Any,
        engine: str,
        on_event: Any,
    ) -> None:
        planning = task.agent_kind in _PLANNING_KINDS
        breaker = self._breaker(task.research_id)
        transition = breaker.record_transport_failure(
            engine, planning=planning
        )
        if transition is None:
            return
        target = next(item for item in _ENGINES if item != engine)
        if not await self._probe(target):
            breaker.reject_failover(engine)
            return
        activated = breaker.activate_failover(engine, target)
        self._route_overrides[task.research_id] = target
        self._start_recovery_probe(
            research_id=task.research_id,
            engine=engine,
            on_event=on_event,
        )
        await self._emit(on_event, self._health_event(activated))

    async def run(self, task: Any, ctx: Any, on_event: Any = None) -> Any:
        self._last_research_id = task.research_id
        selection = pick_engine(task.agent_kind, task.user_override)
        run_key = (task.research_id, task.agent_id)
        run_number = self._agent_runs.get(run_key, 0) + 1
        self._agent_runs[run_key] = run_number
        after_attempt = self._manual_agent_alternates.get(run_key)
        manual_alternate = (
            task.research_id in self._manual_research_alternates
            or (after_attempt is not None and run_number > after_attempt)
        )
        preferred = (
            self._alternate(selection.engine) if manual_alternate else selection.engine
        )
        selected_engine = self._route_overrides.get(task.research_id, preferred)
        backoff_released = await self._await_route_gates(
            task.research_id, selected_engine
        )
        adapter = self._adapters[selected_engine]
        saw_transport = False

        if backoff_released:
            await self._emit(on_event, NormalizedEvent(
                engine=selected_engine,
                thread_id=task.research_id,
                turn_id=None,
                item_kind=ItemKind.THINKING,
                text="退避结束",
                is_error=False,
                raw={"event": "BACKOFF_RELEASED"},
                route_state="CONTINUE",
                outcome="BACKOFF_RELEASED",
                cause="rate_limit",
            ))

        async def routed_event(event: Any) -> None:
            nonlocal saw_transport
            if self._event_cause(event) == "transport":
                saw_transport = True
            route_state = getattr(event, "route_state", None)
            state_value = getattr(route_state, "value", route_state)
            if state_value == "BACKOFF" and self._is_rate_limit(event):
                self._start_backoff(task.research_id, selected_engine, event)
            reason = str(getattr(event, "reason", None) or getattr(event, "text", ""))
            if state_value == "WARN" and "继续跑会计费" in reason:
                self._quota_gates.setdefault(task.research_id, asyncio.Event())
            target = getattr(event, "failover_target", None)
            scope = getattr(event, "scope", None)
            if target is not None and scope == "new_tasks":
                if target not in _ENGINES:
                    raise ValueError(f"未知限流让路目标：{target}")
                self._route_overrides[task.research_id] = target
            await self._emit(on_event, event)

        self._active = adapter
        prepare_source_events(task)
        try:
            try:
                parameters = inspect.signature(adapter.run).parameters
                kwargs = {"on_event": routed_event}
                if "source_adapter" in parameters:
                    kwargs["source_adapter"] = self._source_adapter
                result = await adapter.run(task, ctx, **kwargs)
            finally:
                await replay_source_events(task, routed_event)
            breaker = self._breaker(task.research_id)
            if saw_transport:
                await self._trip_if_needed(
                    task=task,
                    engine=selected_engine,
                    on_event=on_event,
                )
            elif bool(getattr(result, "succeeded", False)):
                breaker.record_success(selected_engine)
            else:
                breaker.record_non_transport(selected_engine)
            return result
        finally:
            self._active = None

    async def run_planning_segment(
        self,
        request: PlanningSegmentRequest,
        *,
        on_text: Any = None,
    ) -> PlanningSegmentResult:
        """规划短流固定走 Claude；执行期断路覆盖对此入口无效。"""

        generator = getattr(self._adapters["claude"], "generate_plan_segment", None)
        if generator is None:
            if request.output_path is None:
                return PlanningSegmentResult(
                    text="",
                    completed=False,
                    error="规划短流请求缺少落盘路径",
                )
            from app.adapters import validation
            from app.adapters.capability import Capability, FileSystemScope
            from app.adapters.contracts import EngineTask

            task = EngineTask(
                body=request.prompt,
                output_path=request.output_path,
                output_format="json",
                research_id=request.research_id,
                goal_id="plan-segments",
                agent_id=f"plan-{request.segment_name}",
                agent_kind="planning",
                validators=["file_exists"],
                capability=Capability(
                    profile="custom",
                    tools=("fs.write",),
                    fs=FileSystemScope(write=("plan-segments/**",)),
                ),
            )
            ctx = validation.Ctx(
                output_path=request.output_path,
                output_format="json",
                research_id=request.research_id,
                goal_id="plan-segments",
                agent_id=f"plan-{request.segment_name}",
                read_text=lambda: request.output_path.read_text(encoding="utf-8"),
                read_json=lambda: json.loads(
                    request.output_path.read_text(encoding="utf-8")
                ),
                store=None,
                source_domains=frozenset(),
            )
            result = await self._adapters["claude"].run(task, ctx)
            text = (
                request.output_path.read_text(encoding="utf-8")
                if request.output_path.is_file()
                else ""
            )
            if on_text is not None and text:
                callback_result = on_text(text)
                if inspect.isawaitable(callback_result):
                    await callback_result
            transport = any(
                self._event_cause(event) == "transport"
                for event in getattr(result, "events", [])
            )
            error = (
                getattr(result, "engine_error", None)
                or getattr(result, "conclusion_error", None)
            )
            if not error:
                details: list[str] = []
                report = getattr(result, "validation", None)
                for item in getattr(report, "results", []):
                    verdict = getattr(getattr(item, "verdict", None), "value", None)
                    if verdict == "pass":
                        continue
                    message = str(getattr(item, "message", "")).strip()
                    offenders = [str(value) for value in getattr(item, "offenders", [])]
                    if offenders:
                        message = f"{message}；offenders={offenders}"
                    if message:
                        details.append(message)
                error = "；".join(details) or None
            return PlanningSegmentResult(
                text=text,
                completed=bool(getattr(result, "succeeded", False)),
                transport_interrupted=transport,
                error=str(error) if error else None,
            )
        result = generator(request, on_text=on_text)
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, PlanningSegmentResult):
            raise TypeError("规划短流适配器必须返回 PlanningSegmentResult")
        return result

    async def call_source(
        self,
        tool_name: str,
        query: str,
        window: str,
        *,
        research_id: str,
        goal_id: str,
        agent_id: str,
        capability: Any,
        on_event: Any = None,
        **kwargs: Any,
    ) -> Any:
        """在适配层解析 source.* 工具并转发源事件，不向编排层泄漏实现。"""

        return await self._source_adapter.call(
            tool_name,
            query,
            window,
            research_id=research_id,
            goal_id=goal_id,
            agent_id=agent_id,
            capability=capability,
            on_event=on_event,
            **kwargs,
        )

    async def interrupt(self) -> None:
        if self._active is None:
            raise RuntimeError("当前没有运行中的引擎任务")
        await self._active.interrupt()
