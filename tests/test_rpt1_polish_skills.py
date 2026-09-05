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


# 用户 09-05 裁决后，开篇节必须带那句人话把握度；旧夹具缺它是被替换的语义，不是尺子太严。
GOOD = """# 执行摘要

豆包在国内讨论量最大，562 条证据里 296 条来自小红书[S01]。

本报告结论的把握度为低，主要因为绝大多数说法都只有一个来源撑着。

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


# ── 用户 2026-09-05 读稿裁决的四条，尺子侧 ────────────────────────────────
GOOD_V2 = """# 执行摘要

豆包在国内讨论量最大，562 条证据里 296 条来自小红书[S01]。

本报告结论的把握度为低，主要因为绝大多数说法都只有一个来源撑着。

# 关键发现

1. 【A】小红书贡献过半证据但零引用[S01]

## 小红书贡献了 296 条证据，却一条都没被引用

正文解读[S01]。

# 论据与数据

| 平台 | 采集条数 |
| --- | --- |
| xhs | 296 |

来源：各平台采集量与被引量对照

# 建议

1. 补一轮小红书精读[S02]

## 值得进一步验证的方向

1. 豆包的输出深度是否偏浅[S01]

# 附录

信息源：S01。
"""


def _tables_v2(tmp_path: Path, crossref: dict[str, str]) -> Path:
    path = tmp_path / "r-t.polished.consulting.tables.json"
    path.write_text(json.dumps({
        "entities": ["豆包", "Kimi"],
        "sources": [{"mark": m, "title": "帖", "url": "u", "grade": "A", "crossref": v}
                    for m, v in crossref.items()],
        "counts": {"evidence": 562},
        "tables": {"platform_mix": {"rows": [{"平台": "xhs", "采集条数": 296}], "n": 562}},
    }, ensure_ascii=False), encoding="utf-8")
    return path


def _run_v2(tmp_path: Path, markdown: str,
            crossref: dict[str, str] | None = None) -> dict[str, list[str]]:
    crossref = crossref or {"S01": "PASS", "S02": "PASS"}
    md = tmp_path / "r-t.polished.consulting.md"
    md.write_text(markdown, encoding="utf-8")
    work = tmp_path / "work.md"
    work.write_text("".join(f"[{m}]" for m in crossref), encoding="utf-8")
    return check_polished.run(md, _tables_v2(tmp_path, crossref), work)


def test_v2_clean_report_passes_all_eight(tmp_path):
    assert not [name for name, problems in _run_v2(tmp_path, GOOD_V2).items() if problems]


@pytest.mark.parametrize("word", ["topic_polarity", "citation_no", "evidence.platform",
                                  "app/report/polish/lexicon.py"])
def test_ruler_catches_table_and_field_names_in_the_body(tmp_path, word):
    """裁决条 3：读者不需要知道库长什么样——表名、字段名、代码路径一律不许出现。"""
    findings = _run_v2(tmp_path, GOOD_V2.replace("来源：各平台采集量与被引量对照", f"来源：{word}"))
    assert any(word.split("/")[-1] in p for p in findings["① 无内部词"])


def test_ruler_catches_a_judgement_without_its_subject(tmp_path):
    """裁决条 1：首稿那句「输出深度偏浅」读者分不清说的是产品还是这份报告。"""
    findings = _run_v2(tmp_path, GOOD_V2.replace("正文解读[S01]。", "输出深度偏浅[S01]。"))
    assert any("偏浅" in p for p in findings["⑥ 评价句写清说谁"])


def test_ruler_accepts_a_judgement_that_names_the_subject(tmp_path):
    findings = _run_v2(tmp_path, GOOD_V2.replace("正文解读[S01]。", "豆包的输出深度偏浅[S01]。"))
    assert not findings["⑥ 评价句写清说谁"]


@pytest.mark.parametrize("bad, expect", [
    ("多数主张为 SINGLE（253/274）。", "内部口径词 SINGLE"),
    ("", "把握度"),
])
def test_ruler_guards_the_opening_section(tmp_path, bad, expect):
    """裁决条 2：开篇节零方法论术语 + 必须给一句人话把握度。"""
    line = "本报告结论的把握度为低，主要因为绝大多数说法都只有一个来源撑着。"
    markdown = GOOD_V2.replace(line, bad or "")
    assert any(expect in p for p in _run_v2(tmp_path, markdown)["⑦ 摘要口径与把握度"])


def test_ruler_demotes_advice_backed_only_by_single_source_claims(tmp_path):
    """裁决条 4：建议所引角标全是单源孤证就得降级。首稿建议 1、2 正是这样。"""
    findings = _run_v2(tmp_path, GOOD_V2, {"S01": "PASS", "S02": "SINGLE"})
    assert any("单源孤证" in p for p in findings["⑧ 建议门禁"])


def test_ruler_lets_advice_stand_when_a_cited_mark_is_cross_verified(tmp_path):
    findings = _run_v2(tmp_path, GOOD_V2, {"S01": "SINGLE", "S02": "PASS"})
    assert not findings["⑧ 建议门禁"]


def test_downgrade_section_itself_is_exempt_from_the_advice_gate(tmp_path):
    """降级区里本来就是孤证条目，门禁不能再拿它开刀。"""
    findings = _run_v2(tmp_path, GOOD_V2, {"S01": "SINGLE", "S02": "PASS"})
    assert not findings["⑧ 建议门禁"]


# ── 骨架验收：字节数不是判据，节齐不齐才是 ──────────────────────────────
def test_missing_sections_catches_a_truncated_draft():
    """09-05 实测：引擎中途断流，落盘 4 805 B 只写到第一节，光看字节数就是假绿。"""
    from app.report.polish.run import MIN_DRAFT_BYTES, missing_sections

    sections = ["执行摘要", "关键发现", "论据与数据", "建议", "附录"]
    truncated = "# 调研报告：国内大家对豆包的看法\n\n## 执行摘要\n\n" + "正文。" * 900
    assert len(truncated.encode()) > MIN_DRAFT_BYTES     # 字节数够，骗得过旧判据
    assert missing_sections(truncated, sections) == sections


def test_missing_sections_accepts_the_declared_skeleton():
    from app.report.polish.run import missing_sections

    sections = ["执行摘要", "建议"]
    assert missing_sections("# 执行摘要\n\n正文\n\n# 建议\n\n正文\n", sections) == []


def test_a_document_title_that_demotes_the_skeleton_is_rejected():
    """写手加文档标题、把骨架降成二级 —— 判「没写完」，不是判过。"""
    from app.report.polish.run import missing_sections

    demoted = "# 国内大家对豆包的看法\n\n## 执行摘要\n\n正文\n\n## 建议\n\n正文\n"
    assert missing_sections(demoted, ["执行摘要", "建议"]) == ["执行摘要", "建议"]


# ── 一节一个文件：骨架由代码拼，不再靠写手写对 ────────────────────────────
def test_assemble_writes_the_skeleton_itself(tmp_path):
    from app.report.polish.run import assemble, section_paths

    parts = section_paths(tmp_path, "r-t", "consulting", ["执行摘要", "建议"])
    parts[0][1].parent.mkdir(parents=True)
    parts[0][1].write_text("摘要正文[S01]。\n", encoding="utf-8")
    parts[1][1].write_text("建议正文[S01]。\n", encoding="utf-8")
    assert assemble(parts) == "# 执行摘要\n\n摘要正文[S01]。\n\n# 建议\n\n建议正文[S01]。\n"


@pytest.mark.parametrize("written", ["# 执行摘要\n\n正文。", "## 执行摘要\n\n正文。", "执行摘要\n正文。"])
def test_assemble_drops_a_title_the_writer_wrote_anyway(tmp_path, written):
    """提示词说别写标题，写手偶尔还是会写；重复那行去掉，不许出现两个同名一级标题。"""
    from app.report.polish.run import assemble, section_paths

    parts = section_paths(tmp_path, "r-t", "consulting", ["执行摘要"])
    parts[0][1].parent.mkdir(parents=True)
    parts[0][1].write_text(written, encoding="utf-8")
    assembled = assemble(parts)
    assert assembled.count("执行摘要") == 1 and assembled.startswith("# 执行摘要\n\n正文。")


def test_prompt_lists_every_section_file_as_an_absolute_path(tmp_path):
    from app.report.polish.run import build_prompt, section_paths

    skill = get_template("consulting")
    parts = section_paths(tmp_path, "r-t", skill.name, skill.sections)
    data = {"title": "T", "research_question": "q", "objectives": [], "entities": [],
            "counts": {}, "sources": [], "tables": {}}
    prompt = build_prompt(skill, data, "# 工作稿", Path("/tmp/x.md"), (), parts)
    for name, path in parts:
        assert f"「{name}」→ `{path}`" in prompt
    assert "不要写标题行" in prompt
