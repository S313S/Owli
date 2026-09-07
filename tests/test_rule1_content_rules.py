"""§RULE-1 正式稿内容规则第二包（评审第二轮 #2/#7/#8/#9/#10/#11/#12）。

每条规则都先造红再造绿——尺子自己也要验（记忆 `verification-ruler-needs-verifying`）。
本文件零引擎、零采集：只喂离线夹具给尺子与表生成器。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "check_polished", ROOT / "scripts" / "acceptance" / "rpt1" / "check_polished.py")
check_polished = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_polished)

#: 一份过得了全部判据的咨询体稿；每条用例只改它的一处，改出来的红就是那条规则抓的。
GOOD = """# 执行摘要

豆包在国内讨论量最大，562 条采集、33 条进入引用[S01]。

本报告结论的把握度为低，主要因为绝大多数说法都只有一个来源撑着。

# 关键发现

1. 【A】豆包的正向说法集中在内容生产[S01]

## 豆包的正向说法集中在内容生产，19 条里最密的就是这一类

正文解读[S01]。

> 用着还行，写文案比我自己快
> —— 小红书 · 等级 A [S01]

# 论据与数据

| 主题 | 提及条数 |
| --- | --- |
| 价格与付费 | 19 |

来源：主题提及量与极性词命中

# 建议

1. 补一轮小红书精读[S02]

# 附录

样本怎么来的：562 条采集、33 条进入引用。

## 假设与不确定性

本次样本的代表性有限[S01]。
"""


def _tables(tmp_path: Path, **override) -> Path:
    """确定性表夹具。默认这一份不触发任何闸，用例按需覆写某一块。"""
    data = {
        "entities": ["豆包"],
        "sources": [{"mark": "S01", "title": "豆包用着还行", "url": "u", "grade": "A",
                     "crossref": "PASS"},
                    {"mark": "S02", "title": "豆包专业版上线", "url": "u2", "grade": "A",
                     "crossref": "PASS"}],
        "counts": {"evidence": 562, "cited": 33},
        "tables": {"topic_polarity": {"title": "主题提及量与极性词命中",
                                      "rows": [{"主题": "价格与付费", "提及条数": 19}], "n": 562}},
    }
    data.update(override)
    path = tmp_path / "r-t.polished.consulting.tables.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def _run(tmp_path: Path, markdown: str, **override) -> dict[str, list[str]]:
    md = tmp_path / "r-t.polished.consulting.md"
    md.write_text(markdown, encoding="utf-8")
    work = tmp_path / "work.md"
    work.write_text("[S01][S02]", encoding="utf-8")
    return check_polished.run(md, _tables(tmp_path, **override), work)


def test_the_fixture_itself_passes_every_check(tmp_path):
    """底稿先得是绿的，否则后面每条造红都可能是别的闸在响。"""
    assert not [name for name, problems in _run(tmp_path, GOOD).items() if problems]


# ── 货 1（评审 #9）：抓取时间不进正文 ──────────────────────────────────────
@pytest.mark.parametrize("noise, why", [
    ("正文解读[S01]（fetched_at: 2026-09-06T15:25:58+08:00）。", "字段名原样带进正文"),
    ("正文解读[S01]，该条的抓取时间为 2026-09-06。", "中文说法同样禁"),
    ("正文解读[S01]，采于 2026-09-06T15:25。", "只剩 ISO 时间戳也禁"),
])
def test_fetched_at_is_red_in_the_body(tmp_path, noise, why):
    """竞品稿里这串东西出现 38 次，每读一句就要跨过一段机器字符串。"""
    findings = _run(tmp_path, GOOD.replace("正文解读[S01]。", noise))
    assert findings["① 无内部词"], why


def test_the_program_generated_source_list_is_not_judged(tmp_path):
    """抓取时间的合法落点是程序生成的清单那一列——尺子判自己生成的文本是假红。"""
    from app.report.polish.run import sources_table

    listing = sources_table([{"mark": "S01", "grade": "A", "title": "帖", "url": "u",
                              "fetched_at": "2026-09-06T15:25:58+08:00"}])
    assert "| 抓取时间 |" in listing and "2026-09-06 15:25" in listing
    assert "T15:25" not in listing          # 给读者看的是日期时间，不是 ISO 串
    assert not _run(tmp_path, GOOD.rstrip() + "\n\n" + listing)["① 无内部词"]


def test_fetched_at_reaches_the_source_list_from_the_evidence_row(tmp_path):
    """接缝：表生成器把 evidence.fetched_at 挂到 sources 上，清单才填得出这一列。"""
    from app.report.polish.tables import build_tables

    data = build_tables(
        report={"id": "r-t"}, plan={"subjects": ["豆包"]},
        evidence=[{"id": "e1", "citation_no": 1, "platform": "xhs", "grade": "A",
                   "title": "豆包用着还行", "fetched_at": "2026-09-06T15:25:58+08:00"}],
        claims=[], view={"sources": [{"citation_no": 1, "title": "豆包用着还行", "url": "u"}]})
    assert data["sources"][0]["fetched_at"] == "2026-09-06T15:25:58+08:00"


# ── 货 3（评审 #10）：「假设与不确定性」全篇一次，且在附录 ────────────────
def test_a_second_uncertainty_heading_is_red(tmp_path):
    """竞品稿 5 处、舆情简报 6 处，说的都是同一件事——读者读到第三遍开始跳过。"""
    markdown = GOOD.replace("正文解读[S01]。",
                            "正文解读[S01]。\n\n### 假设与不确定性\n\n本次样本单源较多[S01]。")
    assert _run(tmp_path, markdown)["⑫ 假设与不确定性只写一次"]


def test_the_only_uncertainty_heading_must_sit_in_the_appendix(tmp_path):
    """只写一次也不够：写在关键发现里，读者仍要在正文中间读一段免责。"""
    markdown = GOOD.replace("## 假设与不确定性\n\n本次样本的代表性有限[S01]。\n", "") \
                   .replace("正文解读[S01]。",
                            "正文解读[S01]。\n\n### 假设与不确定性\n\n本次样本代表性有限[S01]。")
    problems = _run(tmp_path, markdown)["⑫ 假设与不确定性只写一次"]
    assert problems and "关键发现" in problems[0]


def test_one_uncertainty_heading_in_the_appendix_passes(tmp_path):
    assert not _run(tmp_path, GOOD)["⑫ 假设与不确定性只写一次"]


# ── 货 2（评审 #11）：限定句密度判黄不判红 ────────────────────────────────
def _warn(tmp_path: Path, markdown: str) -> dict[str, list[str]]:
    md = tmp_path / "r-t.polished.consulting.md"
    md.write_text(markdown, encoding="utf-8")
    return check_polished.warnings_of(md)


HEDGY = ("正文解读[S01]。该结论为单源，不能外推；相关口径待核实，"
         "现有证据不足以支撑更强的判断，仍属单源。")


def test_hedge_density_warns_but_never_fails_the_cell(tmp_path):
    """判黄不判红：红一格等于让写手整节重写（实测 60–80 分钟），文风不值当付这个钱。"""
    markdown = GOOD.replace("正文解读[S01]。", HEDGY)
    assert _warn(tmp_path, markdown)["⒜ 限定句密度"]
    assert "⒜ 限定句密度" not in _run(tmp_path, markdown)   # 不掀掉这一格
    assert not [n for n, p in _run(tmp_path, markdown).items() if p]


def test_two_hedges_in_a_section_are_fine(tmp_path):
    """上限是 2 不是 0——该限定的时候还是要限定，别把尺子改成禁止说人话。"""
    markdown = GOOD.replace("正文解读[S01]。", "正文解读[S01]。该结论为单源，不能外推。")
    assert not _warn(tmp_path, markdown)["⒜ 限定句密度"]


# ── 货 4（评审 #8，调度拍乙）：正式稿只出表不出图 ────────────────────────
MERMAID = """```mermaid
xychart-beta
    line [27, 4, 11]
```"""


def test_a_mermaid_chart_is_red(tmp_path):
    """真机截图坐实：这段在页面上整块显示成裸代码，读者一个字读不出来。"""
    problems = _run(tmp_path, GOOD.replace("正文解读[S01]。",
                                           "正文解读[S01]。\n\n" + MERMAID))["⑬ 只出表不出图"]
    assert problems and any("mermaid" in p for p in problems)


def test_any_code_fence_is_red_even_without_mermaid(tmp_path):
    """围栏里除了图表源码没别的东西该进正式稿——只堵 mermaid 会漏掉别的画法。"""
    markdown = GOOD.replace("正文解读[S01]。", "正文解读[S01]。\n\n```\npie 30 70\n```")
    assert _run(tmp_path, markdown)["⑬ 只出表不出图"]


def test_a_markdown_table_is_still_fine(tmp_path):
    """趋势改成表 + 一句结论，是这条规则要的形态，不能顺手把表也判红。"""
    trend = "证据在 8 月见顶后回落[S01]。\n\n| 月份 | 证据条数 |\n| --- | --- |\n| 2026-08 | 19 |"
    assert not _run(tmp_path, GOOD.replace("正文解读[S01]。", trend))["⑬ 只出表不出图"]


# ── 货 5（评审 #7）：原声必须是人说的评价句 ──────────────────────────────
def _quoted(text: str) -> str:
    return GOOD.replace("> 用着还行，写文案比我自己快", f"> {text}")


@pytest.mark.parametrize("text, why", [
    ("豆包用着还行", "与信息源清单里那条源的标题一字不差 = 帖子标题"),
    ("豆包专业版现已上线，立即体验", "产品公告"),
    ("Doubao Seed Character now available on Atlas", "服务商上架广告（评审实测的 S79）"),
])
def test_a_title_or_announcement_is_not_a_quote(tmp_path, text, why):
    assert _run(tmp_path, _quoted(text))["⑭ 原声是人说的话"], why


def test_a_real_user_sentence_passes(tmp_path):
    """别把闸开太大：人说自己怎么看，一个字都不许拦。"""
    assert not _run(tmp_path, _quoted("写文案是真的快，但让它写长的就开始车轱辘话"))[
        "⑭ 原声是人说的话"]


def test_the_attribution_line_is_not_judged(tmp_path):
    """`—— 平台 · 等级 X [S01]` 是出处行，不是引语本身。"""
    assert not _run(tmp_path, GOOD)["⑭ 原声是人说的话"]


def test_the_candidate_filter_and_the_ruler_share_one_predicate(tmp_path):
    """同一个概念两处两个定义，是 09-07 现形过的一种假绿——这里只许有一个判据函数。"""
    from app.report.polish import tables as polish_tables_mod

    assert check_polished.check_quotes_are_speech.__doc__
    assert polish_tables_mod.is_speech_quote("豆包用着还行", ["豆包用着还行"]) is False
    assert polish_tables_mod.is_speech_quote("写文案是真的快", ["豆包用着还行"]) is True


def test_non_speech_candidates_never_reach_the_writer():
    """挡在候选层：摆出来的候选写手就会用，规则拦不住一张摆在眼前的表。"""
    from app.report.polish.tables import _drop_non_speech_quotes

    coding = {"quotes": {"rows": [{"原声": "豆包专业版现已上线"}, {"原声": "写文案是真的快"}],
                         "basis": "口径。"}}
    dropped = _drop_non_speech_quotes(coding, [{"title": "豆包用着还行"}])
    assert dropped == 1
    assert [row["原声"] for row in coding["quotes"]["rows"]] == ["写文案是真的快"]
    assert "已剔除" in coding["quotes"]["basis"]


# ── 货 6（评审 #2）：编码表某格的语义只能用这一格的证据解释 ──────────────
def _coded(rows) -> dict:
    return {"tables": {"attitude_by_topic": {"rows": rows, "n": 19},
                       "topic_polarity": {"title": "主题提及量与极性词命中",
                                          "rows": [{"主题": "价格与付费", "提及条数": 19}],
                                          "n": 562}}}


THIN = _coded([{"主题": "价格与付费", "态度": "负", "条数": 19, "marks": ["S02"]}])
THICK = _coded([{"主题": "价格与付费", "态度": "负", "条数": 3, "marks": ["S01", "S02"]}])


def test_attribution_on_a_cell_with_thin_citation_is_red(tmp_path):
    """19 条里只有 1 条带角标，照那 1 条解释整格，必然编错（实测「嫌豆包太便宜」）。"""
    line = "价格与付费的负向说法并非嫌贵，而是嫌豆包定价太低[S02]。"
    problems = _run(tmp_path, GOOD.replace("正文解读[S01]。", line), **THIN)[
        "⑮ 归因只引该格内的角标"]
    assert problems and "未进引用" in problems[0]


def test_stating_the_count_without_attributing_passes(tmp_path):
    """规则要的是「只写条数」，那这句就必须放行，否则等于禁止写这一格。"""
    line = "价格与付费 19 条编码为负向，其中 1 条进入引用[S02]。"
    assert not _run(tmp_path, GOOD.replace("正文解读[S01]。", line), **THIN)[
        "⑮ 归因只引该格内的角标"]


def test_attribution_citing_a_mark_outside_the_cell_is_red(tmp_path):
    """覆盖够了也不能拿别的格的角标当依据——那是「引错人」的另一种形态。"""
    line = "价格与付费的负向说法其实是在说涨价[S99]。"
    problems = _run(tmp_path, GOOD.replace("正文解读[S01]。", line), **THICK)[
        "⑮ 归因只引该格内的角标"]
    assert problems and "不在这一格" in problems[0]


def test_attribution_citing_a_mark_inside_the_cell_passes(tmp_path):
    line = "价格与付费的负向说法其实是在说涨价[S01]。"
    assert not _run(tmp_path, GOOD.replace("正文解读[S01]。", line), **THICK)[
        "⑮ 归因只引该格内的角标"]


def test_the_gate_is_silent_without_a_coding_table(tmp_path):
    """没有编码表的老稿判不了这件事——尺子不猜（判据落在本轮产出上）。"""
    assert not _run(tmp_path, GOOD.replace("正文解读[S01]。",
                                           "这并非价格问题，而是习惯问题[S02]。"))[
        "⑮ 归因只引该格内的角标"]
