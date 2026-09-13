"""§D-062-fu：反向约束豁免词表漏「未越界写入 / 仅出现」+ 短形产物路径闸不看。

现象（r-50600e09f7dd，ALLOC-2 重采规划期）：[修正4] 摘掉 goal-1 第 6 条
「任务文本仅出现闭集内的实体叫法（豆包、Doubao、豆包AI、字节豆包），未越界写入「DeepSeek」「Kimi」
等其他实体叫法。」——它提到竞品是为了**禁止**写竞品，是 D-062 口径③要豁免的防串号约束。
按子句判时第二子句「未越界写入「DeepSeek」「Kimi」等其他实体叫法」里没有旧词表任何一个词。

挂账一并收：路径判只认 `goals/goal-N/<file>` 全形；引擎写的短形
`goal-1/data-collection.json`、`goal-2/data-collection-3`（连扩展名都没有）闸看不见。
短形归一到全形再判；⛔ 「上游 goal-1/goal-2」这种纯 goal 并列不是路径。

夹具是真的：r-50600e09f7dd 的 plan-segments 原样拷进 tests/fixtures/d062fu，
走 `assembled.json → _build_plan → normalize_plan` 整条（同 D-061/D-062 手法）。
⛔ 不改口径③本身、不动 lint。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.plan.generate import _build_plan
from app.plan.model import Plan
from app.plan.normalize import normalize_plan
from tests.test_d062_acceptance_gate import _entity, _plan_with

FIXTURES = Path(__file__).parent / "fixtures" / "d062fu"
REVERSE = "未越界写入「DeepSeek」「Kimi」等其他实体叫法"


RUN_A = Path(__file__).parent / "fixtures" / "d062fu-run-a"


def _real_plan(fixtures: Path = FIXTURES, query: str = "国内大家对豆包的看法",
               research_id: str = "r-50600e09f7dd") -> Plan:
    skeleton = json.loads((fixtures / "skeleton.json").read_text(encoding="utf-8"))
    assembled = json.loads((fixtures / "assembled.json").read_text(encoding="utf-8"))
    entities = []
    for index in (1, 2, 3):
        card = json.loads((fixtures / f"entity-{index}.json").read_text(encoding="utf-8"))
        card.setdefault("id", card["canonical"])
        entities.append(card)
    return _build_plan(
        assembled,
        query=query,
        research_id=research_id,
        timestamp="2026-09-12T18:31:00+00:00",
        scale="fast",
        market_profile=skeleton["market_profile"],
        market_profile_justification=skeleton["market_profile_justification"],
        subjects=skeleton["subjects"],
        subjects_justification=skeleton["subjects_justification"],
        entities=entities,
        repairs=[],
    )


def _allocation(fixtures: Path = FIXTURES) -> dict:
    return json.loads((fixtures / "allocation.json").read_text(encoding="utf-8"))


def _normalize(plan: Plan, fixtures: Path = FIXTURES) -> list[str]:
    # 生产那条路的参数形状（generate.py:1729）；fast 档每 goal 两张。
    return normalize_plan(plan, collection_plan=_allocation(fixtures), per_goal_capacity=2)


# —— 判据 1：真夹具造红 ————————————————————————————————


def test_真夹具_goal1第6条反向约束不许摘():
    """旧代码：[修正4] goal-1.acceptance[5] 已摘（红）。新代码：留下，且全计划零 [修正4]。"""
    plan = _real_plan()
    before = [list(g.acceptance) for g in plan.goals]
    assert REVERSE in before[0][5], "夹具就是现场那条原文"
    notes = [n for n in _normalize(plan) if n.startswith("[修正4]")]
    assert notes == [], notes
    assert [list(g.acceptance) for g in plan.goals] == before


def test_真夹具_goal3的上游goal并列不是短形路径():
    """goal-3 第 7 条「不与上游 goal-1/goal-2 已采集的组合冲突」——短形归一不许把它当路径摘。
    已被上一条覆盖（全计划零 [修正4]），单列出来是因为它是短形口径最容易误伤的现场原文。"""
    plan = _real_plan()
    line = plan.goals[2].acceptance[6]
    assert "goal-1/goal-2" in line
    _normalize(plan)
    assert line in plan.goals[2].acceptance


def _run_a_plan() -> Plan:
    return _real_plan(RUN_A, query="豆包语音输入法的竞品分析", research_id="r-d062fu-0913-a")


def test_小跑a_括号元组里的逗号不切子句_不重复消费条不许摘():
    """本包第一轮小跑 r-d062fu-0913-a 现形：goal-2 第 7 条「本 goal 不重复消费上游 goal-1
    已完成的 (xhs, 豆包语音输入法) 与 (douyin, 豆包语音输入法) 组合，…」被摘。
    两个病：① 子句按英文逗号切，把「(xhs, 豆包语音输入法)」切成两半，豆包落进没有否定词
    的半截；② 「不重复」不在词表。它提到豆包是为了**禁止**重复采，是反向约束。
    """
    plan = _run_a_plan()
    goal2 = list(plan.goals[1].acceptance)
    assert "不重复消费上游 goal-1" in goal2[6]
    notes = [n for n in _normalize(plan, RUN_A) if n.startswith("[修正4]")]
    assert goal2[6] in plan.goals[1].acceptance, notes
    assert goal2[5] in plan.goals[1].acceptance, "「不混入主角豆包语音输入法」那条也得留"


def test_小跑a_被删卡产物的路径条照摘():
    """反面判据：goal-3 第 7 条引了 D-061 删掉的 web_search·讯飞输入法 卡产物，照摘——
    全计划 [修正4] 只剩这一条。"""
    plan = _run_a_plan()
    notes = [n for n in _normalize(plan, RUN_A) if n.startswith("[修正4]")]
    assert len(notes) == 1 and notes[0].startswith("[修正4] goal-3.acceptance[6]"), notes
    assert "goals/goal-2/data-collection-5.json" in notes[0]


# —— 词表：按子句判，新增写法 ————————————————————————————


@pytest.mark.parametrize("line", [
    "任务文本仅出现豆包叫法，未越界写入「Kimi」等其他实体叫法",
    "任务文本不越界写入 Kimi 的叫法",
    "正文只出现豆包，不得出现 Kimi",
    "正文仅出现豆包专名，Kimi 不出现",
    "检索词排除 Kimi 及其别名",
    "避免把 Kimi 的评价混进豆包小节",
    "不混入 Kimi 的任何叫法",
    "严禁写入 Kimi 叫法",
    "勿引入 Kimi 的语料",
    "本 goal 不重复采集上游已完成的 (xhs, Kimi) 组合",
    "采集组合不与上游 (xhs, Kimi)、(douyin, Kimi) 重复",
])
def test_反向约束新写法_不摘(line):
    plan = _plan_with([("goal-1", "xhs", "豆包")], {"goal-1": [line]},
                      [_entity("豆包"), _entity("Kimi")])
    notes = normalize_plan(plan)
    assert plan.goals[0].acceptance == [line], notes


def test_词表扩了_正向要求照摘():
    """反面判据：扩词表不许把「要求写 Kimi」的条也豁免掉。"""
    lines = ["报告须含 Kimi 对照小节，仅出现一次的结论需标注样本量",
             "报告须分别呈现豆包与 Kimi 的口碑",
             "报告须汇总 (xhs, Kimi) 组合的高频词，不得遗漏 permalink"]
    plan = _plan_with([("goal-1", "xhs", "豆包")], {"goal-1": lines + ["文件存在"]},
                      [_entity("豆包"), _entity("Kimi")])
    normalize_plan(plan)
    assert plan.goals[0].acceptance == ["文件存在"]


# —— 短形路径：归一到全形再判 ——————————————————————————————


def test_短形路径引已删产物_摘():
    """旧代码只认 goals/ 前缀：这条引了不存在的 goal-1/data-collection-9.json 却留下（红）。"""
    plan = _plan_with([("goal-1", "xhs", "豆包"), ("goal-2", "weibo", "豆包")],
                      {"goal-2": ["交叉验证章消费 goal-1/data-collection-9.json 的豆包语料",
                                  "文件存在"]},
                      [_entity("豆包")])
    notes = normalize_plan(plan)
    assert plan.goals[1].acceptance == ["文件存在"], notes
    assert any("goals/goal-1/data-collection-9.json" in n for n in notes), notes


def test_短形无扩展名_按产物主干匹配():
    plan = _plan_with([("goal-1", "xhs", "豆包"), ("goal-2", "weibo", "豆包")],
                      {"goal-2": ["交叉验证章消费 goal-1/data-collection-1 的语料",
                                  "交叉验证章消费 goal-1/data-collection-7 的语料"]},
                      [_entity("豆包")])
    notes = normalize_plan(plan)
    assert plan.goals[1].acceptance == ["交叉验证章消费 goal-1/data-collection-1 的语料"], notes
    assert any("goals/goal-1/data-collection-7" in n for n in notes), notes


@pytest.mark.parametrize("line", [
    "不与上游 goal-1/goal-2 已采集的组合冲突",
    "goal-1/goal-2/goal-3 三个 goal 的采集组合互不重复",
    "fs.read 覆盖 goals/goal-1/** 与 goal-2/**",
    "交叉验证章消费 goal-1/data-collection-1.json。",
    "交叉验证章显式引用 goals/goal-1/data-collection-1.json.",
])
def test_不是路径或路径存在_不摘(line):
    plan = _plan_with([("goal-1", "xhs", "豆包"), ("goal-2", "weibo", "豆包")],
                      {"goal-2": [line]}, [_entity("豆包")])
    notes = normalize_plan(plan)
    assert plan.goals[1].acceptance == [line], notes
