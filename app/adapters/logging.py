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
