"""双引擎限流决策；只分类与发事件，不执行真实退避或重试。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping

from app.adapters.events import ItemKind, NormalizedEvent
from app.adapters.logging import (
    DEFAULT_LOG_ROOT,
    append_engine_error,
    append_routing_event,
)


class RouteState(str, Enum):
    CONTINUE = "CONTINUE"
    WARN = "WARN"
    BACKOFF = "BACKOFF"
    FAILOVER = "FAILOVER"


@dataclass(frozen=True)
class RouteDecision:
    state: RouteState
    reason: str
    raw: Any
    failover_target: str | None = None
    no_fallback_left: bool = False
    suspend_new_tasks: bool = False
    scope: str | None = None
    allow_current_task_to_finish: bool = False


EventCallback = Callable[[NormalizedEvent], Any]
RateLimitedCallback = Callable[[str, RouteDecision], Any]
Clock = Callable[[], datetime]


_CODEX_LIMIT_PATTERN = re.compile(
    r"\b(?:usage|rate)[\s_-]*limits?\b",
    re.IGNORECASE,
)


def _field(value: Any, *names: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
        return default
    for name in names:
        if hasattr(value, name):
            return getattr(value, name)
    return default


def _failover(raw: Any, reason: str, target: str | None) -> RouteDecision:
    # 架构不对称 #4：一旦让路目标是 Codex，它再撞限流就没有下一引擎可退。
    no_fallback_left = bool(target and target.casefold() == "codex")
    return RouteDecision(
        RouteState.FAILOVER,
        reason,
        raw,
        failover_target=target,
        no_fallback_left=no_fallback_left,
        scope="new_tasks",
        allow_current_task_to_finish=True,
    )


def _message_ids(message: Any) -> tuple[str | None, str | None]:
    return (
        _field(message, "session_id"),
        _field(message, "uuid", "message_id"),
    )


def _decision_event(
    decision: RouteDecision,
    *,
    engine: str,
    thread_id: str | None = None,
    turn_id: str | None = None,
) -> NormalizedEvent:
    raw_thread_id, raw_turn_id = _message_ids(decision.raw)
    is_error = decision.state in {RouteState.BACKOFF, RouteState.FAILOVER}
    return NormalizedEvent(
        engine=engine,
        thread_id=raw_thread_id or thread_id,
        turn_id=raw_turn_id or turn_id,
        item_kind=ItemKind.ERROR if is_error else ItemKind.THINKING,
        text=decision.reason,
        is_error=is_error,
        raw=decision.raw,
        route_state=decision.state.value,
        suspend_new_tasks=decision.suspend_new_tasks,
        failover_target=decision.failover_target,
        no_fallback_left=decision.no_fallback_left,
        scope=decision.scope,
        allow_current_task_to_finish=decision.allow_current_task_to_finish,
    )


def _is_rate_limited(decision: RouteDecision) -> bool:
    info = _field(decision.raw, "rate_limit_info", "rateLimitInfo")
    if info is not None:
        return _field(info, "status") == "rejected"
    api_limited = _field(
        decision.raw, "api_error_status", "apiErrorStatus"
    ) == 429
    raw_text = json.dumps(decision.raw, ensure_ascii=False, default=str)
    return api_limited or classify_codex_error(raw_text)


def _publish(
    decision: RouteDecision,
    *,
    engine: str,
    on_event: EventCallback | None,
    on_rate_limited: RateLimitedCallback | None,
    log_root: Path,
    log_clock: Clock | None,
    thread_id: str | None = None,
    turn_id: str | None = None,
) -> RouteDecision:
    if decision.state is RouteState.CONTINUE:
        return decision
    event = _decision_event(
        decision,
        engine=engine,
        thread_id=thread_id,
        turn_id=turn_id,
    )
    logging_args = {"log_root": log_root}
    if log_clock is not None:
        logging_args["clock"] = log_clock
    append_routing_event(event, **logging_args)
    if event.is_error:
        append_engine_error(event, **logging_args)
    if on_event is not None:
        on_event(event)
    # 最小公共接口只接收 rejected / 429 的事后通知；allowed_warning 与
    # utilization 留在 Claude 独享扩展，不能为了对称塞进公共接口。
    if _is_rate_limited(decision) and on_rate_limited is not None:
        on_rate_limited(engine, decision)
    return decision


def _route_rate_limit_event(message: Any, info: Any) -> RouteDecision:
    status = _field(info, "status")
    rate_limit_type = _field(
        info, "rate_limit_type", "rateLimitType", default="未知窗口"
    )
    if status == "rejected":
        overage_status = _field(info, "overage_status", "overageStatus")
        # 判定陷阱二：rejected 不等于必须切引擎；先查 overage_status。
        # 可用时必须让用户确认是否接受计费，不能闷头切换或闷头烧钱。
        if overage_status in {"allowed", "allowed_warning"}:
            overage_warning = (
                "；overage 也接近上限"
                if overage_status == "allowed_warning"
                else ""
            )
            return RouteDecision(
                RouteState.WARN,
                f"{rate_limit_type} 限流；overage 可用{overage_warning}，"
                "继续跑会计费，等待用户确认",
                message,
                suspend_new_tasks=True,
            )
        disabled_reason = _field(
            info,
            "overage_disabled_reason",
            "overageDisabledReason",
            default="未提供原因",
        ) or "未提供原因"
        return _failover(
            message,
            f"{rate_limit_type} 限流，overage 不可用：{disabled_reason}",
            "codex",
        )
    if status == "allowed_warning":
        utilization = _field(info, "utilization")
        percentage = (
            f"{utilization:.0%}" if isinstance(utilization, (int, float)) else "未知"
        )
        return RouteDecision(
            RouteState.WARN,
            f"{rate_limit_type} 已用 {percentage}，接近上限；后续新任务让路",
            message,
            failover_target="codex",
            no_fallback_left=True,
            scope="new_tasks",
            allow_current_task_to_finish=True,
        )
    return RouteDecision(RouteState.CONTINUE, "额度正常", message)


def _route_result_message(message: Any) -> RouteDecision:
    api_error_status = _field(message, "api_error_status", "apiErrorStatus")
    # 判定陷阱一：Claude 限流时 subtype 仍可能是 "success"；
    # 必须优先看 api_error_status，绝不能用 subtype 判成功。
    if api_error_status in {429, 500, 529}:
        if api_error_status == 429:
            reason = "API 429 限流，等待编排层退避重试"
        else:
            reason = f"API {api_error_status} 服务端错误，等待编排层退避重试"
        return RouteDecision(
            RouteState.BACKOFF,
            reason,
            message,
            suspend_new_tasks=True,
        )
    if bool(_field(message, "is_error", "isError", default=False)):
        subtype = _field(message, "subtype", default="未知错误")
        return _failover(message, f"非限流错误：{subtype}", "codex")
    return RouteDecision(RouteState.CONTINUE, "消息正常", message)


def _route_claude_message(message: Any) -> RouteDecision:
    info = _field(message, "rate_limit_info", "rateLimitInfo")
    if info is not None:
        return _route_rate_limit_event(message, info)
    return _route_result_message(message)


def _route_codex_message(message: Any) -> RouteDecision:
    raw_text = json.dumps(message, ensure_ascii=False, default=str)
    if not classify_codex_error(raw_text):
        return RouteDecision(RouteState.CONTINUE, "消息正常", message)
    return RouteDecision(
        RouteState.BACKOFF,
        "Codex 撞墙式限流；无下一引擎可退，等待额度重置",
        message,
        no_fallback_left=True,
        suspend_new_tasks=True,
    )


def route(
    msg: Any,
    *,
    engine: str = "Claude",
    on_event: EventCallback | None = None,
    on_rate_limited: RateLimitedCallback | None = None,
    log_root: Path = DEFAULT_LOG_ROOT,
    log_clock: Clock | None = None,
    thread_id: str | None = None,
    turn_id: str | None = None,
) -> RouteDecision:
    """把双引擎消息归入四态，并同步发布非正常事件。"""
    routers = {
        "claude": _route_claude_message,
        "codex": _route_codex_message,
    }
    engine_key = engine.strip().casefold()
    try:
        decision = routers[engine_key](msg)
    except KeyError as exc:
        raise ValueError(f"未知限流消息来源：{engine}") from exc
    return _publish(
        decision,
        engine=engine,
        on_event=on_event,
        on_rate_limited=on_rate_limited,
        log_root=log_root,
        log_clock=log_clock,
        thread_id=thread_id,
        turn_id=turn_id,
    )


def classify_codex_error(text: str) -> bool:
    """宽容识别 Codex 的撞墙文案；真实 exec 分支样本积累后再收紧。"""
    # 判定陷阱三：Codex 越权或失败时退出码仍可能为 0；不能依赖退出码，
    # 要靠产物校验、结构化结论与这里的错误文本匹配。
    return bool(_CODEX_LIMIT_PATTERN.search(str(text or "")))


class R8State(str, Enum):
    IDLE = "IDLE"
    WAITING = "WAITING"
    RESOLVED = "RESOLVED"


class R8Confirm:
    """额度耗尽确认的纯状态机；UI、真实计时器与任务恢复均由 M2 接入。"""

    TIMEOUT = timedelta(minutes=15)

    def __init__(
        self,
        *,
        clock: Clock,
        on_event: EventCallback | None = None,
        log_root: Path = DEFAULT_LOG_ROOT,
        log_clock: Clock | None = None,
    ) -> None:
        self._clock = clock
        self._on_event = on_event
        self._log_root = log_root
        self._log_clock = log_clock or clock
        self.state = R8State.IDLE
        self.started_at: datetime | None = None
        self.deadline: datetime | None = None
        self._pending: RouteDecision | None = None
        self.outcome: RouteDecision | None = None

    @property
    def suspend_new_tasks(self) -> bool:
        return self.state is R8State.WAITING

    def wait(self, decision: RouteDecision) -> None:
        if self.state is R8State.WAITING:
            raise RuntimeError("R8 已在等待用户确认")
        if decision.state is not RouteState.WARN or "继续跑会计费" not in decision.reason:
            raise ValueError("R8 只能接收 overage 可用且等待计费确认的 WARN 决策")
        self.started_at = self._clock()
        self.deadline = self.started_at + self.TIMEOUT
        self._pending = decision
        self.outcome = None
        self.state = R8State.WAITING

    def _resolve(self, decision: RouteDecision) -> RouteDecision:
        self._pending = None
        self.outcome = decision
        self.state = R8State.RESOLVED
        return _publish(
            decision,
            engine="Claude",
            on_event=self._on_event,
            # 初次 rejected 已由 route() 发过公共回调；R8 只发布解决事件，
            # 避免 M2 把同一次限流误当成第二次限流并重建确认流程。
            on_rate_limited=None,
            log_root=self._log_root,
            log_clock=self._log_clock,
        )

    def _timeout_decision(self) -> RouteDecision:
        assert self._pending is not None
        return _failover(
            self._pending.raw,
            "15 分钟内未收到额度计费确认，自动切换到 codex",
            "codex",
        )

    def check_timeout(self) -> RouteDecision | None:
        if self.state is not R8State.WAITING:
            return None
        assert self.deadline is not None
        if self._clock() < self.deadline:
            return None
        return self._resolve(self._timeout_decision())

    def confirm(self, *, continue_with_overage: bool) -> RouteDecision:
        if self.state is not R8State.WAITING or self._pending is None:
            raise RuntimeError("R8 当前没有待确认决策")
        timed_out = self.check_timeout()
        if timed_out is not None:
            return timed_out
        pending = self._pending
        if continue_with_overage:
            decision = RouteDecision(
                RouteState.CONTINUE,
                "用户确认计费，允许使用 overage 继续",
                pending.raw,
            )
        else:
            decision = _failover(
                pending.raw,
                "用户不接受 overage 计费，切换到 codex",
                "codex",
            )
        return self._resolve(decision)
