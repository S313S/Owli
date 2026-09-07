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
