"""§PLAN-1 货 1：采集分配表——规则 25 的分子分母第一次在同一步定下来。"""

from __future__ import annotations

import asyncio
import json
import random
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import load_research_scale_config
from app.plan.allocation import (
    allocate_collections, collection_capacity, collection_plan_dict,
)
from tests.test_plan_generate import FakeEngine, FakeStore, ForbiddenEngine

FAST = load_research_scale_config().profile("fast")
STANDARD = load_research_scale_config().profile("standard")
TEA = ["小罐茶", "八马茶业", "大益", "竹叶青", "澜沧古茶"]
FAST_TEA = TEA[:3]
SCAFFOLDS = [
    {"depends_on": []}, {"depends_on": []}, {"depends_on": ["goal-1", "goal-2"]},
]


def _pairs(plan) -> set[tuple[str, str]]:
    return {(s.source_id, s.entity) for slots in plan.values() for s in slots}


def test_fast档五品牌每个一位_同goal源数不超限_对不重复() -> None:
    plan = allocate_collections(TEA, "cn_product", SCAFFOLDS, FAST)
    assert {s.entity for slots in plan.values() for s in slots} == set(TEA)
    assert all(len(slots) <= 2 for slots in plan.values())
    assert all(len({s.source_id for s in slots}) <= 2 for slots in plan.values())
    assert len(_pairs(plan)) == len(TEA)
    assert "weibo" in {s.source_id for slots in plan.values() for s in slots}
    # 有 depends_on 的 goal-3 最后才分，只拿到剩下那一位
    assert len(plan["goal-3"]) == 1


def test_单主体标准档只占goal1的HN() -> None:
    plan = collection_plan_dict(allocate_collections(["飞书"], "global_product", SCAFFOLDS, STANDARD))
    assert plan == {
        "goal-1": [{"entity": "飞书", "source_id": "hacker_news", "collector_name": "HN 数据抓取"}],
        "goal-2": [], "goal-3": [],
    }


def test_超出容量抛错且容量算法可读() -> None:
    assert collection_capacity(3, FAST) == 6
    assert collection_capacity(3, STANDARD) is None
    with pytest.raises(ValueError, match="超出采集容量"):
        allocate_collections([f"品牌{i}" for i in range(7)], "cn_product", SCAFFOLDS, FAST)


def test_骨架层拦住超容量subjects_提示词写明上限() -> None:
    from app.plan.generate import _skeleton_prompt, _skeleton_scaffolds

    skeleton = {
        "market_profile": "cn_product", "market_profile_justification": "国内茶叶品牌。",
        "subjects": [f"品牌{i}" for i in range(7)], "subjects_justification": "七个品牌。",
        "goals": [
            {"title": "一", "objective": "采", "depends_on": []},
            {"title": "二", "objective": "采", "depends_on": []},
            {"title": "三", "objective": "写", "depends_on": ["goal-1"]},
        ],
    }
    # §ENT-3：6 个采集位留 3 个给主角跨语域，实体上限随之收敛为 3。
    with pytest.raises(ValueError, match="超出 fast 档研究实体上限 3"):
        _skeleton_scaffolds(skeleton, scale="fast", scale_config=load_research_scale_config())
    assert "subjects 最多 3 个" in _skeleton_prompt("茶叶", [], scale="fast")
    assert "留三位给主角的跨语域来源" in _skeleton_prompt("茶叶", [], scale="fast")
    assert "subjects 最多" not in _skeleton_prompt("茶叶", [], scale="standard")

    # 恰好 3 个照过；第 4 个才拦
    skeleton["subjects"] = [f"品牌{i}" for i in range(3)]
    _skeleton_scaffolds(skeleton, scale="fast", scale_config=load_research_scale_config())
    skeleton["subjects"] = [f"品牌{i}" for i in range(4)]
    with pytest.raises(ValueError, match="超出 fast 档研究实体上限 3"):
        _skeleton_scaffolds(skeleton, scale="fast", scale_config=load_research_scale_config())


def test_规则25锚到被分配的goal_规则31点名未落实的对_且都能路由() -> None:
    from app.plan.generate import _affected_goal_indices
    from app.plan.lint import lint
    from tests.plan_factory import attach_rating_agents, make_agent, make_plan_dict

    collector = make_agent("data-collection", "goal-1")
    collector["display_name"] = "小红书数据抓取·小罐茶"
    collector["entity"] = "小罐茶"
    collector["capability"] = {**collector["capability"], "profile": "web-collector", "sources": ["xhs"]}
    plan = make_plan_dict()
    plan["goals"][0]["agents"] = [collector]
    plan["subjects"] = ["小罐茶", "大益"]
    plan["subjects_justification"] = "两个品牌。"
    attach_rating_agents(plan)
    allocation = {
        "goal-1": [{"entity": "小罐茶", "source_id": "xhs", "collector_name": "小红书数据抓取"}],
        "goal-2": [{"entity": "大益", "source_id": "weibo", "collector_name": "微博数据抓取"}],
    }
    errors = lint(plan, collection_plan=allocation)["errors"]
    r25 = [e for e in errors if e.startswith("[规则25]")]
    r31 = [e for e in errors if e.startswith("[规则31]")]
    assert r25 == ["[规则25] goal-2/subjects 缺少实体采集 agent：大益；请在本 goal 补充该实体的采集 agent，或在其他 goal 分摊该实体"]
    assert len(r31) == 1 and r31[0].startswith("[规则31] goal-2/collection-plan 分配的采集对未落实：微博数据抓取·大益（source_id=weibo）")
    assert _affected_goal_indices(r25 + r31, 3) == [2]
    # 无分配表：规则 25 回落到旧锚点（最后一个 goal），规则 31 不出声
    legacy = lint(plan)["errors"]
    assert any(e.startswith("[规则25] goal-3/") for e in legacy)
    assert not any(e.startswith("[规则31]") for e in legacy)


CN_SOURCES = {
    "xhs": "小红书数据抓取", "weibo": "微博数据抓取", "web_search": "网页搜索数据抓取",
    "douyin": "抖音数据抓取", "wechat_mp": "微信公众号数据抓取", "x": "X 数据抓取",
}


def _tea_skeleton() -> dict:
    return {
        "market_profile": "cn_product", "market_profile_justification": "国内茶叶品牌。",
        "subjects": list(FAST_TEA), "subjects_justification": "三个可采集的茶叶品牌。",
        "goals": [
            {"title": "声量矩阵", "objective": "采集三品牌社媒声量。", "depends_on": [], "agents": []},
            {"title": "内容效率", "objective": "采集内容互动表现。", "depends_on": [], "agents": []},
            {"title": "综合研判", "objective": "交叉验证并成稿。", "depends_on": ["goal-1", "goal-2"], "agents": []},
        ],
    }


class ObedientEngine(FakeEngine):
    """照必采清单造 goal 段的模型替身；种子随机加清单外采集章与非采集章。"""

    def __init__(self, seed: int, *, drop_first_slot_of: int | None = None) -> None:
        super().__init__([_tea_skeleton()])
        self.seed = seed
        self.drop_first_slot_of = drop_first_slot_of
        self._used: set[tuple[str, str]] = set()

    async def run(self, task, ctx, on_event=None):
        stem = task.output_path.stem
        if stem == "skeleton" or "-ch-" in stem:
            return await super().run(task, ctx, on_event)
        number = int(stem.removeprefix("goal-"))
        call = self._goal_calls.get(number, 0)
        self._goal_calls[number] = call + 1
        self.tasks.append(task)
        slots = list(self._prompt_json(task.body, "必采清单 JSON="))
        if call == 0 and number == self.drop_first_slot_of and slots:
            slots = slots[1:]
        rng = random.Random(self.seed * 100 + number)
        agents = [
            {"name": f"{s['collector_name']}·{s['entity']}", "task": "采集证据", "output": {"shape": "array"}}
            for s in slots
        ]
        self._used |= {(s["source_id"], s["entity"]) for s in slots}
        goal_sources = {s["source_id"] for s in slots}
        if len(agents) < 2 and rng.random() < 0.5:
            # 只在本 goal 已分配的源内加同源异实体，避免抢占后续 goal 的必采对。
            pool = sorted(goal_sources)
            options = [(src, e) for src in pool for e in FAST_TEA if (src, e) not in self._used]
            if options:
                src, entity = rng.choice(options)
                self._used.add((src, entity))
                agents.append({"name": f"{CN_SOURCES[src]}·{entity}", "task": "采集证据", "output": {"shape": "array"}})
        if 4 - len(agents) >= 2 and rng.random() < 0.7:
            agents.append({"name": "交叉验证", "task": "交叉验证证据一致性", "output": {"shape": "object"}})
        agents.append({"name": "报告撰写", "task": "撰写带角标的 Markdown 报告", "output": {"shape": "object"}})
        self._current["goals"][number - 1]["agents"] = agents
        payload = {
            "deliverable": {"format": "markdown", "shape": "object", "path": f"stage-{number}.md", "description": "阶段结论。"},
            "acceptance": ["文件存在且至少包含 1 条带链接记录"],
            "agents": agents,
        }
        task.output_path.parent.mkdir(parents=True, exist_ok=True)
        task.output_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return SimpleNamespace(succeeded=True)


def _run(tmp_path: Path, engine: ObedientEngine):
    from app.adapters.routing import RoutedAdapter
    from app.plan.generate import generate_plan

    store = FakeStore(tmp_path)
    adapter = RoutedAdapter(adapters={"claude": engine, "codex": ForbiddenEngine()})
    plan = asyncio.run(generate_plan("茶叶领域社媒竞品洞察", store, adapter, scale="fast"))
    return plan, store


@pytest.mark.parametrize("seed", range(20))
def test_听话引擎二十个种子全过段级lint零重试(tmp_path, seed) -> None:
    plan, store = _run(tmp_path, ObedientEngine(seed))
    assert [e for e in store.events if e.outcome == "retrying"] == []
    assert not any(e.text.startswith("机械修正") for e in store.events)
    collected = {a.entity for g in plan.goals for a in g.agents if a.entity}
    assert collected >= set(FAST_TEA)
    allocation = json.loads((store.runs_root / plan.research_id / "plan-segments" / "allocation.json").read_text())
    assert {s["entity"] for slots in allocation.values() for s in slots} == set(FAST_TEA)
    assert any(e.text.startswith("采集分配表：goal-1=") for e in store.events)


def test_不听话引擎丢一对_只重生被分配的goal_且第二轮过(tmp_path) -> None:
    engine = ObedientEngine(3, drop_first_slot_of=2)
    plan, store = _run(tmp_path, engine)
    assert engine._goal_calls == {1: 1, 2: 2, 3: 1}
    retries = [e for e in store.events if e.outcome == "retrying"]
    assert len(retries) == 1
    assert "[规则31] goal-2/collection-plan" in retries[0].text
    assert "[规则31]" in engine.tasks[-1].body  # 错误原文回灌到被点名那段
    assert {a.entity for g in plan.goals for a in g.agents if a.entity} >= set(FAST_TEA)


def test_骨架提示词给market_profile下定义_按面向市场判不按原产地() -> None:
    """§D-031-wb 抓到的新缺陷：`market_profile` 是个没定义的枚举。

    「国内大家对workbuddy的看法」被骨架判成 `global_product`，理由写「措辞暗含其
    并非本土原生产品」——模型自造了「按产品原产地判」的语义。一判海外，
    `_SOURCE_MARKET_PROFILES` 就把 weibo/douyin/wechat_mp 整体滤掉，分配表根本
    不给微博这个源，计划零微博章。提示词层只能锁文案（模型输出不可测），
    锁的是「按面向的市场判」与「国内一律 cn_product」这两句口径。
    """

    from app.plan.generate import _skeleton_prompt

    prompt = _skeleton_prompt("国内大家对workbuddy的看法", [])
    assert "面向哪个市场的受众与舆论" in prompt
    assert "与产品的原产地、公司归属、名字是不是英文都无关" in prompt
    assert "一律取 cn_product" in prompt
    assert "拿不准时取 cn_product" in prompt


# —— §ENT-2 货 1：分配表按实体叫法定「有没有」，market_profile 只定顺序 ——

DOUBAO = {
    "id": "豆包", "canonical": "豆包",
    "names": {"zh": "豆包", "en": "Doubao", "aliases": []},
}
XHS_ONLY = {
    "id": "小罐茶", "canonical": "小罐茶",
    "names": {"zh": "小罐茶", "en": None, "aliases": ["小罐茶业"]},
}


def test_ent2_国内题面有英文名时海外源进闭集且排在国内源之后() -> None:
    from app.plan.allocation import ordered_sources

    assert ordered_sources("cn_product", [DOUBAO])[:6] == ordered_sources("cn_product")
    assert "reddit" in ordered_sources("cn_product", [DOUBAO])
    assert "reddit" not in ordered_sources("cn_product", [XHS_ONLY])


def test_ent2_海外题面有中文名时国内源进闭集() -> None:
    from app.plan.allocation import ordered_sources

    assert "xhs" in ordered_sources("global_product", [DOUBAO])
    assert "weibo" in ordered_sources("global_product", [DOUBAO])
    assert "weibo" not in ordered_sources("global_product")


def test_ent2_无实体时排源与分配表逐字退回旧行为() -> None:
    from app.plan.allocation import ordered_sources

    assert ordered_sources("cn_product", []) == ordered_sources("cn_product")
    assert ordered_sources("cn_product", None) == ordered_sources("cn_product")
    for profile in (FAST, STANDARD):
        assert collection_plan_dict(
            allocate_collections(TEA, "cn_product", SCAFFOLDS, profile, [])
        ) == collection_plan_dict(
            allocate_collections(TEA, "cn_product", SCAFFOLDS, profile)
        )


def test_ent3_中外都有叫法的主角按standard补四个跨语域位() -> None:
    plan = allocate_collections(
        ["豆包"], "cn_product", SCAFFOLDS, STANDARD, [DOUBAO],
    )
    got = {source for source, entity in _pairs(plan) if entity == "豆包"}
    assert got == {"xhs", "reddit", "x", "hacker_news", "product_hunt"}, got
    # 反过来：题面判成海外，中文名照样把小红书排进来
    plan = allocate_collections(
        ["豆包"], "global_product", SCAFFOLDS, STANDARD, [DOUBAO],
    )
    assert {s for s, e in _pairs(plan) if e == "豆包"} == {
        "hacker_news", "xhs", "weibo", "douyin", "wechat_mp",
    }


def test_ent2_只有单侧叫法的实体不补位_章预算不够时也不抛错() -> None:
    plan = allocate_collections(
        ["小罐茶"], "cn_product", SCAFFOLDS, STANDARD, [XHS_ONLY],
    )
    assert len(_pairs(plan)) == 1
    # fast 档 6 个采集位装 5 个实体，补位只补得下 1 个，剩下的静默放弃
    entities = [dict(DOUBAO, id=name, canonical=name) for name in TEA]
    plan = allocate_collections(TEA, "cn_product", SCAFFOLDS, FAST, entities)
    assert len(_pairs(plan)) == 6
    assert all(len(slots) <= 2 for slots in plan.values())
