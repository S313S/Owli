"""§ENT-1 货 4：采集查询词按源的语域取名，分别查询再合并去重。"""

from __future__ import annotations

import asyncio
from typing import Any


class FakeStore:
    def __init__(self, snapshot: dict[str, Any] | None) -> None:
        self.snapshot = snapshot

    def get_report(self, report_id: str) -> dict[str, Any]:
        del report_id
        return {"plan_snapshot": self.snapshot}


def _snapshot(market_profile: str = "cn_product") -> dict[str, Any]:
    return {
        "market_profile": market_profile,
        "entities": [{
            "id": "豆包", "canonical": "豆包",
            "names": {"zh": "豆包", "en": "Doubao", "aliases": ["豆包大模型", "Doubao AI", "豆包AI"]},
            "official_handles": {}, "same_product": True, "note": "字节的 AI 助手",
        }, {
            "id": "抖音", "canonical": "抖音",
            "names": {"zh": "抖音", "en": "Douyin", "aliases": []},
            "official_handles": {}, "same_product": False, "note": "与 TikTok 不是一个产品",
        }],
        "goals": [{"agents": [
            {"agent_id": "data-collection", "entity": "豆包"},
            {"agent_id": "data-collection-2", "entity": "抖音"},
        ]}],
    }


def _capability(source_id: str) -> Any:
    from app.adapters.capability import Capability

    return Capability(
        tools=(f"source.{source_id}",), sources=(source_id,), network="sources_only",
    )


def _call(adapter: Any, source_id: str, query: str, agent_id: str, events: list | None = None) -> Any:
    return asyncio.run(adapter.call(
        f"source.{source_id}", query, "30d",
        research_id="r-1", goal_id="goal-1", agent_id=agent_id,
        capability=_capability(source_id), on_event=(events.append if events is not None else None),
    ))


def test_国内源取中文名海外源取英文名且每源最多两个() -> None:
    from app.adapters.source_mcp import SourceToolAdapter

    seen: list[str] = []

    def tool(query: str, window: str, **kwargs: Any) -> list[dict[str, Any]]:
        del window, kwargs
        seen.append(query)
        return [{"permalink": f"https://example.com/{query}"}]

    adapter = SourceToolAdapter(
        {"source.xhs": tool, "source.reddit": tool}, store=FakeStore(_snapshot()),
    )
    _call(adapter, "xhs", "豆包", "data-collection")
    assert seen == ["豆包", "豆包大模型"]

    seen.clear()
    _call(adapter, "reddit", "豆包", "data-collection")
    assert seen == ["Doubao", "Doubao AI"]


def test_分别查询再合并去重不拼_OR() -> None:
    from app.adapters.source_mcp import SourceToolAdapter

    def tool(query: str, window: str, **kwargs: Any) -> list[dict[str, Any]]:
        del window, kwargs
        return [
            {"permalink": "https://example.com/shared"},
            {"permalink": f"https://example.com/{query}"},
        ]

    events: list[Any] = []
    adapter = SourceToolAdapter({"source.xhs": tool}, store=FakeStore(_snapshot()))
    result = _call(adapter, "xhs", "豆包", "data-collection", events)
    assert [item["permalink"] for item in result] == [
        "https://example.com/shared",
        "https://example.com/豆包",
        "https://example.com/豆包大模型",
    ]
    plans = [e for e in events if e.get("type") == "source_query_plan"]
    assert plans and plans[0]["data"] == {
        "source_id": "xhs", "entity": "豆包", "locale": "zh",
        "queries": ["豆包", "豆包大模型"],
    }


def test_跨语域源跟着计划的市场属性走() -> None:
    from app.adapters.source_mcp import SourceToolAdapter

    seen: list[str] = []

    def tool(query: str, window: str, **kwargs: Any) -> list[dict[str, Any]]:
        del window, kwargs
        seen.append(query)
        return []

    for profile, expected in (("cn_product", ["豆包", "豆包大模型"]), ("global_product", ["Doubao", "Doubao AI"])):
        seen.clear()
        adapter = SourceToolAdapter(
            {"source.web_search": tool}, store=FakeStore(_snapshot(profile)),
        )
        _call(adapter, "web_search", "豆包", "data-collection")
        assert seen == expected, profile


def test_同名不同产品不跨语域借名() -> None:
    """抖音的 en 是 Douyin，不会因为「海外源」就去搜 TikTok。"""
    from app.adapters.source_mcp import SourceToolAdapter

    seen: list[str] = []

    def tool(query: str, window: str, **kwargs: Any) -> list[dict[str, Any]]:
        del window, kwargs
        seen.append(query)
        return []

    adapter = SourceToolAdapter({"source.reddit": tool}, store=FakeStore(_snapshot()))
    _call(adapter, "reddit", "抖音", "data-collection-2")
    assert seen == ["Douyin"]


def test_没库没快照没实体一律原样退回单查询() -> None:
    from app.adapters.source_mcp import SourceToolAdapter

    seen: list[str] = []

    def tool(query: str, window: str, **kwargs: Any) -> list[dict[str, Any]]:
        del window, kwargs
        seen.append(query)
        return []

    for store in (None, FakeStore(None), FakeStore({"entities": [], "goals": []})):
        seen.clear()
        adapter = SourceToolAdapter({"source.xhs": tool}, store=store)
        _call(adapter, "xhs", "模型给的词", "data-collection")
        assert seen == ["模型给的词"], store

    # 卡登记的实体没有实体卡（超出 5 张上限）时同样只查一次
    seen.clear()
    snapshot = _snapshot()
    snapshot["goals"][0]["agents"].append({"agent_id": "data-collection-9", "entity": "通义千问"})
    adapter = SourceToolAdapter({"source.xhs": tool}, store=FakeStore(snapshot))
    _call(adapter, "xhs", "通义千问", "data-collection-9")
    assert seen == ["通义千问"]
