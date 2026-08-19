"""引擎原始错误事件的防御性 JSONL 日志。"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from app.adapters.events import NormalizedEvent


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LOG_ROOT = PROJECT_ROOT / "var" / "logs" / "engine-errors"
Clock = Callable[[], datetime]


def _now() -> datetime:
    return datetime.now().astimezone()


def _raw_type(raw: Any) -> str:
    if isinstance(raw, Mapping):
        return str(raw.get("type", ""))
    return str(getattr(raw, "type", ""))


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _json_value(model_dump())
    if is_dataclass(value):
        return _json_value(asdict(value))
    attributes = getattr(value, "__dict__", None)
    if isinstance(attributes, dict):
        return {key: _json_value(item) for key, item in attributes.items()}
    raise TypeError(f"原始事件无法序列化为 JSON：{type(value).__name__}")


def _engine_slug(engine: str) -> str:
    slug = re.sub(r"[^a-z0-9_.-]+", "-", engine.strip().lower()).strip("-.")
    return slug or "unknown"


def append_engine_error(
    event: NormalizedEvent,
    *,
    log_root: Path = DEFAULT_LOG_ROOT,
    clock: Clock = _now,
) -> Path | None:
    """错误事件只写 raw 本体；不截断、不改写、不脱敏。"""
    if not event.is_error and "error" not in _raw_type(event.raw).lower():
        return None
    now = clock()
    path = log_root / f"{_engine_slug(event.engine)}-{now.date().isoformat()}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        _json_value(event.raw),
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8") + b"\n"
    with path.open("ab") as stream:
        stream.write(payload)
    return path


def append_routing_event(
    event: NormalizedEvent,
    *,
    log_root: Path = DEFAULT_LOG_ROOT,
    clock: Clock = _now,
) -> Path:
    """非 CONTINUE 路由事件完整落盘，raw 保持原始结构。"""

    if event.route_state is None:
        raise ValueError("只有路由事件可以写入 routing 日志")
    now = clock()
    path = (
        log_root
        / "routing"
        / f"{_engine_slug(event.engine)}-{now.date().isoformat()}.jsonl"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "engine": event.engine,
        "thread_id": event.thread_id,
        "turn_id": event.turn_id,
        "item_kind": event.item_kind.value,
        "text": event.text,
        "is_error": event.is_error,
        "route_state": event.route_state,
        "suspend_new_tasks": event.suspend_new_tasks,
        "failover_target": event.failover_target,
        "no_fallback_left": event.no_fallback_left,
        "scope": event.scope,
        "allow_current_task_to_finish": event.allow_current_task_to_finish,
        "raw": _json_value(event.raw),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8") + b"\n"
    with path.open("ab") as stream:
        stream.write(encoded)
    return path


def append_outcome_event(
    event: NormalizedEvent,
    *,
    log_root: Path = DEFAULT_LOG_ROOT,
    clock: Clock = _now,
) -> Path:
    """把 FAIL / UNAVAILABLE 终止事件写入独立日志，避免污染原生错误样本。"""

    if event.outcome not in {"FAIL", "UNAVAILABLE"}:
        raise ValueError("终止事件 outcome 必须是 FAIL 或 UNAVAILABLE")
    now = clock()
    path = (
        log_root
        / "outcomes"
        / f"{_engine_slug(event.engine)}-{now.date().isoformat()}.jsonl"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {
            "engine": event.engine,
            "thread_id": event.thread_id,
            "turn_id": event.turn_id,
            "outcome": event.outcome,
            "text": event.text,
            "raw": _json_value(event.raw),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8") + b"\n"
    with path.open("ab") as stream:
        stream.write(payload)
    return path
