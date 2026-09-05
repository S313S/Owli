"""§RPT-1 货 2：模板加载器与尺子。尺子自己也要验——先造红再造绿。"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from app.report.polish.run import build_prompt, offpool_marks
from app.report.polish.skills import DEFAULT_TEMPLATE, get_template, load_templates, shared_rules
from app.report.polish.tables import TABLE_NAMES

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "check_polished", ROOT / "scripts" / "acceptance" / "rpt1" / "check_polished.py")
check_polished = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_polished)


def test_three_templates_load_with_default_first():
    templates = load_templates()
    assert [t.name for t in templates][0] == DEFAULT_TEMPLATE
    assert {t.name for t in templates} == {"consulting", "sentiment-brief", "competitor-matrix"}
    for template in templates:
        assert template.sections and template.body.strip()
        assert set(template.tables) <= set(TABLE_NAMES)
        assert template.model == "opus"


def test_unknown_template_name_raises():
    with pytest.raises(KeyError):
        get_template("no-such-template")


def test_skill_declaring_an_unknown_table_is_rejected(tmp_path):
    """代码只认 frontmatter 六个字段，其中 tables 必须是真表——写错要当场炸，不静默。"""
    skill = tmp_path / "bogus" / "SKILL.md"
    skill.parent.mkdir()
    skill.write_text("---\nname: bogus\ntitle: T\ntables: [no_such_table]\nsections: [A]\n"
                     "---\n正文\n", encoding="utf-8")
    load_templates.cache_clear()
    with pytest.raises(ValueError, match="不存在的表"):
        load_templates(str(tmp_path))
    load_templates.cache_clear()


def test_prompt_carries_rules_skeleton_pool_and_tables():
    data = {"title": "T", "research_question": "国内大家对豆包的看法", "objectives": [],
            "entities": ["豆包"], "counts": {}, "sources": [{"mark": "S01", "title": "帖",
                                                          "url": "u", "grade": "A"}],
            "tables": {"platform_mix": {"name": "platform_mix", "rows": [], "n": 1}}}
    prompt = build_prompt(get_template("consulting"), data, "# 工作稿\n\n正文[S01]",
                          Path("/tmp/x.md"))
    assert "正式稿硬规则" in prompt and "调研报告（咨询体）" in prompt
    assert "S01｜A 级" in prompt and "platform_mix" in prompt
    assert "国内大家对豆包的看法" in prompt and "工作稿正文全文" in prompt
    assert "上一轮被打回" not in prompt


GOOD = """# 执行摘要

豆包在国内讨论量最大，562 条证据里 296 条来自小红书[S01]。

# 关键发现

1. 【A】小红书贡献过半证据但零引用[S01]

## 小红书贡献了 296 条证据，却一条都没被引用

正文解读[S01]。

# 论据与数据

| 平台 | 采集条数 |
| --- | --- |
| xhs | 296 |

# 建议

1. 补一轮小红书精读[S01]

# 附录

信息源：S01。
"""


def _tables_file(tmp_path: Path, marks=("S01",)) -> Path:
    path = tmp_path / "r-t.polished.consulting.tables.json"
    path.write_text(json.dumps({
        "sources": [{"mark": m, "title": "帖", "url": "u", "grade": "A"} for m in marks],
        "counts": {"evidence": 562},
        "tables": {"platform_mix": {"rows": [{"平台": "xhs", "采集条数": 296}], "n": 562}},
    }, ensure_ascii=False), encoding="utf-8")
    return path


def _run(tmp_path: Path, markdown: str, marks=("S01",)) -> dict[str, list[str]]:
    md = tmp_path / "r-t.polished.consulting.md"
    md.write_text(markdown, encoding="utf-8")
    work = tmp_path / "work.md"
    work.write_text("".join(f"[{m}]" for m in marks), encoding="utf-8")
    return check_polished.run(md, _tables_file(tmp_path, marks), work)


def test_ruler_passes_a_clean_report(tmp_path):
    assert not [name for name, problems in _run(tmp_path, GOOD).items() if problems]


@pytest.mark.parametrize("mutation, expected", [
    ("goal-3 的证据显示……", "① 无内部词"),
    ("本片样本不足。", "① 无内部词"),
])
def test_ruler_catches_internal_words(tmp_path, mutation, expected):
    findings = _run(tmp_path, GOOD.replace("正文解读[S01]。", mutation + "[S01]"))
    assert findings[expected] and not findings["③ 角标不越池"]


def test_ruler_catches_missing_section(tmp_path):
    findings = _run(tmp_path, GOOD.replace("# 建议", "# 行动项"))
    assert any("建议" in p for p in findings["② 一级标题齐"])


def test_ruler_catches_offpool_mark(tmp_path):
    findings = _run(tmp_path, GOOD.replace("正文解读[S01]。", "正文解读[S99]。"))
    assert any("S99" in p for p in findings["③ 角标不越池"])


def test_ruler_catches_invented_number(tmp_path):
    """写手自己算出来的数：表里没有、同句也没角标。"""
    findings = _run(tmp_path, GOOD.replace("正文解读[S01]。", "占比达到 87 个百分点。"))
    assert any("87" in p for p in findings["④ 数字有出处"])


def test_ruler_catches_topic_style_heading(tmp_path):
    findings = _run(tmp_path, GOOD.replace(
        "## 小红书贡献了 296 条证据，却一条都没被引用", "## 小红书数据分析"))
    assert any("小红书数据分析" in p for p in findings["⑤ 行动式标题"])


def test_ruler_does_not_demand_action_titles_inside_structural_sections(tmp_path):
    """附录/建议底下的小标题措辞是模板自己规定的，尺子不许拿行动式标题去要求它们。

    09-05 首稿实测：尺子把「一、样本怎么来的」「值得进一步验证的方向」判成红，
    是尺子越界不是稿有问题。
    """
    markdown = GOOD.replace("# 附录\n\n信息源：S01。",
                            "# 附录\n\n## 一、样本怎么来的\n\n说明。\n\n## 四、信息源清单\n\nS01。")
    markdown = markdown.replace("1. 补一轮小红书精读[S01]",
                                "## 值得进一步验证的方向\n\n1. 补一轮小红书精读[S01]")
    assert not _run(tmp_path, markdown)["⑤ 行动式标题"]


@pytest.mark.parametrize("title", [
    "豆包的负向印象聚焦在交付质检，不在AI能力本身",
    "豆包正被字节统一为AI办公场景的入口",
    "DeepSeek在同期证据里形成清晰的技术派对照",
])
def test_ruler_accepts_contrast_style_action_titles(tmp_path, title):
    """「A 在 X 不在 Y」这类对比句是最典型的行动式标题，早先的词表漏收了它们。"""
    markdown = GOOD.replace("## 小红书贡献了 296 条证据，却一条都没被引用", f"## {title}")
    assert not _run(tmp_path, markdown)["⑤ 行动式标题"]


def test_ruler_still_catches_a_topic_heading_in_the_body(tmp_path):
    """放宽之后仍要抓得住真正的话题式标题，否则等于把尺子改废了。"""
    markdown = GOOD.replace("## 小红书贡献了 296 条证据，却一条都没被引用", "## 平台情况说明")
    assert any("平台情况说明" in p for p in _run(tmp_path, markdown)["⑤ 行动式标题"])
