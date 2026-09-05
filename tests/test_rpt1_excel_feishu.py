"""§RPT-1 货 5：Excel `00_正式稿` 与飞书云文档改推正式稿。"""

from __future__ import annotations

import json
from pathlib import Path

from app.export.excel import SHEETS, build_workbook
from app.export.excel_check import EXPECTED_SHEETS
from app.export.feishu import doc_markdown
from app.export.polished_sheet import EMPTY_HINT, SHEET_NAME, add_charts, load_polished, write_sheet

RESEARCH_ID = "r-rpt1x"
POLISHED_MD = """# 执行摘要

豆包讨论量最大，562 条证据里 296 条来自小红书[S01]。

# 关键发现

1. 【A】小红书贡献过半证据却零引用[S01]

# 附录

信息源：S01。
"""
TABLES = {
    "research_question": "国内大家对豆包的看法",
    "counts": {"evidence": 562, "cited": 33, "claims": 274},
    "sources": [{"mark": "S01", "title": "帖", "url": "https://e.com/a", "grade": "A"}],
    "tables": {
        "platform_mix": {"name": "platform_mix", "title": "各平台采集量与被引量对照",
                         "columns": ["平台", "采集条数", "被引条数"],
                         "rows": [{"平台": "xhs", "采集条数": 296, "被引条数": 0, "marks": []},
                                  {"平台": "weibo", "采集条数": 27, "被引条数": 11,
                                   "marks": ["S01"]}],
                         "n": 562, "basis": "按 platform 分组计数。", "coverage": {"总条数": 562}},
        "grade_mix": {"name": "grade_mix", "title": "被引证据的可靠度等级分布",
                      "columns": ["等级", "被引条数"],
                      "rows": [{"等级": "A", "被引条数": 5, "marks": ["S01"]},
                               {"等级": "B", "被引条数": 21, "marks": []}],
                      "n": 33, "basis": "库内生成列。", "coverage": {}},
    },
}


def _seed(tmp_path: Path) -> Path:
    runs = tmp_path / "runs"
    exports = runs / RESEARCH_ID / "exports"
    exports.mkdir(parents=True)
    (exports / f"{RESEARCH_ID}.polished.consulting.md").write_text(POLISHED_MD, encoding="utf-8")
    (exports / f"{RESEARCH_ID}.polished.consulting.tables.json").write_text(
        json.dumps(TABLES, ensure_ascii=False), encoding="utf-8")
    return runs


def test_sheet_list_is_the_same_contract_in_generator_and_checker():
    """sheet 清单是死契约，生成器与校验器必须一字不差，且 `00` 排最前。"""
    assert list(SHEETS) == EXPECTED_SHEETS
    assert EXPECTED_SHEETS[0] == SHEET_NAME


def test_workbook_always_has_the_polished_sheet_even_without_a_polished_report(tmp_path: Path):
    """没整理过正式稿也要有这张 sheet，只写一行说明——清单不能随数据变。"""
    wb = build_workbook({"research_question": "q", "title": "T"},
                        {"title": "T", "conclusions": [], "entities": []}, [], [], None)
    assert wb.sheetnames == EXPECTED_SHEETS
    assert wb[SHEET_NAME].cell(row=2, column=1).value == EMPTY_HINT


def test_polished_sheet_carries_summary_findings_tables_and_three_charts(tmp_path: Path):
    runs = _seed(tmp_path)
    data = load_polished(runs, RESEARCH_ID)
    assert data is not None and data["template"] == "consulting"
    wb = build_workbook({"research_question": "q", "title": "T"},
                        {"title": "T", "conclusions": [], "entities": []}, [], [], data)
    ws = wb[SHEET_NAME]
    text = "\n".join(str(c.value) for row in ws.iter_rows() for c in row if c.value)
    assert "执行摘要" in text and "562 条证据里 296 条来自小红书[S01]" in text
    assert "【A】小红书贡献过半证据却零引用[S01]" in text
    assert "各平台采集量与被引量对照" in text and "n=562" in text
    assert "口径：按 platform 分组计数。" in text
    # 只有 platform_mix 与 grade_mix 有数据，timeline 缺表 → 两张图，不硬凑第三张。
    assert len(ws._charts) == 2
    for chart in ws._charts:
        assert chart.title is None and (chart.width, chart.height) == (24, 11)
        assert chart.dataLabels.showVal and not chart.dataLabels.showSerName


def test_chart_titles_are_action_titles_written_into_cells(tmp_path: Path):
    """行动式标题写单元格（图对象自身留空），带主语+判断+量级。"""
    runs = _seed(tmp_path)
    data = load_polished(runs, RESEARCH_ID)
    wb = build_workbook({"research_question": "q", "title": "T"},
                        {"title": "T", "conclusions": [], "entities": []}, [], [], data)
    text = "\n".join(str(c.value) for row in wb[SHEET_NAME].iter_rows() for c in row if c.value)
    assert "xhs 贡献了最多证据（296 条）" in text
    assert "被引证据以 B 级最多（21 条）" in text


def test_feishu_doc_pushes_the_polished_draft_when_one_exists(tmp_path: Path):
    """有正式稿：云文档首段就是执行摘要，角标降级成 [n]，信息源清单仍在。"""
    runs = _seed(tmp_path)
    data = load_polished(runs, RESEARCH_ID)
    sources = [{"citation_no": 1, "title": "帖", "permalink": "https://e.com/a"}]
    body = doc_markdown({"title": "T", "conclusions": ["工作稿结论[S01]"], "sections": []},
                        sources, data)
    head = body.split("\n\n")[2]
    assert head.startswith("# 执行摘要")
    assert "工作稿结论" not in body and "[S01]" not in body and "\\[1\\]" in body
    # 正文自带「附录 · 信息源」，不许再贴一份重复的清单。
    assert body.count("信息源") == 1 and "https://e.com/a" not in body


def test_feishu_appends_the_source_list_when_the_template_omits_it(tmp_path: Path):
    """模板没写信息源就补一份——出处不能丢。"""
    sources = [{"citation_no": 1, "title": "帖", "permalink": "https://e.com/a"}]
    body = doc_markdown({"title": "T", "conclusions": [], "sections": []}, sources,
                        {"markdown": "# 总体倾向\n\n外面在夸[S01]。\n"})
    assert "## 信息源" in body and "https://e.com/a" in body


def test_feishu_doc_falls_back_to_the_work_draft_without_a_polished_one():
    body = doc_markdown({"title": "T", "conclusions": ["工作稿结论[S01]"], "sections": []},
                        [{"citation_no": 1, "title": "帖", "permalink": "https://e.com/a"}], None)
    assert "## 结论" in body and "工作稿结论" in body
