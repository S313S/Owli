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
