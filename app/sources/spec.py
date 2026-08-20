"""信息源模块的分散声明契约。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable


_SOURCE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True)
class SourceSpec:
    """源模块自带的最小注册信息。"""

    source_id: str
    tool_name: str
    entrypoint: Callable[..., Any]

    def __post_init__(self) -> None:
        if not _SOURCE_ID_PATTERN.fullmatch(self.source_id):
            raise ValueError("source_id 必须是小写 snake_case")
        expected = f"source.{self.source_id}"
        if self.tool_name != expected:
            raise ValueError(f"工具名必须是 {expected}")
        if not callable(self.entrypoint):
            raise TypeError("entrypoint 必须可调用")


__all__ = ["SourceSpec"]
