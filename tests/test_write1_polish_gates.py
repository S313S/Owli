"""§WRITE-1：正文只留一套尺子 + 引语两道程序闸 + 尺子 ⑪ 认否定句。

判据落在**产物与实际入参**上：打 `build_prompt` 的入参看词表数进没进正文，
造一份改过字的引语看闸退不退回。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.report.polish.run import build_prompt, lexicon_reference_table
from app.report.polish.skills import load_templates, shared_rules

LEXICON_TABLE = {
    "name": "topic_polarity", "title": "主题提及量与极性词命中（Top 8）",
    "columns": ["主题", "提及条数", "含正向词", "含负向词", "正负同现"],
    "rows": [{"主题": "价格与付费", "提及条数": 247, "含正向词": 17,
              "含负向词": 3, "正负同现": 1, "marks": ["S22"]}],
    "n": 247, "basis": "固定词表 v1 命中计数，不是情感判断。", "coverage": {},
}
CODING_TABLE = {
    "name": "attitude_by_topic", "title": "UGC 逐条编码：主题 × 态度条数",
    "columns": ["主题", "态度", "条数"],
    "rows": [{"主题": "价格与付费", "态度": "负", "条数": 19, "marks": ["S23"]}],
    "n": 287, "basis": "对 287 条 UGC 逐条模型编码后计数。", "coverage": {},
}


def _data() -> dict:
    return {"title": "t", "research_question": "q", "objectives": [], "entities": ["豆包"],
            "sources": [], "audience_role": "不明", "audience_stake": "",
            "tables": {"topic_polarity": dict(LEXICON_TABLE),
                       "attitude_by_topic": dict(CODING_TABLE)}}


@pytest.mark.parametrize("template", [t.name for t in load_templates()])
def test_lexicon_table_not_declared_by_any_template(template):
    """三份模板的 `tables:` 行都不许再点名词表表——写手看得见它，就会引它的数。"""
    skill = next(t for t in load_templates() if t.name == template)
    assert "topic_polarity" not in skill.tables


@pytest.mark.parametrize("template", [t.name for t in load_templates()])
def test_lexicon_numbers_never_reach_the_prompt(template, tmp_path):
    """判据 1：打 `build_prompt` 的实际入参，词表表的数一个都不在里面。"""
    skill = next(t for t in load_templates() if t.name == template)
    body = build_prompt(skill, _data(), "# 工作稿\n", tmp_path / "x.md")
    assert '"含正向词": 17' not in body
    assert "主题提及量与极性词命中" not in body      # 中文表名也不许露面
    assert "UGC 逐条编码：主题 × 态度条数" in body    # 编码表照旧投给写手


def test_shared_rules_example_names_a_table_the_writer_actually_gets():
    """共用规则里那个「来源：<中文表名>」的例子，不能再举一张写手拿不到的表。"""
    assert "主题提及量与极性词命中" not in shared_rules()


def test_lexicon_reference_table_goes_to_appendix_with_the_caveat():
    """判据 1 后半：附录里有它，且写明「只数触发词、不是情感判断」。"""
    block = lexicon_reference_table(_data()["tables"])
    assert "## 词表命中参考（只数触发词，不是情感判断）" in block
    assert "不是情感判断" in block
    assert "| 价格与付费 | 247 | 17 | 3 | 1 |" in block


def test_lexicon_reference_table_absent_when_no_rows():
    """没有词表命中行就整块不出——空表会被读成「没人谈」。"""
    assert lexicon_reference_table({"topic_polarity": {**LEXICON_TABLE, "rows": []}}) == ""
    assert lexicon_reference_table({}) == ""
