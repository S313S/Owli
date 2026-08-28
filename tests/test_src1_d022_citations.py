"""§SRC-1 货 7（D-022）：JSON 成稿的角标回填，以及「解析到 0 不许清空」。"""

from __future__ import annotations

import json

import pytest

_MD = (
    "## 结论\n\n- 「通义听悟」口碑集中在免费与会议纪要[S01][S02]。\n\n"
    "## 信息源\n\n"
    "- [S01] [知乎长文](https://www.zhihu.com/tardis/jm/art/2055594870052501337)\n"
    "- [S02] [36 氪笔记](https://ai.36kr.com/note-detail/3568010593718002)\n"
)


def _json_report(sections: list[dict]) -> str:
    return json.dumps(
        {"title": "报告", "chapter_id": "ch-4", "sections": sections},
        ensure_ascii=False,
    )


def test_JSON成稿也能读出角标() -> None:
    """诊断根因：解析器逐行扫 Markdown 标题，JSON 把正文塞在转义字符串里，
    整份文件没有一行是标题，于是解析出 0 个 —— 全库 citation_no 被清成 NULL。"""

    from app.report.markdown import report_citations

    citations = report_citations(_json_report([
        {"section_id": "ch-4/sec-1", "markdown": _MD},
    ]))

    assert citations == {
        "https://www.zhihu.com/tardis/jm/art/2055594870052501337": 1,
        "https://ai.36kr.com/note-detail/3568010593718002": 2,
    }


def test_Markdown成稿的老行为不变() -> None:
    from app.report.markdown import report_citations, source_citations

    assert report_citations(_MD) == source_citations(_MD)


def test_多节合并且同一来源同号不算冲突() -> None:
    from app.report.markdown import report_citations

    citations = report_citations(_json_report([
        {"section_id": "ch-4/sec-1", "markdown": _MD},
        {"section_id": "ch-4/sec-2", "markdown": _MD},
    ]))

    assert len(citations) == 2


def test_跨节同号指向不同来源要报错() -> None:
    from app.report.markdown import report_citations

    conflict = _MD.replace(
        "https://ai.36kr.com/note-detail/3568010593718002",
        "https://example.com/other",
    )
    with pytest.raises(ValueError):
        report_citations(_json_report([
            {"section_id": "ch-4/sec-1", "markdown": _MD},
            {"section_id": "ch-4/sec-2", "markdown": conflict},
        ]))


def test_正文有角标却列不出信息源就是没读懂格式() -> None:
    from app.report.markdown import report_cites_but_lists_nothing

    cited_but_unlisted = "## 结论\n\n- 有角标[S01]。\n\n## 信息源\n\n- 无。\n"
    assert report_cites_but_lists_nothing(cited_but_unlisted) is True
    assert report_cites_but_lists_nothing(_MD) is False


def test_报告确实一条没引时不算异常_允许照常清空() -> None:
    """护栏不能太钝：真的没引任何东西时，把 citation_no 清成 NULL 是对的。"""

    from app.report.markdown import report_cites_but_lists_nothing

    nothing_cited = "## 结论\n\n- 本节无可引用证据。\n\n## 信息源\n\n- 无。\n"
    assert report_cites_but_lists_nothing(nothing_cited) is False
    assert report_cites_but_lists_nothing(_json_report([
        {"section_id": "ch-4/sec-1", "markdown": nothing_cited},
    ])) is False


def test_D013那份真成稿从0条变14条() -> None:
    """离线夹具用真产物，不自造：D-013 那轮的 JSON 成稿。"""

    from pathlib import Path

    from app.report.markdown import report_citations, source_citations

    report = Path(
        "/Users/xiaoci/Downloads/Workspace/VibeCoding/InformationCollection"
        "/Owli-d013/runs/r-982499efd2fa/goals/goal-3"
        "/goal-3-comparison-synthesis.json"
    )
    if not report.is_file():
        pytest.skip("D-013 归档产物不在本机")
    text = report.read_text(encoding="utf-8")

    assert source_citations(text) == {}   # 旧解析器：0 条，于是全库被清空
    assert len(report_citations(text)) == 14
