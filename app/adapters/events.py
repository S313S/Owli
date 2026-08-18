"""双引擎统一事件模型；以 Codex 的 thread/turn/item 三级为基准。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any


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
) -> NormalizedEvent:
    return NormalizedEvent(
        engine="Claude",
        thread_id=thread_id,
        turn_id=turn_id,
        item_kind=item_kind,
        text=str(text or ""),
        is_error=is_error,
        raw=message,
    )


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
        )]
    if isinstance(message, sdk.SystemMessage):
        return [_event(
            message,
            thread_id=thread_id,
            turn_id=turn_id,
            item_kind=ItemKind.THINKING,
            text=f"[session] {getattr(message, 'subtype', '')}",
        )]
    return [_event(
        message,
        thread_id=thread_id,
        turn_id=turn_id,
        item_kind=ItemKind.THINKING,
        text=f"[{type(message).__name__}]",
    )]


def normalize_codex_event(raw: Any) -> list[NormalizedEvent]:
    """M1-b 实装：本包只锁定公共签名，不猜测 Codex 原生字段。"""
    raise NotImplementedError("Codex 事件归一由 M1-b 实装")
