"""§RPT-2 货 5：工作稿三处小修——实体去重、缺失节人话、证据缺口摊成人话行。

判据落在渲染出来的文本上：`conclusion_invalid`、`gap={` 不许出现在读者看得到的地方。
"""

from __future__ import annotations

from app.report.render import (
    dedupe_entity_lines,
    humanize_gap_lines,
    missing_text,
    parse_report,
)

WORK = {
    "title": "国内大家对豆包的看法",
    "sections": [
        {"section_id": "ch-6/entities", "goal_id": "goal-3", "title": "研究对象",
         "markdown": "## 研究对象\n\n"
                     "- **豆包**：豆包、Doubao、豆包AI。字节跳动的 AI 助手。\n"
                     "- **豆包**：豆包、字节豆包。字节跳动推出的免费 AI 对话助手，多模态。\n"
                     "- **Kimi**：Kimi、月之暗面。月之暗面的助手。\n"},
        {"section_id": "ch-6/sec-1", "goal_id": "goal-1", "title": "官方定位",
         "markdown": "## goal-1｜豆包官方产品定位\n\n"
                     "- 此处缺失：goal-1/ch-6/sec-1；原因：conclusion_invalid\n"},
        {"section_id": "ch-6/sec-2", "goal_id": "goal-2", "title": "用户口碑",
         "markdown": '## 证据缺口\n'
                     '- gap={"dimension":"representativeness","status":"insufficient",'
                     '"detail":"样本均来自微博，不能外推。"}\n\n'
                     '## 结论\n\n- 豆包正向口碑集中在内容生产[S52]\n'},
    ],
    "缺失清单": [],
}


def _rendered(view) -> str:
    return "\n".join(str(s.get("markdown") or "") for s in view["sections"])


def test_研究对象节里同名实体合成一行():
    view = parse_report(__import__("json").dumps(WORK, ensure_ascii=False))
    entities = view["sections"][0]["markdown"]
    assert entities.count("- **豆包**") == 1
    # 两行的别名并集都在，说明是合并不是丢弃。
    for alias in ("Doubao", "豆包AI", "字节豆包"):
        assert alias in entities
    # 说明取长的那句。
    assert "多模态" in entities
    assert [e["name"] for e in view["entities"]] == ["豆包", "Kimi"]


def test_缺失节渲染成人话且原因码不出页面():
    view = parse_report(__import__("json").dumps(WORK, ensure_ascii=False))
    section = view["sections"][1]
    assert section["placeholder"] is True
    assert section["missing_text"] == missing_text("conclusion_invalid")
    assert "写手引用了证据池外的来源" in section["markdown"]
    assert "重跑这节" in section["markdown"]
    rendered = _rendered(view)
    assert "conclusion_invalid" not in rendered
    assert "此处缺失" not in rendered


def test_证据缺口由裸_JSON_改成三行人话():
    view = parse_report(__import__("json").dumps(WORK, ensure_ascii=False))
    body = view["sections"][2]["markdown"]
    assert "gap={" not in body
    assert "- 维度：representativeness" in body
    assert "- 状态：证据不足" in body           # 机器状态词也翻
    assert "- 说明：样本均来自微博，不能外推。" in body


def test_假设那行摊成假设与理由两行():
    text = '- 假设={"item":"微博单帖仅作方向性信号","reason":"样本不具备总体代表性"}'
    out = humanize_gap_lines(text)
    assert out.splitlines() == ["- 假设：微博单帖仅作方向性信号", "- 理由：样本不具备总体代表性"]


def test_解析不了的裸_JSON_原样留着不吞内容():
    """摊不平就别乱动——吞掉一行内容比留着难看的 JSON 更糟。"""
    broken = '- gap={"dimension": 坏掉的 JSON'
    assert humanize_gap_lines(broken) == broken
    assert humanize_gap_lines('- gap={"foo":"bar"}') == '- gap={"foo":"bar"}'


def test_认不出的原因码也不许原样漏到页面():
    assert "重跑这节" in missing_text("某个还没见过的码")
    assert "某个还没见过的码" not in missing_text("某个还没见过的码")


def test_没有重名时实体节一个字不动():
    text = "- **豆包**：豆包、Doubao。字节的助手。\n- **Kimi**：Kimi。月之暗面。"
    assert dedupe_entity_lines(text) == text


def test_缺口的reason键也摊成说明():
    """09-04 底料实测的第四种说明键；不收进词表它会印成英文标签「reason：…」。"""
    from app.report.render import humanize_gap_lines

    line = ('- gap={"dimension":"豆包官方定位","status":"missing",'
            '"reason":"可见证据无豆包官网、官方公告或应用市场页"}')
    out = humanize_gap_lines(line)
    assert "reason" not in out and "gap={" not in out
    assert "- 说明：可见证据无豆包官网、官方公告或应用市场页" in out
    assert "- 状态：完全没采到" in out
