"""§RPT-2 货 1：模板按题面选 + 读者字段。

09-05 那份「国内大家对豆包的看法」走了默认咨询体，六张表没一张回答「谁在夸什么」；
建议第一条写给了字节的产品经理，而提问的多半是竞品 PM。这两件事各由一半货修。
"""

from __future__ import annotations

from app.plan.question import AUDIENCE_UNKNOWN, audience_from, make_questions
from app.report.polish.run import _audience_view
from app.report.polish.skills import recommend_template


def test_三个题面各命中预期模板():
    assert recommend_template("国内大家对豆包的看法") == "sentiment-brief"
    assert recommend_template("豆包与 Kimi 的竞品对比", entity_count=2) == "competitor-matrix"
    assert recommend_template("AI 编程工具的市场规模与增长") == "consulting"


def test_对比线索但只有一个实体仍回咨询体():
    """「竞品」两个字不作数——矩阵稿得真有两个对象才排得出来。"""
    assert recommend_template("豆包的竞品分析", entity_count=1) == "consulting"


def test_两头都沾的题面归舆情简报():
    """读者问的是「怎么评价」，对比只是范围；判定顺序就是提货单里那三条的书写顺序。"""
    assert recommend_template("豆包和 Kimi 大家怎么评价", entity_count=2) == "sentiment-brief"


def test_空题面与认不出的题面都回默认模板():
    assert recommend_template("") == "consulting"
    assert recommend_template("   ", entity_count=5) == "consulting"


def test_q2_q3_预填不明且不阻塞批准():
    from app.plan.model import Plan
    from tests.plan_factory import make_plan_dict

    plan = Plan.from_dict(make_plan_dict())
    questions = make_questions(plan, "国内大家对豆包的看法")
    by_id = {q["q_id"]: q for q in questions}
    assert set(by_id) == {"q-1", "q-2", "q-3"}
    assert by_id["q-1"]["answer"] is None            # 只有它拦批准
    assert by_id["q-2"]["answer"] == AUDIENCE_UNKNOWN
    assert by_id["q-3"]["answer"] == AUDIENCE_UNKNOWN
    assert by_id["q-2"]["options"][0] == AUDIENCE_UNKNOWN  # 自动批准档点第一个，别替用户瞎猜
    assert by_id["q-3"]["input_type"] == "text"


def test_audience_from_收_plan_也收_dict_快照():
    snapshot = {"decision_balance": [
        {"q_id": "q-2", "answer": "竞品团队"},
        {"q_id": "q-3", "answer": "想知道该不该跟进多模态"},
    ]}
    assert audience_from(snapshot) == {
        "audience_role": "竞品团队", "audience_stake": "想知道该不该跟进多模态"}
    assert audience_from({})["audience_role"] == AUDIENCE_UNKNOWN


def test_读者不明时提示词要写成对不同读者的含义():
    unknown = _audience_view({"audience": {"audience_role": AUDIENCE_UNKNOWN}})
    assert "对不同读者的含义" in unknown and "竞品团队" in unknown
    known = _audience_view({"audience": {"audience_role": "投资与分析",
                                         "audience_stake": "该不该加仓"}})
    assert "对投资与分析意味着什么" in known and "该不该加仓" in known
