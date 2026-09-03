"""§ENT-1 货 1：规划期实体卡解析——上限、降级与网页线索。"""

from __future__ import annotations

import asyncio
from typing import Any


class FakeWorkspace:
    """只记提示词、按预置回答吐 JSON 的规划段替身。"""

    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.prompts: dict[str, str] = {}

    async def generate(self, name: str, prompt: str, adapter: Any) -> Any:
        del adapter
        self.prompts[name] = prompt
        answer = self.answers.get(name, {})
        if isinstance(answer, Exception):
            raise answer
        return answer


def _card(name: str, en: str, **extra: Any) -> dict[str, Any]:
    return {
        "canonical": name,
        "names": {"zh": name, "en": en, "aliases": extra.pop("aliases", [])},
        "official_handles": {},
        "same_product": extra.pop("same_product", True),
        "note": extra.pop("note", f"{name} 是一个产品"),
    }


def test_实体卡上限五个且多出来的实体不查不问() -> None:
    from app.plan.entities import MAX_ENTITIES, resolve_entities

    subjects = [f"产品{index}" for index in range(1, 9)]
    workspace = FakeWorkspace({
        f"entity-{index}": _card(f"产品{index}", f"P{index}")
        for index in range(1, 9)
    })
    searched: list[str] = []

    def search(query: str, window: str, *, max_results: int) -> list:
        del window, max_results
        searched.append(query)
        return []

    cards = asyncio.run(resolve_entities(
        "八个产品的口碑对比", subjects, workspace, None, search=search,
    ))
    assert len(cards) == MAX_ENTITIES == 5
    assert [card["id"] for card in cards] == subjects[:5]
    assert len(searched) == 5 and len(workspace.prompts) == 5


def test_网页查不动照样出卡且线索进提示词() -> None:
    from app.plan.entities import resolve_entities

    workspace = FakeWorkspace({"entity-1": _card("豆包", "Doubao")})

    def broken(query: str, window: str, *, max_results: int) -> list:
        del query, window, max_results
        raise RuntimeError("EXA_API_KEY 缺失")

    cards = asyncio.run(resolve_entities(
        "国内大家对豆包的看法", ["豆包"], workspace, None, search=broken,
    ))
    assert cards[0]["names"] == {"zh": "豆包", "en": "Doubao", "aliases": []}
    assert "这次没查到可用的网页线索" in workspace.prompts["entity-1"]

    workspace = FakeWorkspace({"entity-1": _card("豆包", "Doubao")})
    hit = [{
        "title": "豆包官网", "permalink": "https://www.doubao.com/",
        "content_excerpt": "豆包（Doubao）是字节跳动推出的 AI 助手",
    }]
    asyncio.run(resolve_entities(
        "国内大家对豆包的看法", ["豆包"], workspace, None,
        search=lambda *args, **kwargs: hit,
    ))
    assert "https://www.doubao.com/" in workspace.prompts["entity-1"]


def test_单张卡失败只丢这一张且整步降级不阻塞规划() -> None:
    from app.plan.entities import resolve_entities

    workspace = FakeWorkspace({
        "entity-1": RuntimeError("规划段预算耗尽"),
        "entity-2": {"canonical": ""},          # 模型自认「不是真实产品」
        "entity-3": _card("飞书", "Feishu"),
    })
    progress: list[str] = []
    cards = asyncio.run(resolve_entities(
        "三个产品", ["钉钉", "国内", "飞书"], workspace, None,
        on_progress=progress.append, search=None,
    ))
    assert [card["id"] for card in cards] == ["飞书"]
    assert any("钉钉" in text and "跳过" in text for text in progress)
    assert any("国内" in text and "跳过" in text for text in progress)


def test_实体卡随计划落盘且规划期不因它变慢地打网络() -> None:
    """整条规划链：实体卡进 plan JSON；没注入 search 就一次网都不打。"""
    from tests.test_plan_generate import _generate, _valid_skeleton
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as raw:
        plan, _, engine = _generate(Path(raw), [_valid_skeleton()])
    entities = plan.to_dict()["entities"]
    assert [item["id"] for item in entities] == plan.subjects
    assert entities[0]["names"]["en"].endswith("-en")
    assert len(engine.entity_tasks) == len(plan.subjects)
