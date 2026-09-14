"""§D-066：适配层两处静默缩水。

1. 源失败被吞成「搜到 0 条」：TikHub 402 时每个检索词各自发 `source_unavailable`
   并返回 `[]`，适配层合并后回 `result=[] error=null`，模型把缺口写成 empty_result。
   修后：合并为空且有检索词失败 → 抛 `SourceUnavailableError`（MCP 载荷 `error` 非空、
   带 closed_reason / HTTP 状态 / 上游原文）；部分失败 → 保留成功行并带 `query_failures`。
2. 英文检索词大小写重复：豆包卡 en 名 `Doubao` + 别名 `doubao` 占满上限 2。
   修后按 casefold + NFKC + 空白归一去重，空出的名额按原候选顺序补下一个叫法。

判据量在适配层返回结构（`SourceToolAdapter.call` 的返回值 / 异常、`_build_tool_payload`），不是日志。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from app.adapters.source_mcp import SourceToolAdapter, _build_tool_payload, entity_queries

RESEARCH = "r-d066"


class _SnapshotStore:
    def __init__(self, names: dict[str, Any]) -> None:
        self.snapshot = {
            "market_profile": "cn_product",
            "entities": [{"id": "豆包", "canonical": "豆包", "names": names}],
            "goals": [{"goal_id": "goal-1", "agents": [
                {"agent_id": "data-collection", "entity": "豆包"},
            ]}],
        }

    def get_report(self, research_id: str):
        return {"plan_snapshot": self.snapshot} if research_id == RESEARCH else None


#: 真机 alloc2 r-50600e09f7dd 的豆包实体卡原样。
REAL_DOUBAO = {"zh": "豆包", "en": "Doubao", "aliases": ["Doubao", "豆包AI", "字节豆包"]}


def _402(query: str) -> dict[str, Any]:
    return {"type": "source_unavailable", "data": {
        "source": "xhs", "reason": "tool_unavailable", "closed_reason": "tikhub_http_402",
        "provider": "tikhub", "endpoint": "/api/v1/xiaohongshu/web_v2/search_notes",
        "http_status": 402, "upstream_code": None,
        "detail": "Insufficient balance, please recharge", "task_continues": True,
        "calls": {"search_notes": 1, "get_image_note_detail": 0},
    }}


def _call(tool: Any, *, source: str = "xhs", names: dict[str, Any] | None = None,
          events: list | None = None, store: Any = "default") -> Any:
    adapter = SourceToolAdapter(
        {f"source.{source}": tool},
        store=_SnapshotStore(names or REAL_DOUBAO) if store == "default" else store,
    )
    return asyncio.run(adapter.call(
        f"source.{source}", "豆包", "30d",
        research_id=RESEARCH, goal_id="goal-1", agent_id="data-collection",
        capability=SimpleNamespace(tools=(f"source.{source}",), sources=(source,),
                                   network="sources_only"),
        on_event=events.append if events is not None else None,
        with_comments="off",
    ))


def _row(query: str, n: int) -> dict[str, Any]:
    return {"permalink": f"https://example.com/{query}/{n}", "title": f"{query}-{n}"}


# ---------------------------------------------------------------- ① 源失败


def test_两个检索词都402_适配层报源不可用_不是空结果() -> None:
    seen: list[str] = []

    def tool(query: str, window: str, on_event=None) -> list:
        seen.append(query)
        on_event(_402(query))
        return []

    events: list[Any] = []
    with pytest.raises(Exception) as caught:
        _call(tool, events=events)
    assert seen == ["豆包", "豆包AI"]
    message = str(caught.value)
    assert "源不可用" in message and "402" in message and "Insufficient balance" in message
    assert "「豆包」" in message and "「豆包AI」" in message
    assert "tikhub_http_402" in message
    # 源事件照样回流、不因抛错丢失（落盘与对账靠它）
    assert sum(e.get("type") == "source_unavailable" for e in events) == 2

    # MCP 回灌载荷：error 非空，result 不是一个会被读成「0 条」的空列表
    payload, _text = _build_tool_payload(
        result=None, events=events, error=caught.value, event_path=None, byte_limit=64_000,
    )
    assert payload["error"] is not None and "402" in payload["error"]["message"]
    assert payload["error"]["type"] == "SourceUnavailableError"


def test_一词成功一词失败_保留成功行并带失败说明() -> None:
    def tool(query: str, window: str, on_event=None) -> list:
        if query == "豆包AI":
            on_event(_402(query))
            return []
        return [_row(query, 1), _row(query, 2)]

    result = _call(tool)
    assert isinstance(result, dict)
    assert [row["title"] for row in result["evidence"]] == ["豆包-1", "豆包-2"]
    failures = result["query_failures"]
    assert len(failures) == 1 and failures[0]["query"] == "豆包AI"
    assert failures[0]["closed_reason"] == "tikhub_http_402"
    assert failures[0]["http_status"] == 402
    assert "402" in result["note"] and "「豆包AI」" in result["note"]


def test_一词失败一词正常搜空_仍按源不可用报() -> None:
    def tool(query: str, window: str, on_event=None) -> list:
        if query == "豆包":
            on_event(_402(query))
        return []

    with pytest.raises(Exception, match="402"):
        _call(tool)


def test_单检索词失败同样不吞() -> None:
    def tool(query: str, window: str, on_event=None) -> list:
        on_event(_402(query))
        return []

    with pytest.raises(Exception, match="tikhub_http_402"):
        _call(tool, store=None)


def test_真搜空不是失败_原样回空列表() -> None:
    def tool(query: str, window: str, on_event=None) -> list:
        on_event({"type": "source_empty", "data": {"source": "xhs", "reason": "empty_result"}})
        return []

    assert _call(tool) == []


def test_有行时的告警事件不算检索词失败_形状不变() -> None:
    """池源「最新批次未登录但老批次有货」会发 source_unavailable 同时返回行——不是失败。"""

    def tool(query: str, window: str, on_event=None) -> list:
        on_event({"type": "source_unavailable", "data": {
            "source": "xhs", "reason": "login_required", "closed_reason": "login_required"}})
        return [_row(query, 1)]

    result = _call(tool)
    assert isinstance(result, list) and len(result) == 2


def test_Reddit全供应商不可用_没有closed_reason也带出原因() -> None:
    def tool(query: str, window: str, on_event=None) -> list:
        on_event({"type": "source_unavailable", "data": {
            "source": "reddit", "reason": "all_providers_unavailable",
            "failures": [{"provider": "prowlo", "error": "HTTP 503"}]}})
        return []

    with pytest.raises(Exception) as caught:
        _call(tool, source="reddit")
    assert "all_providers_unavailable" in str(caught.value)
    assert "「Doubao」" in str(caught.value)


# ---------------------------------------------------------------- ② 大小写重复


def test_真机豆包卡英文语域只有一个叫法_不凑数() -> None:
    assert entity_queries({"names": REAL_DOUBAO}, "en", "豆包") == ["Doubao"]
    names = {"zh": "豆包", "en": "Doubao", "aliases": ["Doubao", "doubao", "豆包AI", "字节豆包"]}
    assert entity_queries({"names": names}, "en", "豆包") == ["Doubao"]


def test_真机Kimi卡_小写别名不再占第二个名额() -> None:
    """alloc2 r-50600e09f7dd 的 Kimi 卡原样：旧码英文语域搜「Kimi」「kimi」。"""
    kimi = {"zh": "Kimi智能助手", "en": "Kimi",
            "aliases": ["kimi", "Kimi Chat", "月之暗面", "Moonshot"]}
    assert entity_queries({"names": kimi}, "en", "Kimi") == ["Kimi", "Kimi Chat"]


def test_大小写全半角空白重复不占名额_空位补下一个叫法() -> None:
    names = {"zh": "豆包", "en": "Doubao",
             "aliases": ["doubao", "ＤＯＵＢＡＯ", "Doubao ", "Doubao  AI", "Doubao AI", "Cici"]}
    queries = entity_queries({"names": names}, "en", "豆包")
    assert queries == ["Doubao", "Doubao AI"]
    assert len({q.casefold() for q in queries}) == 2


def test_Reddit卡实际检索词两个大小写不同名() -> None:
    seen: list[str] = []

    def tool(query: str, window: str, on_event=None) -> list:
        seen.append(query)
        return [_row(query, 1)]

    names = {"zh": "豆包", "en": "Doubao", "aliases": ["doubao", "Doubao AI", "豆包AI"]}
    events: list[Any] = []
    _call(tool, source="reddit", names=names, events=events)
    assert seen == ["Doubao", "Doubao AI"]

    seen.clear()
    _call(tool, source="reddit", names=REAL_DOUBAO)
    assert seen == ["Doubao"]
