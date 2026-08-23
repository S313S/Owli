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


class RouteCause(str, Enum):
    NORMAL = "normal"
    RATE_LIMIT = "rate_limit"
    TRANSPORT = "transport"
    SERVICE = "service"
    ENGINE_ERROR = "engine_error"


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
    cause: RouteCause = RouteCause.NORMAL


EventCallback = Callable[[NormalizedEvent], Any]
RateLimitedCallback = Callable[[str, RouteDecision], Any]
Clock = Callable[[], datetime]


_CODEX_LIMIT_PATTERN = re.compile(
    r"\b(?:usage|rate)[\s_-]*limits?\b",
    re.IGNORECASE,
)

# 传输层抖动文案（本机代理掐流/断连的典型指纹），命中即退避而非让路。
_TRANSPORT_JITTER_PATTERN = re.compile(
    r"tls|handshake|stream disconnected|econn(?:reset|refused)|socket"
    r"|connection (?:reset|refused|closed|aborted|error)|timed? ?out"
    r"|proxy|\beof\b|network|dns|unreachable|broken pipe",
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


def _failover(
    raw: Any,
    reason: str,
    target: str | None,
    *,
    cause: RouteCause = RouteCause.ENGINE_ERROR,
) -> RouteDecision:
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
        cause=cause,
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
        cause=decision.cause.value,
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
    publish_continue: bool = False,
) -> RouteDecision:
    if decision.state is RouteState.CONTINUE and not publish_continue:
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


def publish_route_decision(
    decision: RouteDecision,
    *,
    engine: str,
    on_event: EventCallback | None = None,
    on_rate_limited: RateLimitedCallback | None = None,
    log_root: Path = DEFAULT_LOG_ROOT,
    log_clock: Clock | None = None,
    thread_id: str | None = None,
    turn_id: str | None = None,
    publish_continue: bool = False,
) -> RouteDecision:
    """把外部适配器的四态决策接入统一事件落盘与附加通知。"""

    return _publish(
        decision,
        engine=engine,
        on_event=on_event,
        on_rate_limited=on_rate_limited,
        log_root=log_root,
        log_clock=log_clock,
        thread_id=thread_id,
        turn_id=turn_id,
        publish_continue=publish_continue,
    )


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
                cause=RouteCause.RATE_LIMIT,
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
            cause=RouteCause.RATE_LIMIT,
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
            cause=RouteCause.RATE_LIMIT,
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
            cause=(
                RouteCause.RATE_LIMIT
                if api_error_status == 429
                else RouteCause.SERVICE
            ),
        )
    if bool(_field(message, "is_error", "isError", default=False)):
        subtype = _field(message, "subtype", default="未知错误")
        # 判定陷阱四：本机代理掐流产生的传输层错误没有 api_error_status，
        # 若按「非限流错误」让路会把整条调研的后续新任务静默钉死在 Codex
        # 上（含规划重试）。网络抖动 ≠ 引擎故障：原引擎退避重跑，不让路。
        probe = " ".join(
            str(value)
            for value in (subtype, _field(message, "result", default=""))
        )
        if classify_transport_error(probe):
            return RouteDecision(
                RouteState.BACKOFF,
                f"疑似网络抖动（代理/传输层）：{subtype}，原引擎退避重试",
                message,
                suspend_new_tasks=True,
                cause=RouteCause.TRANSPORT,
            )
        return _failover(message, f"非限流错误：{subtype}", "codex")
    return RouteDecision(RouteState.CONTINUE, "消息正常", message)


def _route_claude_message(message: Any) -> RouteDecision:
    info = _field(message, "rate_limit_info", "rateLimitInfo")
    if info is not None:
        return _route_rate_limit_event(message, info)
    return _route_result_message(message)


def _codex_error_text(message: Any) -> str:
    """只提取错误载荷文本参与撞墙匹配。

    普通输出（命令回显、读到的文件内容）不得参与：真实误报样本——agent
    `sed` 读 architecture.md，正文里的文件名 `ratelimit.py` 命中宽容正则，
    整条链路被误判 BACKOFF（2026-08-19 M2-e 验收实录）。
    """
    if not isinstance(message, Mapping):
        return str(message or "")
    texts: list[str] = []
    if str(message.get("type", "")) == "error":
        texts.append(str(message.get("message", "")))
    item = message.get("item")
    if isinstance(item, Mapping) and str(item.get("type", "")) == "error":
        texts.append(str(item.get("message", "")))
    error = message.get("error")
    if isinstance(error, Mapping):
        texts.append(str(error.get("message", "")))
    elif isinstance(error, str):
        texts.append(error)
    if bool(_field(message, "is_error", "isError", default=False)):
        texts.append(json.dumps(message, ensure_ascii=False, default=str))
    return "\n".join(text for text in texts if text)


def _route_codex_message(message: Any) -> RouteDecision:
    if not classify_codex_error(_codex_error_text(message)):
        return RouteDecision(RouteState.CONTINUE, "消息正常", message)
    return RouteDecision(
        RouteState.BACKOFF,
        "Codex 撞墙式限流；无下一引擎可退，等待额度重置",
        message,
        no_fallback_left=True,
        suspend_new_tasks=True,
        cause=RouteCause.RATE_LIMIT,
    )


def classify_transport_error(text: str) -> bool:
    """复用 M3-a 指纹判断传输故障；断路器不得复制或改写该正则。"""

    return bool(_TRANSPORT_JITTER_PATTERN.search(str(text or "")))


# --- 限流判定：结构化字段优先，措辞正则只做兜底 -------------------------------
# 红线（M3-h 硬约束 1）：不得用措辞正则做闭集判定。D-002/缺陷 C 的教训是
# `限流` 子串命中了「非限流错误」，把 schema 校验失败误判成额度耗尽。
# 因此分两级：
#   1) 结构化级：读引擎载荷里的 api_error_status / 错误类型 / rate_limit_info；
#      拿到状态码就以它为准（429 = 限流，其它状态码 = 明确不是限流）。
#   2) 措辞级：只有结构化完全取不到信号时才用，且必须排除否定语境。

RATE_LIMIT_HTTP_STATUS = 429

_RATE_LIMIT_STATUS_FIELDS = (
    "api_error_status", "apiErrorStatus",
    "status_code", "statusCode",
    "http_status", "httpStatus",
)
_RATE_LIMIT_TYPE_FIELDS = (
    "error_type", "errorType", "type", "code", "error_code", "errorCode",
)
_RATE_LIMIT_TYPE_VALUES = frozenset({
    "rate_limit_error", "rate_limit_exceeded", "rate_limited",
    "quota_exceeded", "quota_exhausted", "insufficient_quota",
    "usage_limit_reached", "too_many_requests",
})

_QUOTA_WORDING_PATTERN = re.compile(
    r"quota|rate[\s_-]?limits?|usage[\s_-]?limits?|too many requests"
    r"|\b429\b|额度|配额|限流",
    re.IGNORECASE,
)

# 否定语境：中文只看紧邻的连续汉字（不跨标点），英文只看同一子句内的否定词，
# 另外看命中片段后面紧跟的「未超 / not exceeded」这类后置否定。
_CJK_NEGATIONS = ("非", "未", "无", "没", "免", "不")
_CJK_TAIL_PATTERN = re.compile(r"[一-鿿]{0,4}$")
_EN_NEGATION_PATTERN = re.compile(
    r"\b(?:no|not|non|never|without|neither|nor|isn't|aren't|wasn't|weren't"
    r"|doesn't|don't|didn't|hasn't|haven't|won't|can't|cannot)\b"
    # 否定词与命中片段之间允许若干冠词/限定词：not a rate limit error
    r"(?:[\s\-]+(?:a|an|the|any|real|actual|true|hard|engine|api)\b)*[\s\-]*$",
    re.IGNORECASE,
)
_TRAILING_NEGATION_PATTERN = re.compile(
    r"^[\s\-—:：,，]*(?:未(?:超|达|到|触发|命中|生效)|没(?:有)?(?:超|达|触发)"
    r"|不(?:会|是|超|存在)"
    r"|not\s+(?:exceeded|hit|reached|triggered|limited|rejected))",
    re.IGNORECASE,
)
_CLAUSE_BOUNDARY_PATTERN = re.compile(r"[。！？；;.\n\r]")


class RateLimitVerdict(str, Enum):
    """限流三态：命中 / 明确不是 / 无结构化信号。"""

    LIMITED = "limited"
    NOT_LIMITED = "not_limited"
    UNKNOWN = "unknown"


def _is_negated(text: str, start: int, end: int) -> bool:
    """判断命中片段是否落在否定语境里（「非限流」「limit 未超」都算）。"""

    prefix = text[max(0, start - 32):start]
    boundary = None
    for match in _CLAUSE_BOUNDARY_PATTERN.finditer(prefix):
        boundary = match.end()
    if boundary is not None:
        prefix = prefix[boundary:]
    cjk_tail = _CJK_TAIL_PATTERN.search(prefix)
    if any(negation in (cjk_tail.group() if cjk_tail else "")
           for negation in _CJK_NEGATIONS):
        return True
    if _EN_NEGATION_PATTERN.search(prefix):
        return True
    return bool(_TRAILING_NEGATION_PATTERN.match(text[end:end + 24]))


def search_without_negation(
    pattern: re.Pattern[str], text: Any,
) -> re.Match[str] | None:
    """措辞兜底专用：返回第一个不处于否定语境的命中，全被否定则返回 None。"""

    probe = str(text or "")
    for match in pattern.finditer(probe):
        if not _is_negated(probe, match.start(), match.end()):
            return match
    return None


def _rate_limit_mapping(value: Any) -> Mapping[str, Any] | None:
    """把引擎错误载荷（dict / JSON 字符串 / 结果对象）摊成一个 mapping。"""

    if isinstance(value, Mapping):
        return value
    if isinstance(value, str):
        candidate = value.strip()
        if not candidate.startswith("{"):
            return None
        try:
            parsed = json.loads(candidate)
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, Mapping) else None
    if value is None or isinstance(value, (bool, int, float)):
        return None
    harvested: dict[str, Any] = {}
    for name in (
        *_RATE_LIMIT_STATUS_FIELDS,
        *_RATE_LIMIT_TYPE_FIELDS,
        "rate_limit_info", "rateLimitInfo", "engine_error", "conclusion_error",
    ):
        got = getattr(value, name, None)
        if got is not None:
            harvested[name] = got
    return harvested or None


def _flatten_rate_limit_payloads(value: Any, depth: int = 0) -> list[Mapping[str, Any]]:
    """递归摊平嵌套载荷（error / errors / 序列化后的子 JSON），深度设上限。"""

    if depth > 3:
        return []
    mapping = _rate_limit_mapping(value)
    if mapping is None:
        return []
    found: list[Mapping[str, Any]] = [mapping]
    for nested in mapping.values():
        if isinstance(nested, (Mapping, str)):
            found.extend(_flatten_rate_limit_payloads(nested, depth + 1))
        elif isinstance(nested, (list, tuple)):
            for item in nested:
                found.extend(_flatten_rate_limit_payloads(item, depth + 1))
    return found


def _http_status(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    return int(text) if text.isdigit() else None


def structured_rate_limit_verdict(*payloads: Any) -> RateLimitVerdict:
    """只读结构化字段的限流判定；一个状态信号都取不到才返回 UNKNOWN。"""

    statuses: list[int] = []
    for payload in payloads:
        for mapping in _flatten_rate_limit_payloads(payload):
            info = mapping.get("rate_limit_info", mapping.get("rateLimitInfo"))
            if (
                isinstance(info, Mapping)
                and str(info.get("status", "")).casefold() == "rejected"
            ):
                return RateLimitVerdict.LIMITED
            for name in _RATE_LIMIT_TYPE_FIELDS:
                value = mapping.get(name)
                if (
                    isinstance(value, str)
                    and value.strip().casefold() in _RATE_LIMIT_TYPE_VALUES
                ):
                    return RateLimitVerdict.LIMITED
            for name in _RATE_LIMIT_STATUS_FIELDS:
                if name not in mapping:
                    continue
                status = _http_status(mapping[name])
                if status is not None:
                    statuses.append(status)
    if RATE_LIMIT_HTTP_STATUS in statuses:
        return RateLimitVerdict.LIMITED
    if statuses:
        # 拿到了明确的非 429 状态码：结构化字段说了不是限流，措辞不得翻案。
        return RateLimitVerdict.NOT_LIMITED
    return RateLimitVerdict.UNKNOWN


def classify_rate_limit(*payloads: Any, text: Any = None) -> RateLimitVerdict:
    """限流判定入口：结构化字段优先，措辞正则只在结构化无信号时兜底。"""

    verdict = structured_rate_limit_verdict(*payloads)
    if verdict is not RateLimitVerdict.UNKNOWN:
        return verdict
    probe = text if text is not None else " ".join(
        payload for payload in payloads if isinstance(payload, str)
    )
    if search_without_negation(_QUOTA_WORDING_PATTERN, probe) is not None:
        return RateLimitVerdict.LIMITED
    return RateLimitVerdict.UNKNOWN


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
            cause=RouteCause.RATE_LIMIT,
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
                cause=RouteCause.RATE_LIMIT,
            )
        else:
            decision = _failover(
                pending.raw,
                "用户不接受 overage 计费，切换到 codex",
                "codex",
                cause=RouteCause.RATE_LIMIT,
            )
        return self._resolve(decision)
