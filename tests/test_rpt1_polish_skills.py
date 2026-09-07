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

豆包在国内讨论量最大，562 条采集、33 条进入引用[S01]。

本报告结论的把握度为低，主要因为绝大多数说法都只有一个来源撑着。

# 关键发现

1. 【A】豆包的正向说法集中在内容生产[S01]

## 豆包的正向说法集中在内容生产，296 条里最密的就是这一类

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
        "counts": {"evidence": 562, "cited": 33},
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
        "## 豆包的正向说法集中在内容生产，296 条里最密的就是这一类", "## 小红书数据分析"))
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
    markdown = GOOD.replace("## 豆包的正向说法集中在内容生产，296 条里最密的就是这一类", f"## {title}")
    assert not _run(tmp_path, markdown)["⑤ 行动式标题"]


def test_ruler_still_catches_a_topic_heading_in_the_body(tmp_path):
    """放宽之后仍要抓得住真正的话题式标题，否则等于把尺子改废了。"""
    markdown = GOOD.replace("## 豆包的正向说法集中在内容生产，296 条里最密的就是这一类", "## 平台情况说明")
    assert any("平台情况说明" in p for p in _run(tmp_path, markdown)["⑤ 行动式标题"])


# ── 用户 2026-09-05 读稿裁决的四条，尺子侧 ────────────────────────────────
GOOD_V2 = """# 执行摘要

豆包在国内讨论量最大，562 条采集、33 条进入引用[S01]。

本报告结论的把握度为低，主要因为绝大多数说法都只有一个来源撑着。

# 关键发现

1. 【A】豆包的正向说法集中在内容生产[S01]

## 豆包的正向说法集中在内容生产，296 条里最密的就是这一类

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
        "counts": {"evidence": 562, "cited": 33},
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


def test_prompt_scopes_the_writer_to_exactly_one_section(tmp_path):
    """适配器每次任务硬墙钟 300 秒，整份五节塞不进去 —— 一轮只写一节。"""
    from app.report.polish.run import build_prompt, section_paths

    skill = get_template("consulting")
    parts = section_paths(tmp_path, "r-t", skill.name, skill.sections)
    data = {"title": "T", "research_question": "q", "objectives": [], "entities": [],
            "counts": {}, "sources": [], "tables": {}}
    name, path = parts[1]
    prompt = build_prompt(skill, data, "# 工作稿", path, (), parts, name)
    assert f"**本轮你只写「{name}」这一节**" in prompt and str(path) in prompt
    assert "别的节这轮不要碰" in prompt and "不要写标题行" in prompt
    # 别的节的路径不许出现，免得写手顺手把整份都写了又超时。
    assert all(str(other) not in prompt for other_name, other in parts if other_name != name)
    assert " / ".join(n for n, _ in parts) in prompt          # 骨架仍给它看，只是不让写


def test_a_crashing_engine_call_costs_one_attempt_not_the_whole_report(tmp_path):
    """SDK 子进程整个崩掉时异常会冲出 adapter；一节崩了只算这一节一次失败。"""
    import asyncio

    from app.report.polish.run import polish, section_paths, sections_for

    skill = get_template("consulting")
    # 这份夹具的 plan_snapshot 是空的 = 读者「不明」，建议节按货 4 ① 改名，
    # 落盘文件名跟着变；期望路径要走 sections_for 算，不能按模板原名写死。
    parts = section_paths(tmp_path / "runs", "r-t", skill.name, sections_for(skill, {}))
    calls = {"n": 0}

    class _Adapter:
        async def run(self, task, ctx, on_event=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("Stream closed")     # 第一次崩
            task.output_path.write_text("正文[S01]。" * 40, encoding="utf-8")
            return type("R", (), {"succeeded": True, "engine_error": None})()

    class _Store:
        def get_report(self, rid):
            return {"id": rid, "title": "T", "research_question": "q", "plan_snapshot": {},
                    "extra": {"claims": []}}

        def list_evidence(self, rid):
            return [{"id": "ev-1", "platform": "xhs", "kind": "post", "citation_no": 1,
                     "title": "帖", "content_excerpt": "豆包好用", "grade": "A",
                     "published_at": None, "extra": "{}"}]

    work = "# 工作稿\n\n## 信息源\n\n- [S01] [帖](https://e.com/a)\n"
    outcome = asyncio.run(polish(_Store(), "r-t", tmp_path / "runs", work,
                                 template="consulting", adapter=_Adapter()))
    assert outcome["status"] == "ok"
    # 第一次崩掉只赔了一次尝试，五节全部写出来了。
    assert calls["n"] == len(skill.sections) + 1
    assert all(path.is_file() for _, path in parts)


def test_polish_widens_only_its_own_wall_clock_and_touches_no_adapter_file():
    """撰写墙钟走 ClaudeAdapter 的公开构造参数放宽，禁区里一个文件都不动。"""
    from app.adapters.claude import DEFAULT_CLAUDE_TIMEOUT_SECONDS
    from app.report.polish.run import SECTION_TIMEOUT_SECONDS, default_adapter

    assert DEFAULT_CLAUDE_TIMEOUT_SECONDS == 300.0        # 全局默认没被改
    assert SECTION_TIMEOUT_SECONDS >= 480                 # 提货单 §3.4 估的是 3–8 分钟
    assert default_adapter().timeout_seconds == SECTION_TIMEOUT_SECONDS


# ── 信息源清单由代码生成 ──────────────────────────────────────────────────
def test_sources_table_is_generated_from_the_pool_not_retyped():
    """三格里两格死在「附录」誊抄几十条链接（socket closed）；清单改由代码出。"""
    from app.report.polish.run import sources_table

    table = sources_table([
        {"mark": "S01", "grade": "A", "title": "帖 | 带竖线", "url": "https://e.com/a"},
        {"mark": "S02", "grade": None, "title": "", "url": "https://e.com/b"},
    ])
    assert "| S01 | A | 可独立支撑结论 | 帖 ｜ 带竖线 | https://e.com/a |" in table
    assert "| S02 | ? | 未评级 | （无标题） | https://e.com/b |" in table
    assert table.count("\n|") >= 3          # 表头 + 分隔 + 两行


def test_assemble_appends_the_source_table_to_the_last_section(tmp_path):
    from app.report.polish.run import assemble, section_paths

    parts = section_paths(tmp_path, "r-t", "consulting", ["执行摘要", "附录"])
    parts[0][1].parent.mkdir(parents=True)
    parts[0][1].write_text("摘要[S01]。", encoding="utf-8")
    parts[1][1].write_text("方法与样本说明。", encoding="utf-8")
    out = assemble(parts, [{"mark": "S01", "grade": "B", "title": "帖", "url": "u"}])
    assert out.index("## 信息源清单") > out.index("# 附录")      # 挂在末节里
    assert "摘要[S01]。" in out and "方法与样本说明。" in out


def test_assemble_without_sources_is_unchanged(tmp_path):
    from app.report.polish.run import assemble, section_paths

    parts = section_paths(tmp_path, "r-t", "consulting", ["附录"])
    parts[0][1].parent.mkdir(parents=True)
    parts[0][1].write_text("正文。", encoding="utf-8")
    assert assemble(parts) == "# 附录\n\n正文。\n"


# ── ⑤ 改成正面认话题式标题；⑧ 按「条」而非按行聚合 ──────────────────────
@pytest.mark.parametrize("title", [
    '豆包的口碑呈"能力被认可、交互与深度被吐槽"的双面结构',
    "产品定位：Kimi 押「技术学霸」、豆包押「字节入口」",
    "小红书贡献了 296 条证据，却一条都没被引用",
    "一、豆包的负向印象聚焦在交付质检，不在能力本身",
])
def test_ruler_accepts_real_conclusion_titles(tmp_path, title):
    """靠「有没有程度词」反着判，连着冤枉了五个真结论句——改成正面认话题式标题。"""
    markdown = GOOD_V2.replace("## 豆包的正向说法集中在内容生产，296 条里最密的就是这一类", f"## {title}")
    assert not _run_v2(tmp_path, markdown)["⑤ 行动式标题"]


@pytest.mark.parametrize("title", ["平台情况说明", "小红书数据分析", "二、竞品对比", "样本概况"])
def test_ruler_still_catches_topic_style_titles(tmp_path, title):
    markdown = GOOD_V2.replace("## 豆包的正向说法集中在内容生产，296 条里最密的就是这一类", f"## {title}")
    assert any(title in p for p in _run_v2(tmp_path, markdown)["⑤ 行动式标题"])


def test_advice_gate_reads_the_whole_entry_not_one_line(tmp_path):
    """一条建议横跨「建议行 + 依据行」，角标分散两行；依据行单独看会被误判成全孤证。"""
    advice = ("1. **补一轮小红书精读**[S02]\n"
              "   依据：回指第 1 条发现[S01]；把握度：中。")
    markdown = GOOD_V2.replace("1. 补一轮小红书精读[S02]", advice)
    findings = _run_v2(tmp_path, markdown, {"S01": "PASS", "S02": "SINGLE"})
    assert not findings["⑧ 建议门禁"]          # 整条里有 PASS，就不该判红


def test_advice_gate_still_fires_when_the_whole_entry_is_single_source(tmp_path):
    advice = ("1. **补一轮小红书精读**[S02]\n"
              "   依据：回指第 1 条发现[S02]；把握度：低。")
    markdown = GOOD_V2.replace("1. 补一轮小红书精读[S02]", advice)
    findings = _run_v2(tmp_path, markdown, {"S01": "PASS", "S02": "SINGLE"})
    assert any("单源孤证" in p for p in findings["⑧ 建议门禁"])


# ── 回退 Codex 时不许带 Claude 的模型名 ────────────────────────────────────
def test_codex_shim_strips_the_claude_model_name():
    """Claude 撞限额回退 Codex 时，opus 被原样传过去导致 400，九格废了五格。"""
    import asyncio

    from app.adapters.contracts import EngineTask
    from app.report.polish.run import _CodexModelShim

    seen = {}

    class _Inner:
        timeout_seconds = 123.0

        async def run(self, task, ctx, on_event=None):
            seen["model"] = task.model
            seen["body"] = task.body
            return "ok"

    shim = _CodexModelShim(_Inner())
    assert shim.timeout_seconds == 123.0          # 其余属性透传
    task = EngineTask(body="正文", output_path=Path("/tmp/x.md"), output_format="markdown",
                      research_id="r-t", goal_id="polished", agent_id="a", agent_kind="k",
                      validators=[], capability=None, model="opus")
    assert asyncio.run(shim.run(task, None)) == "ok"
    assert seen["model"] is None and seen["body"] == "正文"   # 只摘模型名，别的不动


def test_claude_side_keeps_its_model_name():
    """壳只套在 Codex 上；Claude 那条腿仍然拿得到 opus。"""
    from app.report.polish.run import default_adapter

    adapter = default_adapter()
    assert adapter._adapters["claude"].timeout_seconds == 1800.0
    assert type(adapter._adapters["codex"]).__name__ == "_CodexModelShim"


# ── ④ 的老大难：写手把两行的数相加 ────────────────────────────────────────
def test_shared_rules_spell_out_the_no_arithmetic_counter_examples():
    """九格里三格都栽在加总；规则只说「不许算」不够，得把反例摆出来。"""
    rules = shared_rules()
    assert "表里有的数只能原样引一行，两行的数不许合并" in rules
    for bad in ("259", "70%", "21/40"):
        assert bad in rules, f"缺少反例 {bad}"
    assert "D 级 257 条、未评级 2 条" in rules          # 正确写法也要给


def test_ruler_catches_a_number_summed_from_two_table_rows(tmp_path):
    """257 与 2 都在表里，259 不在——加总出来的数没出处，判红。"""
    tables = tmp_path / "r-t.polished.consulting.tables.json"
    tables.write_text(json.dumps({
        "entities": ["豆包"], "counts": {},
        "sources": [{"mark": "S01", "title": "帖", "url": "u", "grade": "A",
                     "crossref": "PASS"}],
        "tables": {"grade_mix": {"rows": [{"等级": "D", "全库条数": 257},
                                          {"等级": "?", "全库条数": 2}]},
                   # GOOD_V2 正文本来就提到 296 / 562，夹具要带上，否则 ④ 会被它们干扰
                   "platform_mix": {"rows": [{"平台": "xhs", "采集条数": 296}], "n": 562}},
    }, ensure_ascii=False), encoding="utf-8")
    md = tmp_path / "r-t.polished.consulting.md"
    md.write_text(GOOD_V2.replace("正文解读[S01]。", "未被引证据合计 259 条。"), encoding="utf-8")
    work = tmp_path / "work.md"
    work.write_text("[S01]", encoding="utf-8")
    findings = check_polished.run(md, tables, work)
    assert any("259" in p for p in findings["④ 数字有出处"])
    # 分开各引一行就该放行
    md.write_text(GOOD_V2.replace("正文解读[S01]。", "D 级 257 条、未评级 2 条。"), encoding="utf-8")
    assert not check_polished.run(md, tables, work)["④ 数字有出处"]


@pytest.mark.parametrize("sentence, why", [
    ("现有材料适合启动诊断，不足以直接支持全面改版[S01]。", "情态：不足以+动词"),
    ("这批材料同时包含相反评价，交叉印证偏弱[S01]。", "主语是「材料」"),
    ("对长期口碑演变的支撑有限[S01]。", "主语是「支撑」"),
    ("读者不能把空白格解释为产品能力较差[S01]。", "否定/劝诫用法"),
])
def test_ruler_lets_these_judgement_shapes_through(tmp_path, sentence, why):
    """⑥ 09-06 九格误报的四种句式（依据见 check_polished.py 常量注释里的原文出处）。"""
    findings = _run_v2(tmp_path, GOOD_V2.replace("正文解读[S01]。", sentence))
    assert not findings["⑥ 评价句写清说谁"], why


def test_ruler_skips_a_line_that_is_only_a_link(tmp_path):
    """一整行「来源链接：https://…」不是判断句，⑥ 不该拿它开刀。"""
    findings = _run_v2(
        tmp_path,
        GOOD_V2.replace("正文解读[S01]。",
                        "正文解读[S01]。\n\n来源链接：https://m.weibo.cn/detail/5338769299341618"))
    assert not findings["⑥ 评价句写清说谁"]


@pytest.mark.parametrize("line, why", [
    ("无法补写符合 ISO 8601 的抓取时间。", "标准编号整体跳过"),
    ("来源链接：https://m.weibo.cn/detail/5338737804574917", "数字不从链接里取"),
    ("```mermaid\nxychart-beta\n    y-axis \"证据条数\" 0 --> 80\n```", "围栏里是图表源码"),
])
def test_ruler_does_not_read_numbers_out_of_these(tmp_path, line, why):
    """④ 09-06 九格误报的三处来源（依据见 check_polished.py 常量注释里的原文出处）。"""
    findings = _run_v2(tmp_path, GOOD_V2.replace("正文解读[S01]。", f"正文解读[S01]。\n\n{line}"))
    assert not findings["④ 数字有出处"], why


def test_ruler_still_catches_a_naked_number_in_prose(tmp_path):
    """放行三处之后，正文里凭空冒出来的数字仍要判红。"""
    findings = _run_v2(tmp_path, GOOD_V2.replace("正文解读[S01]。", "有 947 条材料提到这一点。"))
    assert any("947" in p for p in findings["④ 数字有出处"])


@pytest.mark.parametrize("section, heading", [
    ("对不同读者的含义", "投资分析"),     # §RPT-2 货 4① 规定的三行之一，实测命中 TOPIC_TITLE
    ("对提问方意味着什么", "竞品对比"),   # §RPT-2 货 4② 的竞品收尾节，同族措辞也会命中
])
def test_rpt2_structural_sections_are_exempt_from_action_titles(tmp_path, section, heading):
    """⑤ 不查模板规定措辞的结构节：这两节的小标题是 SKILL 规定的，不是写手在起标题。"""
    findings = _run_v2(tmp_path,
                       GOOD_V2.replace("# 附录", f"# {section}\n\n## {heading}\n\n正文[S01]。\n\n# 附录"))
    assert not findings["⑤ 行动式标题"]


def test_action_title_gate_still_fires_outside_structural_sections(tmp_path):
    """豁免只针对结构节：正文节里的话题式标题照抓，别把放宽做成拆闸。"""
    findings = _run_v2(tmp_path, GOOD_V2.replace(
        "## 小红书贡献了 296 条证据，却一条都没被引用", "## 用户评价分析"))
    assert any("用户评价分析" in p for p in findings["⑤ 行动式标题"])


# —— 九格续跑账本：串行 5–6 h，中途机器重启要能接着跑而不是从头来 ——

def _matrix():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "rpt1_matrix",
        Path(__file__).resolve().parents[1] / "scripts/acceptance/rpt1/rpt1_matrix.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("write, revision, expect", [
    ({"revision": "abc", "cells": {"g": {"passed": True}}}, "abc", 1),   # 版本对上才复用
    ({"revision": "abc", "cells": {"g": {"passed": True}}}, "xyz", 0),   # 代码变了整本作废
    ({"revision": "abc", "cells": {"g": "不是字典"}}, "abc", 0),          # 脏行不当数
])
def test_progress_book_is_only_reused_under_the_same_code(tmp_path, write, revision, expect):
    """账本记的是「哪个 git HEAD 下哪一格过了」——尺子或提示词一改，旧的绿一律不认。"""
    matrix = _matrix()
    path = tmp_path / "book.json"
    path.write_text(json.dumps(write, ensure_ascii=False), encoding="utf-8")
    assert len(matrix._load_progress(path, revision)) == expect


def test_a_broken_or_missing_book_reruns_everything_instead_of_crashing(tmp_path):
    """账本坏了要宁可多跑，不能崩、更不能当成「全过了」。"""
    matrix = _matrix()
    broken = tmp_path / "broken.json"
    broken.write_text("{ 这不是 json", encoding="utf-8")
    assert matrix._load_progress(broken, "abc") == {}
    assert matrix._load_progress(tmp_path / "nope.json", "abc") == {}


def test_progress_is_written_atomically(tmp_path):
    """每格落一次账；先写临时文件再改名，跑到一半被杀不会留半个账本。"""
    matrix = _matrix()
    path = tmp_path / "sub" / "book.json"
    matrix._save_progress(path, "abc", {"g": {"passed": False}})
    assert json.loads(path.read_text(encoding="utf-8"))["revision"] == "abc"
    assert not list(path.parent.glob("*.tmp"))


def test_a_single_cell_run_updates_the_book_instead_of_replacing_it(tmp_path):
    """`--only 某一格` 不能把整本账本覆盖成只剩那一格——否则下次 --resume
    会把本来已过的八格又跑一遍（≈5 h）。账本总是先读进来再往上加。"""
    matrix = _matrix()
    path = tmp_path / "book.json"
    matrix._save_progress(path, "abc", {f"g{i}": {"passed": True} for i in range(8)})
    book = matrix._load_progress(path, "abc")          # 单格那轮开跑时也要先读
    book["g8"] = {"passed": False}                     # 只加自己这一格
    matrix._save_progress(path, "abc", book)
    assert len(matrix._load_progress(path, "abc")) == 9


@pytest.mark.parametrize("cell, skippable", [
    ({"passed": True, "skipped": False}, True),    # 本轮写出来且过了尺子 → 才算数
    ({"passed": True, "skipped": True}, False),    # 旧稿被重新压了遍尺子 → 不算
    ({"passed": False, "skipped": False}, False),  # 本轮写了但没过 → 重写
])
def test_resume_only_credits_a_cell_this_round_actually_wrote(cell, skippable):
    """`--resume` 的跳过条件是 `passed and not skipped`，两个都要。

    只看 passed 会出事：默认路径（零成本复验尺子）也会给上一轮的旧稿判 PASS 并
    记进账本（skipped=True），于是最后一轮把那几格直接跳掉，交出没有质量补丁的
    旧稿，而读数还是 9/9 全绿。这一条同时挡住「md 是旧的、tables.json 是新的」
    那种格——旧稿永远不被当成本轮成果。
    """
    assert bool(cell.get("passed") and not cell.get("skipped")) is skippable


# —— §RPT-2 货 2：管道自诊出正文、摘要样本数口径。尺子自己也要验，先造红再造绿。——

@pytest.mark.parametrize("line", [
    "小红书 296 条采集里 0 条被引[S01]。",
    "本报告的被引证据只有 33 条[S01]。",
    "| 平台 | 采集条数 | 被引条数 |",
    "n=562 · 口径：按平台分组的采集量对照 · 来源：各平台采集量[S01]",
])
def test_闸9_管道自诊出现在主体节判红(tmp_path, line):
    findings = _run(tmp_path, GOOD.replace("正文解读[S01]。", line))
    assert findings["⑨ 管道自诊不占主体节"]


def test_闸9_同样的话放进附录不判红(tmp_path):
    """诚实感该待的地方：附录与开篇那句把握度，不是关键发现的头条。"""
    findings = _run(tmp_path, GOOD.replace(
        "信息源：S01。", "信息源：S01。小红书 296 条采集里 0 条被引，样本结构偏。"))
    assert not findings["⑨ 管道自诊不占主体节"]


def test_闸9_论据与数据节里的管道表不判红(tmp_path):
    """把管道表集中列在「论据与数据」是模板允许的；⑨ 管的是主体节的主表。"""
    assert not _run(tmp_path, GOOD)["⑨ 管道自诊不占主体节"]


def test_闸10_摘要单写采集总数判红(tmp_path):
    findings = _run(tmp_path, GOOD.replace(
        "562 条采集、33 条进入引用[S01]。", "本次调研的 562 条证据显示豆包口碑分化[S01]。"))
    assert findings["⑩ 摘要样本数口径"]


@pytest.mark.parametrize("sentence", [
    "562 条采集、33 条进入引用[S01]。",
    "支撑本报告结论的是 33 条被引证据[S01]。",
])
def test_闸10_两种合法写法都不判红(tmp_path, sentence):
    findings = _run(tmp_path, GOOD.replace("562 条采集、33 条进入引用[S01]。", sentence))
    assert not findings["⑩ 摘要样本数口径"]


def test_闸10_采集总数出现在正文别处不判红(tmp_path):
    """⑩ 只管开篇节——论据与数据里那张表的 n=562 是它该待的地方。"""
    findings = _run(tmp_path, GOOD.replace("| xhs | 296 |", "| xhs | 296 |\n\nn=562 条[S01]"))
    assert not findings["⑩ 摘要样本数口径"]


# —— §RPT-2 货 4 ①：读者不明时建议节改名，尺子跟着认别名 ——————————————

def _skill_and(role: str):
    from app.report.polish.run import sections_for
    from app.report.polish.skills import get_template

    return sections_for(get_template("consulting"), {"audience": {"audience_role": role}})


def test_读者明确时建议节不改名():
    assert "建议" in _skill_and("竞品团队")
    assert "对不同读者的含义" not in _skill_and("竞品团队")


@pytest.mark.parametrize("data", [{}, {"audience": {}}, {"audience": {"audience_role": "不明"}}])
def test_读者不明时建议节改名成对不同读者的含义(data):
    """读者是谁没答（或答了「不明」），这一节就不是行动清单，是「你是谁决定了它的含义」。"""
    from app.report.polish.run import sections_for
    from app.report.polish.skills import get_template

    names = sections_for(get_template("consulting"), data)
    assert "对不同读者的含义" in names and "建议" not in names
    # 只换这一个名字，其余骨架一字不动
    assert len(names) == len(get_template("consulting").sections)


def test_尺子认改名后的建议节不误报缺标题(tmp_path):
    """节名改了尺子不跟着改，等于拿旧尺子量新稿——判据 ② 会误报「缺『建议』」。"""
    findings = _run_v2(tmp_path, GOOD_V2.replace("# 建议", "# 对不同读者的含义"))
    assert not [name for name, problems in findings.items() if problems]


def test_建议门禁在改名后的节上照样开火(tmp_path):
    """改名不能变成绕开门禁的后门：孤证撑着的那条照样判红。"""
    findings = _run_v2(tmp_path, GOOD_V2.replace("# 建议", "# 对不同读者的含义"),
                       {"S01": "PASS", "S02": "SINGLE"})
    assert any("单源孤证" in p for p in findings["⑧ 建议门禁"])


# —— §RPT-2 货 4 ②：竞品对比稿收尾「对提问方意味着什么」 ——————————————

def test_竞品对比稿有对提问方意味着什么这一节():
    """借 competitor-profiling 的 Competitive Implications：谁强在哪之后要落到「所以对你」。"""
    names = list(get_template("competitor-matrix").sections)
    assert "对提问方意味着什么" in names
    # 位置：先摆事实（谁强在哪），再说含义，最后才给动作（建议）
    assert names.index("谁强在哪") < names.index("对提问方意味着什么") < names.index("建议")


def test_对比稿读者不明时建议节不改名():
    """它已经有一节专写含义，建议节再改名就成了两节讲同一件事。"""
    from app.report.polish.run import sections_for

    skill = get_template("competitor-matrix")
    names = sections_for(skill, {})
    assert "建议" in names and "对不同读者的含义" not in names
    assert names == tuple(skill.sections)


def test_建议门禁两节都过一遍(tmp_path):
    """含义节写的也是「凭这些证据你该怎么看」，孤证撑着照样要降级。

    门禁按模板声明的节找节，所以这条必须用竞品对比稿的夹具——
    咨询体不声明这一节，往它稿子里插一节门禁根本看不见。
    """
    crossref = {"S01": "PASS", "S02": "SINGLE"}
    tables = _tables_v2(tmp_path, crossref)
    matrix_tables = tables.with_name("r-t.polished.competitor-matrix.tables.json")
    matrix_tables.write_text(tables.read_text(encoding="utf-8"), encoding="utf-8")
    md = tmp_path / "r-t.polished.competitor-matrix.md"
    md.write_text(GOOD_V2.replace(
        "# 建议",
        "# 对提问方意味着什么\n\n- **对竞品团队**：这块口碑最集中[S02]\n\n# 建议"),
        encoding="utf-8")
    work = tmp_path / "work.md"
    work.write_text("".join(f"[{m}]" for m in crossref), encoding="utf-8")
    problems = check_polished.run(md, matrix_tables, work)["⑧ 建议门禁"]
    assert sum("单源孤证" in p for p in problems) == 2


# —— 守卫：谁再改建议节的名字，这里当场打红 ————————————————————————

@pytest.mark.parametrize("template", [t.name for t in __import__(
    "app.report.polish.skills", fromlist=["load_templates"]).load_templates()])
@pytest.mark.parametrize("audience", [{}, {"audience": {"audience_role": "竞品团队"}}])
def test_每个模板都有一节落在建议门禁的辖区内(template, audience):
    """⑧ 建议门禁按节名硬匹配 `ADVICE_SECTIONS` 找节，找不到就返回空——**不报错**。

    §RPT-2 货 4 ① 给「建议」加了改名分支，改名而尺子不跟着认，后果是六处
    「只有单源孤证撑着」的真抓一处不剩、读数还是绿的（在 main 的尺子上实测如此）。
    这条守卫锁的就是那个静默失灵：任何模板、任何读者身份下，成稿的一级标题里
    必须至少有一节在门禁辖区内。将来谁再改节名，红在这里，不用等实跑。
    """
    from app.report.polish.run import sections_for
    from app.report.polish.skills import get_template

    names = sections_for(get_template(template), audience)
    covered = [n for n in names if n in check_polished.ADVICE_SECTIONS]
    assert covered, f"模板 {template} 在 {audience or '读者不明'} 下没有一节归 ⑧ 管：{names}"


def test_门禁辖区词表跟着改名分支走():
    """改名产生的两个节名都必须在辖区内——漏一个就是漏一种读者身份下的稿。"""
    from app.report.polish.run import (ADVICE_SECTION, ADVICE_SECTION_UNKNOWN_AUDIENCE,
                                       IMPLICATIONS_SECTION)

    for name in (ADVICE_SECTION, ADVICE_SECTION_UNKNOWN_AUDIENCE, IMPLICATIONS_SECTION):
        assert name in check_polished.ADVICE_SECTIONS, name
