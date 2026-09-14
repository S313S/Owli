"""§D-069：题面「对比 X」里的 X 是对照，不是主角。

病根（WX-1 第四轮 standard 规划期小跑 `r-wx1-0914-1749`）：题面写成「国内大家对豆包的看法（对比
DeepSeek、Kimi、文心一言、通义千问）」后，`subject_canonicals` 把题面点名的五家全算主角；分配表
`_protagonist_first` 要求恰好一个主角 ⇒ 返回 None、整张退回实体轮转（豆包国内卡只剩小红书、唯一
公众号卡是「公众号·通义千问」），原声闸名单也混进 17 个竞品叫法。

夹具是真的：`../Owli-wx1/var/wx1-run-0914*`（只读）的 plan-segments 与 plan.json 题面/实体卡另存到
tests/fixtures/d069；`r-50600e09f7dd` 的 plan_snapshot 取自 obs7 留服库的 backup 副本。
`golden_bbcf658.json` 是改前代码的 13 份读数，由 `scripts/acceptance/d069/protagonist_census.py dump`
生成。⛔ 本文件期望值一律按题面人工写死。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from app.report.polish.tables import quote_gate_names, subject_canonicals

FIXTURES = Path(__file__).parent / "fixtures" / "d069"
_CENSUS = Path(__file__).resolve().parents[1] / "scripts" / "acceptance" / "d069" / "protagonist_census.py"


def _census():
    spec = importlib.util.spec_from_file_location("d069_census", _CENSUS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _plan(question: str) -> dict:
    """五实体形状与 r-wx1-0914-1749 相同，只换题面。"""
    plan = json.loads((FIXTURES / "wx1-0914c" / "plan.json").read_text(encoding="utf-8"))
    return {**plan, "research_question": question, "title": question}


DOUBAO = {"豆包", "Doubao", "豆包AI", "字节豆包"}


def test_造红_wx1第四轮题面_旧码五个主角_新码只剩豆包() -> None:
    golden = json.loads((FIXTURES / "golden_bbcf658.json").read_text(encoding="utf-8"))
    old = golden["wx1-0914c/standard"]
    assert old["protagonists"] == ["DeepSeek", "Kimi", "文心一言", "豆包", "通义千问"], "红：五家全算主角"
    assert old["protagonist_first"] is None, "红：主角优先不成立、退回轮转"
    assert old["allocation"] == json.loads(
        (FIXTURES / "wx1-0914c" / "allocation.json").read_text(encoding="utf-8")), "改前重算 = 小跑落盘"

    plan = json.loads((FIXTURES / "wx1-0914c" / "plan.json").read_text(encoding="utf-8"))
    assert subject_canonicals(plan) == ["豆包"]
    assert set(quote_gate_names(plan)) == DOUBAO


def test_造红_分配表主角优先生效_口碑媒体goal拿豆包国内四源_竞品卡仍在() -> None:
    census = _census()
    reading = census.reading(census.cases()["wx1-0914c/standard"])
    assert reading["protagonist_first"][0] == "豆包"
    short = reading["allocation_short"]
    assert {"xhs·豆包", "douyin·豆包", "weibo·豆包", "wechat_mp·豆包"} <= set(short["goal-3"]), \
        "goal-3「国内用户与媒体对豆包的评价与口碑」要拿国内 UGC 三源 + 公众号"
    entities = {slot.split("·", 1)[1] for slots in short.values() for slot in slots}
    assert {"DeepSeek", "Kimi", "文心一言", "通义千问"} <= entities, "竞品卡不许被挤没"


def test_不退化_只点豆包的研究读数与改前逐字相同() -> None:
    census = _census()
    golden = json.loads((FIXTURES / "golden_bbcf658.json").read_text(encoding="utf-8"))
    for name, case in census.cases().items():
        if name == census.RED_CASE:
            continue
        assert census.reading(case) == golden[name], f"{name} 主角表/分配表/原声闸名单变了"


@pytest.mark.parametrize(("question", "protagonists"), [
    ("国内大家对豆包的看法（对比 DeepSeek、Kimi、文心一言、通义千问）", ["豆包"]),
    ("豆包的口碑怎么样，对比 Kimi、DeepSeek", ["豆包"]),
    ("豆包 vs DeepSeek 谁更好", ["豆包"]),
    ("Doubao VS Kimi", ["豆包"]),
    ("与 DeepSeek 和 Kimi 相比，豆包在国内口碑如何", ["豆包"]),
    ("与Kimi相比豆包怎么样", ["豆包"]),
    ("豆包和 Kimi 比哪个好", ["豆包"]),
    ("国内用户怎么看豆包（对照：Kimi）", ["豆包"]),
    ("相较于通义千问，豆包的优势", ["豆包"]),
])
def test_比较标记后的实体是对照不是主角(question: str, protagonists: list[str]) -> None:
    plan = _plan(question)
    assert subject_canonicals(plan) == protagonists
    assert set(quote_gate_names(plan)) == DOUBAO, "原声闸只认主角叫法"


def test_只写对比A和B_主角为空走兜底_闸退回认全部实体() -> None:
    plan = _plan("对比 Kimi 和 DeepSeek")
    assert subject_canonicals(plan) == []
    assert {"豆包", "Kimi", "DeepSeek", "通义千问"} <= set(quote_gate_names(plan))


def test_没有比较标记的并列_两家仍都算主角() -> None:
    """用户没说谁是对照——与 §D-059 用例「豆包和 DeepSeek 谁更好」同口径。"""
    assert subject_canonicals(_plan("豆包和 DeepSeek 谁更好")) == ["DeepSeek", "豆包"]


def test_遮罩外再点一次名仍是主角() -> None:
    assert subject_canonicals(_plan("对比豆包，大家怎么看 Kimi 的口碑")) == ["Kimi"]
    assert subject_canonicals(_plan("豆包（对比 Kimi）和 Kimi 的口碑")) == ["Kimi", "豆包"]


def test_比较标记不越过右括号吞掉后面的主角() -> None:
    assert subject_canonicals(_plan("（对比 Kimi、DeepSeek）国内大家怎么看豆包")) == ["豆包"]


def test_比较标记不越过字段边界() -> None:
    """research_question 以前缀标记收尾时，不能把 title 里的主角一起遮掉。"""
    plan = {**_plan("x"), "research_question": "看法对比", "title": "豆包"}
    assert subject_canonicals(plan) == ["豆包"]


def test_竞品分析这类题面不受影响() -> None:
    assert subject_canonicals(_plan("豆包的竞品分析")) == ["豆包"]
