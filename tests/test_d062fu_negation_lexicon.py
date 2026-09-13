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


def _real_plan() -> Plan:
    skeleton = json.loads((FIXTURES / "skeleton.json").read_text(encoding="utf-8"))
    assembled = json.loads((FIXTURES / "assembled.json").read_text(encoding="utf-8"))
    entities = []
    for index in (1, 2, 3):
        card = json.loads((FIXTURES / f"entity-{index}.json").read_text(encoding="utf-8"))
        card.setdefault("id", card["canonical"])
        entities.append(card)
    return _build_plan(
        assembled,
        query="国内大家对豆包的看法",
        research_id="r-50600e09f7dd",
        timestamp="2026-09-12T18:31:00+00:00",
        scale="fast",
        market_profile=skeleton["market_profile"],
        market_profile_justification=skeleton["market_profile_justification"],
        subjects=skeleton["subjects"],
        subjects_justification=skeleton["subjects_justification"],
        entities=entities,
        repairs=[],
    )


def _allocation() -> dict:
    return json.loads((FIXTURES / "allocation.json").read_text(encoding="utf-8"))


def _normalize(plan: Plan) -> list[str]:
    # 生产那条路的参数形状（generate.py:1729）；fast 档每 goal 两张。
    return normalize_plan(plan, collection_plan=_allocation(), per_goal_capacity=2)


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
])
def test_反向约束新写法_不摘(line):
    plan = _plan_with([("goal-1", "xhs", "豆包")], {"goal-1": [line]},
                      [_entity("豆包"), _entity("Kimi")])
    notes = normalize_plan(plan)
    assert plan.goals[0].acceptance == [line], notes


def test_词表扩了_正向要求照摘():
    """反面判据：扩词表不许把「要求写 Kimi」的条也豁免掉。"""
    lines = ["报告须含 Kimi 对照小节，仅出现一次的结论需标注样本量",
             "报告须分别呈现豆包与 Kimi 的口碑"]
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
