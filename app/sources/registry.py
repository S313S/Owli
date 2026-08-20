"""扫描源模块并聚合其 SOURCE_SPEC 声明。"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterable
from types import ModuleType
from typing import Any

from app.sources.spec import SourceSpec


def _modules() -> Iterable[ModuleType]:
    package = importlib.import_module(__package__ or "app.sources")
    ignored = {"registry", "spec"}
    for module in pkgutil.iter_modules(package.__path__, f"{package.__name__}."):
        leaf = module.name.rsplit(".", 1)[-1]
        if leaf.startswith("_") or leaf in ignored:
            continue
        yield importlib.import_module(module.name)


def discover_sources(
    *, modules: Iterable[Any] | None = None
) -> dict[str, SourceSpec]:
    """聚合声明；重复或伪声明立即拒绝。"""

    by_source: dict[str, SourceSpec] = {}
    by_tool: dict[str, SourceSpec] = {}
    for module in _modules() if modules is None else modules:
        spec = getattr(module, "SOURCE_SPEC", None)
        if spec is None:
            continue
        if not isinstance(spec, SourceSpec):
            raise TypeError(f"{module.__name__}.SOURCE_SPEC 必须是 SourceSpec")
        if spec.source_id in by_source:
            raise ValueError(f"重复 source_id：{spec.source_id}")
        if spec.tool_name in by_tool:
            raise ValueError(f"重复 tool_name：{spec.tool_name}")
        by_source[spec.source_id] = spec
        by_tool[spec.tool_name] = spec
    return by_source


def get_source(source_id: str) -> SourceSpec:
    try:
        return discover_sources()[source_id]
    except KeyError as exc:
        raise KeyError(f"未注册的信息源：{source_id}") from exc


def get_tool(tool_name: str):
    for spec in discover_sources().values():
        if spec.tool_name == tool_name:
            return spec.entrypoint
    raise KeyError(f"未注册的信息源工具：{tool_name}")


__all__ = ["discover_sources", "get_source", "get_tool"]
