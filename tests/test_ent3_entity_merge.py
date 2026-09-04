"""§ENT-3：同一产品的中英文名合一，跨语域位按档位留足。"""

from __future__ import annotations

from app.config import load_research_scale_config
from app.plan.allocation import (
    allocate_collections, cross_locale_slots_budget, subjects_budget,
)


SCAFFOLDS = [
    {"depends_on": []}, {"depends_on": []}, {"depends_on": ["goal-1"]},
]


def _entity(entity_id: str, canonical: str, zh: str | None, en: str | None,
            aliases: list[str] | None = None) -> dict:
    return {
        "id": entity_id, "canonical": canonical,
        "names": {"zh": zh, "en": en, "aliases": aliases or []},
        "official_handles": {}, "same_product": True, "note": "测试实体卡",
    }


def test_货1_骨架提示词明确中英文名只占一个subject_规则33拦重复卡() -> None:
    from app.plan.generate import _skeleton_prompt
    from app.plan.lint import lint
    from tests.plan_factory import make_plan_dict

    prompt = _skeleton_prompt("国内大家对豆包的看法", [], scale="fast")
    assert "同一产品的中外叫法只算一个实体" in prompt
    assert "canonical + 别名" in prompt

    plan = make_plan_dict()
    plan["subjects"] = ["豆包", "Doubao"]
    plan["subjects_justification"] = "题面同时写了中英文名。"
    plan["entities"] = [
        _entity("豆包", "豆包", "豆包", "Doubao"),
        _entity("Doubao", "豆包", "豆包", "Doubao"),
    ]
    assert any(error.startswith("[规则33]") for error in lint(plan)["errors"])

    plan["entities"] = [
        _entity("subject-a", "Foo", None, "Foo", ["Bar"]),
        _entity("subject-b", "Bar", None, "Bar", ["Foo"]),
    ]
    assert any(error.startswith("[规则33]") for error in lint(plan)["errors"])

    plan["entities"] = [
        _entity("豆包", "豆包", "豆包", None, ["Doubao"]),
        _entity("Doubao", "Doubao", None, "Doubao"),
    ]
    assert any(error.startswith("[规则33]") for error in lint(plan)["errors"])


def test_货2_同canonical或正式名的卡按首项确定性合并() -> None:
    from app.plan.entities import merge_entity_cards

    cards, merged = merge_entity_cards([
        _entity("豆包", "豆包", "豆包", "Doubao", ["Cici"]),
        _entity("Doubao", "豆包", "豆包", "Doubao", ["豆包AI"]),
        _entity("ChatGPT", "ChatGPT", None, "ChatGPT"),
    ])
    assert [card["id"] for card in cards] == ["豆包", "ChatGPT"]
    assert merged == [{"from": ["豆包", "Doubao"], "to": "豆包"}]
    assert cards[0]["names"]["aliases"] == ["Cici", "豆包AI"]


def test_货3_合并事件保留来源与去向() -> None:
    from app.plan.generate import _entities_merged_event

    event = _entities_merged_event(
        "r-ent3", {"from": ["豆包", "Doubao"], "to": "豆包"},
    )
    assert event.raw == {
        "entities_merged": {"from": ["豆包", "Doubao"], "to": "豆包"},
    }
    assert "Doubao" in event.text and "豆包" in event.text


def test_货2至3_整条规划链先合并再重算分配并发事件(tmp_path) -> None:
    from tests.test_plan_generate import _agent, _generate, _valid_skeleton

    skeleton = _valid_skeleton()
    skeleton["market_profile"] = "cn_product"
    skeleton["subjects"] = ["豆包", "Doubao", "ChatGPT"]
    skeleton["subjects_justification"] = "主体的中英文名与一个竞品。"
    skeleton["goals"][0]["agents"] = [
        _agent("小红书数据抓取·豆包", "采集豆包"),
        _agent("微博数据抓取·ChatGPT", "采集 ChatGPT"),
        _agent("Reddit 数据抓取·豆包", "采集 Doubao"),
        _agent("X 数据抓取·豆包", "采集 Doubao"),
        _agent("HN 数据抓取·豆包", "采集 Doubao"),
        _agent("Product Hunt 数据抓取·豆包", "采集 Doubao"),
    ]
    skeleton["goals"][2]["agents"].insert(
        0, _agent("网页搜索数据抓取·豆包", "补回合并前的实体采集位"),
    )
    payloads = [
        _entity("豆包", "豆包", "豆包", "Doubao"),
        _entity("Doubao", "豆包", "豆包", "Doubao"),
        _entity("ChatGPT", "ChatGPT", None, "ChatGPT"),
    ]

    plan, store, _ = _generate(tmp_path, [skeleton], entity_payloads=payloads)

    assert plan.subjects == ["豆包", "ChatGPT"]
    assert [entity.id for entity in plan.entities] == ["豆包", "ChatGPT"]
    merged = [event.raw["entities_merged"] for event in store.events
              if "entities_merged" in (event.raw or {})]
    assert merged == [{"from": ["豆包", "Doubao"], "to": "豆包"}]
    allocation = next(event.raw["collection_plan"] for event in store.events
                      if "collection_plan" in (event.raw or {}))
    assert sum(map(len, allocation.values())) == 7
    assert sum(event.outcome == "retrying" for event in store.events) == 2


def test_货1_fast重复骨架由规则33打回并补回第三个独立实体(tmp_path) -> None:
    from copy import deepcopy
    from tests.test_plan_generate import _agent, _generate, _valid_skeleton

    valid = _valid_skeleton()
    valid["market_profile"] = "cn_product"
    valid["subjects"] = ["豆包", "ChatGPT", "Kimi"]
    valid["subjects_justification"] = "主体与两个独立竞品。"
    valid["goals"][0]["agents"] = [
        _agent("小红书数据抓取·豆包", "采集豆包"),
        _agent("Reddit 数据抓取·豆包", "采集 Doubao"),
    ]
    valid["goals"][1]["agents"] = [
        _agent("微博数据抓取·ChatGPT", "采集 ChatGPT"),
        _agent("X 数据抓取·豆包", "采集 Doubao"),
    ]
    valid["goals"][2]["agents"] = [
        _agent("网页搜索数据抓取·Kimi", "采集 Kimi"),
        _agent("HN 数据抓取·豆包", "采集 Doubao"),
    ]
    duplicate = deepcopy(valid)
    duplicate["subjects"] = ["豆包", "Doubao", "ChatGPT"]
    cards = {
        "豆包": _entity("豆包", "豆包", "豆包", "Doubao"),
        "Doubao": _entity("Doubao", "豆包", "豆包", "Doubao"),
        "ChatGPT": _entity("ChatGPT", "ChatGPT", None, "ChatGPT"),
        "Kimi": _entity("Kimi", "Kimi", "Kimi", None),
    }

    plan, store, engine = _generate(
        tmp_path, [valid], scale="fast",
        skeleton_payloads=[duplicate, valid],
        entity_payloads_by_subject=cards,
    )

    assert plan.subjects == ["豆包", "ChatGPT", "Kimi"]
    assert engine._skeleton_calls == 2
    retries = [event for event in store.events if event.outcome == "retrying"]
    assert any("[规则33]" in event.text for event in retries)
    allocation = next(event.raw["collection_plan"] for event in store.events
                      if "collection_plan" in (event.raw or {}))
    assert sum(map(len, allocation.values())) == 6


def test_货2_fast最终兜底合并也不减少采集位() -> None:
    fast = load_research_scale_config().profile("fast")
    skipped: list[dict[str, str]] = []
    plan = allocate_collections(
        ["豆包", "ChatGPT"], "cn_product", SCAFFOLDS, fast,
        [
            _entity("豆包", "豆包", "豆包", "Doubao"),
            _entity("ChatGPT", "ChatGPT", None, "ChatGPT"),
        ],
        scale="fast", entity_slot_target=3, skipped=skipped,
    )
    pairs = {(slot.source_id, slot.entity) for slots in plan.values() for slot in slots}
    assert len(pairs) == 6
    assert len({source for source, entity in pairs if entity == "豆包" and source in {
        "reddit", "x", "hacker_news", "product_hunt",
    }}) == 3
    assert skipped == []


def test_货6_fast留三位_standard主角留四位() -> None:
    config = load_research_scale_config()
    fast, standard = config.profile("fast"), config.profile("standard")
    assert subjects_budget(3, fast, scale="fast") == 3
    assert subjects_budget(3, standard, scale="standard") is None
    assert cross_locale_slots_budget(standard, scale="fast") == 3
    assert cross_locale_slots_budget(fast, scale="standard") == 4

    subjects = ["豆包", "DeepSeek", "Kimi"]
    entities = [
        _entity("豆包", "豆包", "豆包", "Doubao"),
        _entity("DeepSeek", "DeepSeek", "DeepSeek", None),
        _entity("Kimi", "Kimi", "Kimi", None),
    ]
    fast_plan = allocate_collections(
        subjects, "cn_product", SCAFFOLDS, fast, entities, scale="fast",
    )
    fast_pairs = {(slot.source_id, slot.entity) for slots in fast_plan.values() for slot in slots}
    assert len(fast_pairs) == 6
    assert {source for source, entity in fast_pairs if entity == "豆包"} == {
        "xhs", "reddit", "x", "hacker_news",
    }

    standard_plan = allocate_collections(
        ["豆包"], "cn_product", SCAFFOLDS, standard, entities[:1],
        scale="standard",
    )
    assert {slot.source_id for slots in standard_plan.values() for slot in slots} == {
        "xhs", "reddit", "x", "hacker_news", "product_hunt",
    }


def test_货6_装不下的跨语域位尽力跳过并留下诊断() -> None:
    fast = load_research_scale_config().profile("fast")
    skipped: list[dict[str, str]] = []
    allocate_collections(
        ["豆包", "DeepSeek", "Kimi", "文心一言"], "cn_product", SCAFFOLDS,
        fast, [_entity("豆包", "豆包", "豆包", "Doubao")],
        scale="fast", skipped=skipped,
    )
    assert skipped == [{
        "entity": "豆包", "source_id": "hacker_news", "reason": "capacity",
    }]
