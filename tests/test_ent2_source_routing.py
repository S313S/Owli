"""§ENT-2：规划期按实体叫法排源——「有没有」看叫法，market_profile 只定顺序。

用户 2026-09-03 傍晚原话：「中文名在中文平台搜，英文名在英文平台搜，这样就能突破
一个产品只局限于国内、国外的搜索」。ENT-1 已经让采集期按语域取词，缺口在前一步：
题面写「国内大家对豆包的看法」判出 cn_product，Reddit 压根不进分配表，取词再准
也无卡可用。
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from app.plan.lint import applicable_sources, entity_locales, lint


def _entity(entity_id: str, zh: str | None, en: str | None, **extra) -> dict:
    return {
        "id": entity_id, "canonical": extra.pop("canonical", entity_id),
        "names": {"zh": zh, "en": en, "aliases": extra.pop("aliases", [])},
        "official_handles": {}, "same_product": extra.pop("same_product", True),
        "note": extra.pop("note", "替身实体卡"),
    }


def test_实体语域按正式名与别名字形认_canonical不算数() -> None:
    assert entity_locales([_entity("豆包", "豆包", "Doubao")]) == {"zh", "en"}
    assert entity_locales([_entity("小罐茶", "小罐茶", None)]) == {"zh"}
    assert entity_locales([_entity("Notion", None, "Notion")]) == {"en"}
    # canonical 不进语域：entity_queries 只在该语域挑不出名字时才退回 canonical，
    # 那种退回产出的正是「拿中文名去搜 Reddit」
    assert entity_locales([{"id": "豆包", "canonical": "豆包", "names": {}}]) == set()
    # 别名按字形归档
    assert entity_locales([_entity("豆包", "豆包", None, aliases=["Doubao"])]) == {"zh", "en"}
    assert entity_locales([]) == set()
    assert entity_locales(None) == set()


def test_许可名单按叫法放宽且只放已注册的源() -> None:
    doubao = [_entity("豆包", "豆包", "Doubao")]
    assert "reddit" in applicable_sources("cn_product", doubao)
    assert "weibo" in applicable_sources("global_product", doubao)
    # bilibili / zhihu 在语域表里但还没注册，不能凭空进许可名单
    assert not {"bilibili", "zhihu"} & applicable_sources("cn_product", doubao)
    assert applicable_sources("cn_product", []) == applicable_sources("cn_product")


def test_规则23_国内题面里的海外源在实体有英文名时不再报错() -> None:
    from tests.plan_factory import make_plan_dict

    plan = make_plan_dict()
    plan["market_profile"] = "cn_product"
    plan["market_profile_justification"] = "题面问的是国内用户怎么看。"
    plan["subjects"] = ["豆包"]
    plan["subjects_justification"] = "研究主体为豆包。"
    agent = plan["goals"][0]["agents"][0]
    agent["entity"] = "豆包"
    agent["capability"]["profile"] = "web-collector"
    agent["capability"]["sources"] = ["reddit"]

    plan["entities"] = [_entity("豆包", "豆包", None)]
    rule23 = [m for m in lint(plan)["errors"] if m.startswith("[规则23]")]
    assert rule23, "只有中文名时 Reddit 仍该被规则 23 拦下"

    plan["entities"] = [_entity("豆包", "豆包", "Doubao")]
    assert not [m for m in lint(plan)["errors"] if m.startswith("[规则23]")]


def test_整条规划链_有中文名的海外产品会被排一张国内源采集卡() -> None:
    from tests.test_plan_generate import _agent, _generate, _valid_skeleton

    skeleton = _valid_skeleton()          # global_product、主体「飞书」
    skeleton["goals"][1]["agents"].insert(
        0, _agent("小红书数据抓取·飞书", "采集研究主体的国内讨论"),
    )
    with tempfile.TemporaryDirectory() as raw:
        plan, _, _ = _generate(Path(raw), [skeleton], bilingual_entities=True)
        allocation = json.loads(
            (Path(raw) / "runs" / plan.research_id / "plan-segments" / "allocation.json").read_text(
                encoding="utf-8",
            )
        )
    pairs = {
        (slot["source_id"], slot["entity"])
        for slots in allocation.values() for slot in slots
    }
    assert ("hacker_news", "飞书") in pairs   # market_profile 的主场源还在
    assert ("xhs", "飞书") in pairs           # 中文叫法把国内源排了进来
    sources = {
        source
        for goal in plan.to_dict()["goals"] for agent in goal["agents"]
        for source in agent.get("capability", {}).get("sources", [])
    }
    assert {"hacker_news", "xhs"} <= sources


# —— §ENT-2 货 3（ENT-1 挂账②）：别名清洗与 same_product 隔离 ——


def _card(payload: dict, entity_id: str) -> dict:
    from app.plan.entities import entity_card

    card = entity_card(payload, entity_id=entity_id)
    assert card is not None
    return card.to_dict()


def test_货3_ENT1真机样本的三条噪音别名全被清掉() -> None:
    """「跳动」（字节跳动的截断）、「TT」「DS」（两字母缩写）——都出自 ENT-1 真机。"""
    names = _card({
        "canonical": "豆包",
        "names": {"zh": "豆包", "en": "Doubao",
                  "aliases": ["跳动", "TT", "DS", "Cici", "doubao AI"]},
        "same_product": True,
    }, "豆包")["names"]
    assert names["aliases"] == ["Cici", "doubao AI"]
    # 长得完全不一样的真别名（Cici 是豆包的海外名）在 same_product=true 时留着


def test_货3_same_product为假时对方产品的名字绝不进本实体别名() -> None:
    """ENT-1 真机最值钱的一条：飞书卡自己写着 same_product=false，却把 Lark 收进别名。"""
    names = _card({
        "canonical": "飞书",
        "names": {"zh": "飞书", "en": "Feishu",
                  "aliases": ["Lark", "飞书文档", "Feishu Suite"]},
        "same_product": False,
        "note": "飞书与 Lark 是面向国内与海外的两套部署，数据不互通。",
    }, "飞书")["names"]
    assert "Lark" not in names["aliases"]
    assert names["aliases"] == ["飞书文档", "Feishu Suite"]   # 认得出是同一串名字的留着

    names = _card({
        "canonical": "抖音",
        "names": {"zh": "抖音", "en": "Douyin",
                  "aliases": ["TikTok", "抖音短视频", "Douyin App"]},
        "same_product": False, "note": "抖音与 TikTok 内容生态独立。",
    }, "抖音")["names"]
    assert names["aliases"] == ["抖音短视频", "Douyin App"]


def test_货3_截断与本实体正式名的边界() -> None:
    from app.plan.entities import clean_aliases

    # 截断不带来新召回，只带来更宽的误命中
    assert clean_aliases(["字节"], ["字节跳动"], same_product=True) == []
    # 等于本实体某个正式名的短名不受长度闸限制（「豆包」本来就是两个字）
    assert clean_aliases(["豆包"], ["Doubao", "豆包"], same_product=True) == ["豆包"]
    # 大小写变体与重复
    assert clean_aliases(
        ["DOUBAO", "doubao"], ["豆包"], same_product=True,
    ) == ["DOUBAO"]
    # same_product=false 的隔离闸不误伤本实体自己的变体
    assert clean_aliases(
        ["Feishu Docs"], ["飞书", "Feishu"], same_product=False,
    ) == ["Feishu Docs"]


# —— §ENT-2 货 4（ENT-1 挂账④⑤）：实体卡耗时事件与补丁式重试 ——


def test_货4_实体卡耗时事件带_elapsed_s_与_count() -> None:
    from tests.test_plan_generate import _generate, _valid_skeleton

    with tempfile.TemporaryDirectory() as raw:
        plan, store, _ = _generate(Path(raw), [_valid_skeleton()])
    resolved = [
        event.raw["entities_resolved"]
        for event in store.events if "entities_resolved" in (event.raw or {})
    ]
    assert len(resolved) == 1
    assert resolved[0]["count"] == len(plan.subjects)
    assert isinstance(resolved[0]["elapsed_s"], float)
    assert resolved[0]["elapsed_s"] >= 0.0


class _CardEngine:
    """按序吐出预设的实体卡原文；记下每轮拿到的提示词。"""

    def __init__(self, payloads: list[dict]) -> None:
        self.payloads = list(payloads)
        self.prompts: list[str] = []

    async def generate(self, name, prompt, adapter):
        del name, adapter
        self.prompts.append(prompt)
        return self.payloads[min(len(self.prompts) - 1, len(self.payloads) - 1)]

    @staticmethod
    def previous_text(name: str) -> str:
        del name
        return '{"canonical": ""}'


def test_货4_卡折不动时按报错处补一轮_原文与原因都递回去() -> None:
    import asyncio

    from app.plan.entities import resolve_entities

    engine = _CardEngine([
        {"names": {"zh": "豆包"}},                       # 缺 canonical，折不动
        {"canonical": "豆包", "names": {"zh": "豆包", "en": "Doubao"}},
    ])
    progress: list[str] = []
    cards = asyncio.run(resolve_entities(
        "国内大家对豆包的看法", ["豆包"], engine, None,
        on_progress=progress.append, search=None,
    ))
    assert [card["id"] for card in cards] == ["豆包"]
    assert len(engine.prompts) == 2
    assert "缺 canonical" in engine.prompts[1]
    assert "只修改上面点名的地方" in engine.prompts[1]
    assert "缺 canonical" not in engine.prompts[0]
    assert any("补一轮" in text for text in progress)


def test_货4_模型明确说不是实体时不再补问() -> None:
    import asyncio

    from app.plan.entities import resolve_entities

    engine = _CardEngine([{"canonical": ""}])
    progress: list[str] = []
    cards = asyncio.run(resolve_entities(
        "国内", ["国内"], engine, None, on_progress=progress.append, search=None,
    ))
    assert cards == []
    assert len(engine.prompts) == 1, "这是合法的否定答案，不该拿补丁重试去磨它"
    assert any("不是实体" in text for text in progress)


def test_规则23_闭集按卡上的实体逐张算_不是全计划一个大闭集() -> None:
    """题面里有一个实体带英文名，不等于只有中文名的那个实体也能上 Reddit。"""
    from tests.plan_factory import make_plan_dict

    plan = make_plan_dict()
    plan["market_profile"] = "cn_product"
    plan["market_profile_justification"] = "题面问的是国内用户怎么看。"
    plan["subjects"] = ["豆包", "小罐茶"]
    plan["subjects_justification"] = "一个有外文名、一个没有。"
    plan["entities"] = [
        _entity("豆包", "豆包", "Doubao"), _entity("小罐茶", "小罐茶", None),
    ]
    agent = plan["goals"][0]["agents"][0]
    agent["capability"]["profile"] = "web-collector"
    agent["capability"]["sources"] = ["reddit"]

    agent["entity"] = "豆包"
    assert not [m for m in lint(plan)["errors"] if m.startswith("[规则23]")]

    agent["entity"] = "小罐茶"
    matches = [m for m in lint(plan)["errors"] if m.startswith("[规则23]")]
    assert len(matches) == 1
    assert "实体 小罐茶 的叫法" in matches[0]
    assert "reddit" not in matches[0].split("可用源=")[1]


# —— §ENT-2 裁决乙（用户 09-03 傍晚）：骨架名额留一位给同一产品的外文名 ——


def test_乙_铺满型题面下留出的那一位真的落到主角的对面语域卡上() -> None:
    """第一轮小跑 r-53d8dc3e0e03 红在这里：6 个竞品铺满 6 个采集位，补位无处可放。"""
    from app.config import load_research_scale_config
    from app.plan.allocation import allocate_collections, subjects_budget

    fast = load_research_scale_config().profile("fast")
    scaffolds = [{"depends_on": []}, {"depends_on": []}, {"depends_on": ["goal-1"]}]
    assert subjects_budget(3, fast) == 5          # 采集位 6，留一位
    assert subjects_budget(3, load_research_scale_config().profile("standard")) is None

    subjects = ["豆包", "字节跳动", "DeepSeek", "Kimi", "文心一言"]
    entities = [_entity("豆包", "豆包", "Doubao")] + [
        _entity(name, name, None) for name in subjects[1:]
    ]
    plan = allocate_collections(subjects, "cn_product", scaffolds, fast, entities)
    pairs = {(s.source_id, s.entity) for slots in plan.values() for s in slots}
    assert ("xhs", "豆包") in pairs
    assert ("reddit", "豆包") in pairs, f"留出的那一位没给到主角：{sorted(pairs)}"
    assert len(pairs) == 6                        # 5 个主位 + 1 个跨语域补位，正好装满
    assert all(len(slots) <= 2 for slots in plan.values())
