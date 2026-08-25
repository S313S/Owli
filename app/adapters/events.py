"""双引擎统一事件模型；以 Codex 的 thread/turn/item 三级为基准。"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


class ItemKind(str, Enum):
    """编排层可消费的 item 语义；引擎名不参与语义判断。"""

    THINKING = "thinking"
    TOOL_CALL = "tool_call"
    OUTPUT = "output"
    ERROR = "error"
    DONE = "done"


@dataclass(frozen=True)
class NormalizedEvent:
    engine: str
    thread_id: str | None
    turn_id: str | None
    item_kind: ItemKind
    text: str
    is_error: bool
    raw: Any
    route_state: str | None = None
    suspend_new_tasks: bool = False
    failover_target: str | None = None
    no_fallback_left: bool = False
    scope: str | None = None
    allow_current_task_to_finish: bool = False
    outcome: str | None = None
    cause: str | None = None
    usage: dict[str, int | float | None] | None = None


def _token_count(value: Any) -> int:
    """只接收引擎实测的非负整数；缺失或异常字段按 0 归一。"""

    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _cost_usd(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    cost = float(value)
    return cost if cost >= 0 and math.isfinite(cost) else None


def _claude_usage(message: Any) -> dict[str, int | float | None] | None:
    usage = getattr(message, "usage", None)
    if not isinstance(usage, Mapping):
        return None
    return {
        "input_tokens": _token_count(usage.get("input_tokens")),
        "cached_input_tokens": _token_count(usage.get("cache_read_input_tokens")),
        "cache_creation_input_tokens": _token_count(
            usage.get("cache_creation_input_tokens")
        ),
        "cache_write_input_tokens": 0,
        "output_tokens": _token_count(usage.get("output_tokens")),
        "reasoning_output_tokens": 0,
        "cost_usd": _cost_usd(getattr(message, "total_cost_usd", None)),
    }


def _codex_usage(raw: Mapping[str, Any]) -> dict[str, int | float | None] | None:
    usage = raw.get("usage")
    if not isinstance(usage, Mapping):
        return None
    return {
        "input_tokens": _token_count(usage.get("input_tokens")),
        "cached_input_tokens": _token_count(usage.get("cached_input_tokens")),
        "cache_creation_input_tokens": _token_count(
            usage.get("cache_creation_input_tokens")
        ),
        "cache_write_input_tokens": _token_count(
            usage.get("cache_write_input_tokens")
        ),
        "output_tokens": _token_count(usage.get("output_tokens")),
        "reasoning_output_tokens": _token_count(
            usage.get("reasoning_output_tokens")
        ),
        "cost_usd": _cost_usd(usage.get("cost_usd")),
    }


def _message_ids(
    message: Any,
    *,
    thread_id: str | None,
    turn_id: str | None,
) -> tuple[str | None, str | None]:
    data = getattr(message, "data", None)
    data = data if isinstance(data, dict) else {}
    native_thread = getattr(message, "session_id", None) or data.get("session_id")
    native_turn = (
        getattr(message, "message_id", None)
        or getattr(message, "uuid", None)
        or data.get("message_id")
        or data.get("uuid")
    )
    # thread / turn 均由一次 adapter.run 固定注入；Claude 的 session_id、
    # message_id、uuid 只作 raw 内的原始关联，不能把同一执行流拆层。
    return thread_id or native_thread, turn_id or native_turn


def _event(
    message: Any,
    *,
    thread_id: str | None,
    turn_id: str | None,
    item_kind: ItemKind,
    text: str,
    is_error: bool = False,
    outcome: str | None = None,
    cause: str | None = None,
    usage: dict[str, int | float | None] | None = None,
) -> NormalizedEvent:
    return NormalizedEvent(
        engine="Claude",
        thread_id=thread_id,
        turn_id=turn_id,
        item_kind=item_kind,
        text=str(text or ""),
        is_error=is_error,
        raw=message,
        outcome=outcome,
        cause=cause,
        usage=usage,
    )


def _api_retry_cause(message: Any) -> str | None:
    data = getattr(message, "data", None)
    if not isinstance(data, Mapping):
        return None
    nested = data.get("error")
    containers = [data]
    if isinstance(nested, Mapping):
        containers.append(nested)
    for container in containers:
        if container.get("api_error_status", container.get("apiErrorStatus")) == 429:
            return "rate_limit"
        info = container.get("rate_limit_info", container.get("rateLimitInfo"))
        if isinstance(info, Mapping) and info.get("status") == "rejected":
            return "rate_limit"
    return None


def _assistant_events(
    message: Any,
    sdk: Any,
    thread_id: str | None,
    turn_id: str | None,
) -> list[NormalizedEvent]:
    message_error = getattr(message, "error", None)
    if message_error:
        return [_event(
            message,
            thread_id=thread_id,
            turn_id=turn_id,
            item_kind=ItemKind.ERROR,
            text=message_error,
            is_error=True,
        )]

    events: list[NormalizedEvent] = []
    thinking_type = getattr(sdk, "ThinkingBlock", ())
    tool_types = tuple(
        item for item in (
            getattr(sdk, "ToolUseBlock", None),
            getattr(sdk, "ServerToolUseBlock", None),
        ) if isinstance(item, type)
    )
    for block in message.content:
        if thinking_type and isinstance(block, thinking_type):
            kind, text = ItemKind.THINKING, getattr(block, "thinking", "")
        elif tool_types and isinstance(block, tool_types):
            payload = json.dumps(getattr(block, "input", {}), ensure_ascii=False)
            kind, text = ItemKind.TOOL_CALL, f"[{block.name}] {payload}"
        elif isinstance(block, sdk.TextBlock) and block.text.strip():
            kind, text = ItemKind.OUTPUT, block.text
        else:
            continue
        events.append(_event(
            message,
            thread_id=thread_id,
            turn_id=turn_id,
            item_kind=kind,
            text=text,
        ))
    return events or [_event(
        message,
        thread_id=thread_id,
        turn_id=turn_id,
        item_kind=ItemKind.THINKING,
        text="[assistant]",
    )]


def normalize_claude_event(
    message: Any,
    *,
    sdk: Any,
    thread_id: str | None = None,
    turn_id: str | None = None,
) -> list[NormalizedEvent]:
    """把一条 Claude SDK 消息展开为一个或多个 item 级统一事件。"""
    thread_id, turn_id = _message_ids(
        message, thread_id=thread_id, turn_id=turn_id
    )
    if isinstance(message, sdk.AssistantMessage):
        return _assistant_events(message, sdk, thread_id, turn_id)
    if isinstance(message, sdk.UserMessage):
        return [_event(
            message,
            thread_id=thread_id,
            turn_id=turn_id,
            item_kind=ItemKind.TOOL_CALL,
            text="[tool_result] 工具返回",
        )]
    if isinstance(message, sdk.ResultMessage):
        api_error_status = getattr(message, "api_error_status", None)
        # 判定陷阱：Claude 限流时 ResultMessage.subtype 仍是 "success"；
        # 判错误必须看 is_error / api_error_status，绝不能按 subtype 判成功。
        is_error = bool(getattr(message, "is_error", False)) or api_error_status is not None
        return [_event(
            message,
            thread_id=thread_id,
            turn_id=turn_id,
            item_kind=ItemKind.ERROR if is_error else ItemKind.DONE,
            text=getattr(message, "result", "") or "",
            is_error=is_error,
            usage=_claude_usage(message),
        )]
    if isinstance(message, sdk.SystemMessage):
        subtype = str(getattr(message, "subtype", ""))
        if subtype.casefold() == "api_retry":
            return [_event(
                message,
                thread_id=thread_id,
                turn_id=turn_id,
                item_kind=ItemKind.THINKING,
                text="[session] api_retry",
                outcome="API_RETRY",
                cause=_api_retry_cause(message),
            )]
        return [_event(
            message,
            thread_id=thread_id,
            turn_id=turn_id,
            item_kind=ItemKind.THINKING,
            text=f"[session] {subtype}",
        )]
    return [_event(
        message,
        thread_id=thread_id,
        turn_id=turn_id,
        item_kind=ItemKind.THINKING,
        text=f"[{type(message).__name__}]",
    )]


def _codex_text(body: dict[str, Any]) -> str:
    for key in ("message", "text", "delta", "command", "aggregated_output"):
        value = body.get(key)
        if isinstance(value, str) and value:
            return value
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"))


def normalize_codex_event(
    raw: Any,
    *,
    thread_id: str | None = None,
    turn_id: str | None = None,
) -> list[NormalizedEvent]:
    """宽容归一 Codex JSONL；未知 kind 保留原文并归为 thinking。"""
    if not isinstance(raw, dict):
        return [NormalizedEvent(
            "Codex", thread_id, turn_id, ItemKind.THINKING, str(raw), False, raw
        )]

    event_type = str(raw.get("type", ""))
    body = raw.get("item") or raw.get("msg") or raw
    body = body if isinstance(body, dict) else raw
    native_kind = str(body.get("type") or event_type)
    text = _codex_text(body)
    current_thread = raw.get("thread_id") or thread_id
    current_turn = raw.get("turn_id") or turn_id
    lowered = native_kind.lower()

    if event_type == "turn.failed" or "error" in lowered:
        kind, is_error = ItemKind.ERROR, True
    elif event_type == "turn.completed":
        kind, is_error = ItemKind.DONE, False
    elif any(token in lowered for token in ("exec", "tool", "command", "patch", "mcp")):
        kind, is_error = ItemKind.TOOL_CALL, False
        text = f"[{native_kind}] {text}"
    elif any(token in lowered for token in ("agent_message", "assistant", "output_text")):
        kind, is_error = ItemKind.OUTPUT, False
    elif any(token in lowered for token in ("reasoning", "thinking")):
        kind, is_error = ItemKind.THINKING, False
    else:
        kind, is_error = ItemKind.THINKING, False
        text = f"[{native_kind}] {text}"
    return [NormalizedEvent(
        "Codex",
        current_thread,
        current_turn,
        kind,
        text,
        is_error,
        raw,
        usage=_codex_usage(raw) if event_type == "turn.completed" else None,
    )]
