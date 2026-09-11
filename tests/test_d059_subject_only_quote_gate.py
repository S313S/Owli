"""§D-059：原声实体闸只认**研究主体**，不认竞品。

护的是这条真机缺陷：正式稿把「最重要的是，Kimi 开源，每个人都可以用。」摆成
豆包的**正向用户原声**（实测 S89，已进 `tables.json`）。

⚠️ 病不在闸——闸是逐句判的，`_names_the_entity(那句, ["豆包","Doubao"])` 返回
`False`，判得没错。**病在喂给它的名单**：`quote_gate_names` 把计划里所有实体的
叫法摊平（实测 18 个，Kimi / DeepSeek / 月之暗面 全在内），于是「只收点名了
**研究对象**的原声」被实现成「只收点名了**任何一个**实体的原声」。

⛔ 所以本文件的用例全部架在**名单**上，不在闸上。只验闸的用例会全绿而缺陷仍在。
"""

from __future__ import annotations

from app.reliability.coding import CODING_VERSION, coding_tables
from app.report.polish.tables import quote_gate_names, subject_canonicals

#: 就是真机上出表的那一句，一字不改。它夸的是 Kimi，不是豆包。
_RIVAL_QUOTE = "最重要的是，Kimi 开源，每个人都可以用。"

#: 真机 `r-3e04f808dffd` 的 plan_snapshot 形状，按本包要用到的字段缩写：
#: `subjects` 主角与竞品混在一个列表、没有 `competitors` 字段、
#: `same_product` 连 Kimi 那条也是 `true`（它答的是「中外名字是不是同一个产品」）。
def _real_shaped_plan(question: str = "国内大家对豆包的看法") -> dict:
    return {
        "research_question": question,
        "title": question,
        "subjects": ["豆包", "Doubao", "DeepSeek", "Kimi", "文心一言"],
        "subjects_justification": "豆包及其外文名Doubao为研究主体，DeepSeek、Kimi、"
                                  "文心一言为国内主流同类AI助手，用于对照国内用户看法。",
        "entities": [
            {"id": "豆包", "canonical": "豆包", "same_product": True,
             "names": {"zh": "豆包", "en": "Doubao", "aliases": ["Doubao", "豆包AI"]}},
            {"id": "Doubao", "canonical": "豆包", "same_product": True,
             "names": {"zh": "豆包", "en": "Doubao", "aliases": ["字节豆包"]}},
            {"id": "DeepSeek", "canonical": "DeepSeek", "same_product": False,
             "names": {"zh": "深度求索", "en": "DeepSeek", "aliases": ["深度求索"]}},
            {"id": "Kimi", "canonical": "Kimi", "same_product": True,
             "names": {"zh": "Kimi", "en": "Kimi", "aliases": ["Kimi Chat", "月之暗面"]}},
            {"id": "文心一言", "canonical": "文心一言", "same_product": True,
             "names": {"zh": "文心一言", "en": "ERNIE Bot", "aliases": ["文小言"]}},
        ],
    }


def _coded(index: int, quote: str):
    return {"id": f"ev-{index:03d}", "platform": "douyin", "goal_id": "goal-2",
            "extra": {"coding": {
                "coding_version": CODING_VERSION, "audience": "不明", "scenario": "工作",
                "attitude": "正", "topics": ["功能与能力"], "quote": quote}}}


def test_闸名单只含主体叫法_不含任何竞品():
    """⭐ 本包的红就在这一条：修复前这个名单有 18 个名字，Kimi 在内。"""
    names = quote_gate_names(_real_shaped_plan())

    assert set(names) == {"豆包", "Doubao", "豆包AI", "字节豆包"}, \
        "只许留豆包的各种叫法"
    for rival in ("Kimi", "Kimi Chat", "月之暗面", "DeepSeek", "深度求索",
                  "文心一言", "文小言", "ERNIE Bot"):
        assert rival not in names, f"竞品叫法 {rival} 不该进原声闸的名单"


def test_夸竞品的那句真机原声不再出表():
    """判据 1：这一句修前出表（S89），修后必须被闸拦下。"""
    rows = [_coded(1, _RIVAL_QUOTE)]
    data = coding_tables(rows, entity_names=quote_gate_names(_real_shaped_plan()))

    assert data["quotes"] == [], "夸 Kimi 的话不许摆成豆包的正面原声"
    assert data["quotes_dropped"]["点了别的名"]["条数"] == 1, "丢弃要数得出来"


def test_点名主体的原声一条都不许被误伤():
    """判据 2：收紧名单的风险是把该收的也收掉，所以正面必须同时验。"""
    rows = [_coded(1, "豆包的语音功能是真的好用。"),
            _coded(2, "Doubao is surprisingly good at this.")]
    names = quote_gate_names(_real_shaped_plan())

    assert len(coding_tables(rows, entity_names=names)["quotes"]) == 2, \
        "中文名与英文名两条都得留住——归一后英文原声不该被丢"


def test_主角是从题面推出来的_不是列表第一个():
    """⛔ 不许靠「`subjects` 第一个」这类巧合：本例第一个恰好是豆包，那是运气。

    把题面换成竞品、`subjects` 顺序一个字不改——主角必须跟着题面走。
    """
    plan = _real_shaped_plan("国内大家对 Kimi 的看法")
    assert plan["subjects"][0] == "豆包", "夹具前提：列表第一个仍是豆包"

    assert subject_canonicals(plan) == ["Kimi"], "主角要跟题面走，不跟列表顺序走"
    assert "豆包" not in quote_gate_names(plan)


def test_题面同时点名两个产品时两个都算主角():
    """对比型题面（「豆包 vs DeepSeek 谁更好」）本来就有两个主角，不该只留一个。"""
    names = quote_gate_names(_real_shaped_plan("豆包和 DeepSeek 谁更好"))

    assert {"豆包", "DeepSeek"} <= set(names)
    assert "Kimi" not in names, "没被题面点名的仍是竞品"


def test_题面点不出主角时退回旧行为_不把原声池筛空():
    """兜底：宁可不收紧，也不要在读不出主角时把池筛空（与 `accepted` 为空同族）。

    ⚠️ 这条兜底是**已知限制**：走兜底时竞品原声照样进池。
    """
    plan = _real_shaped_plan("国产 AI 助手的口碑如何")

    assert subject_canonicals(plan) == [], "题面一个产品名都没点，推不出主角"
    assert set(quote_gate_names(plan)) >= {"豆包", "Kimi", "DeepSeek"}, \
        "推不出主角就退回认全部实体，不是返回空名单"


def test_老快照没有题面字段时也不炸_退回旧行为():
    """`plan_snapshot` 写死在库里，加新字段救不了历史报告——缺字段必须走兜底。"""
    plan = _real_shaped_plan()
    plan.pop("research_question"); plan.pop("title")

    assert subject_canonicals(plan) == []
    assert "豆包" in quote_gate_names(plan)


def test_提示词名单与出表闸名单是同一份():
    """⛔ 两条路各抽一份名单，就会「提示词让摘豆包、闸却放竞品过」，而两边都绿。"""
    import json
    from app.reliability.coding import _plan_entity_names

    plan = _real_shaped_plan()
    expected = quote_gate_names(plan)

    assert _plan_entity_names({"plan_snapshot": plan}) == expected
    assert _plan_entity_names(
        {"plan_snapshot": json.dumps(plan, ensure_ascii=False)}) == expected


def test_一个字的叫法不进名单():
    """一个字拿去做包含匹配满篇都是，和 `plan/lint.py` 同一条规矩。"""
    plan = _real_shaped_plan("大家对豆包的看法")
    plan["entities"][0]["names"]["aliases"].append("豆")

    assert "豆" not in quote_gate_names(plan)
