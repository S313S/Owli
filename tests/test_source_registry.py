from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_自动发现_HN_并按工具名解析入口() -> None:
    from app.sources import hn, web_search
    from app.sources.registry import discover_sources, get_tool

    discovered = discover_sources()

    assert discovered["hacker_news"] is hn.SOURCE_SPEC
    assert discovered["web_search"] is web_search.SOURCE_SPEC
    assert get_tool("source.hacker_news") is hn.search
    assert get_tool("source.web_search") is web_search.search


def test_SourceSpec_拒绝不自洽声明() -> None:
    from app.sources.spec import SourceSpec

    with pytest.raises(ValueError, match="工具名必须是 source.bad_source"):
        SourceSpec("bad_source", "source.other", lambda: None)
    with pytest.raises(ValueError, match="source_id"):
        SourceSpec("Bad-Source", "source.Bad-Source", lambda: None)
    with pytest.raises(TypeError, match="entrypoint"):
        SourceSpec("bad_source", "source.bad_source", None)  # type: ignore[arg-type]


def test_聚合器拒绝重复源与重复工具名() -> None:
    from app.sources.registry import discover_sources
    from app.sources.spec import SourceSpec

    first = SimpleNamespace(
        __name__="fake.first",
        SOURCE_SPEC=SourceSpec("one", "source.one", lambda: 1),
    )
    duplicate_source = SimpleNamespace(
        __name__="fake.second",
        SOURCE_SPEC=SourceSpec("one", "source.one", lambda: 2),
    )
    with pytest.raises(ValueError, match="重复 source_id"):
        discover_sources(modules=[first, duplicate_source])

    duplicate_tool = SimpleNamespace(
        __name__="fake.third",
        SOURCE_SPEC=SimpleNamespace(
            source_id="two",
            tool_name="source.one",
            entrypoint=lambda: 3,
        ),
    )
    with pytest.raises((TypeError, ValueError), match="SOURCE_SPEC|重复"):
        discover_sources(modules=[first, duplicate_tool])


def test_registry_不硬编码任何具体源条目() -> None:
    source = (ROOT / "app" / "sources" / "registry.py").read_text(encoding="utf-8")

    for forbidden in ("hacker_news", "web_search", "product_hunt", "source.x"):
        assert forbidden not in source
