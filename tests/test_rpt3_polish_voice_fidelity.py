"""§RPT-3 货 2–5：写手池带原话、清单只列实引、把握度数字程序注入、超时文案说真话。

判据落在成稿文本与 tables.json 的形状上；不付引擎调用。
"""

from __future__ import annotations

from pathlib import Path

from app.report.polish.run import (CONFIDENCE_HEADING, PROGRAM_APPENDIX_HEADINGS, altered_quotes,
                                   assemble, build_prompt, confidence_line, confidence_tables,
                                   missing_table, quote_corpus, sources_table)
from app.report.polish.skills import get_template
from app.report.polish.tables import QUOTE_PREFIX_CHARS, quote_prefix

S28 = ("在我看来“是否下载猫箱”是一场典型的囚徒博弈，我个人认为仍然会有很多人去下载的，"
       "因为在10月15日之前，至少智能体的数据还能全部迁移，就算还原度不高，但至少还是留下了一部分。"
       "如果选择反抗猫箱，然后豆包在十月十五之前也没能重启的话")

TABLES = {
    "crossref_mix": {"n": 310, "rows": [
        {"交叉验证结论": "SINGLE", "主张数": 255}, {"交叉验证结论": "WEAK", "主张数": 34},
        {"交叉验证结论": "PASS", "主张数": 21}], "basis": "按 reports.extra.claims[].verdict 计数"},
    "grade_mix": {"n": 80, "basis": "等级由五维评分合计定档", "rows": [
        {"等级": "A", "被引条数": 16, "全库条数": 17, "含义": "可独立支撑结论"},
        {"等级": "?", "被引条数": 0, "全库条数": 374, "含义": "未评级，正文不引"}]},
}


# ── 货 2 ────────────────────────────────────────────────────────────────────

def test_原话前缀只给AB级评论_程序截取不改字() -> None:
    comment = {"kind": "comment", "grade": "A", "content_excerpt": S28}
    prefix = quote_prefix(comment)
    assert prefix == S28[:QUOTE_PREFIX_CHARS] and "猫箱" in prefix
    assert quote_prefix({**comment, "grade": "C"}) is None, "C 级不许作原声"
    assert quote_prefix({**comment, "kind": "post"}) is None, "帖子行不带原话栏"
    assert quote_prefix({**comment, "content_excerpt": "a\n\n b"}) == "a b"


def _data(**extra):
    base = {"research_question": "国内大家对豆包的看法", "objectives": [], "entities": ["豆包"],
            "tables": {}, "sources": [
                {"mark": "S28", "grade": "A", "crossref": "SINGLE",
                 "title": "评论 · 豆包将新增付费版本", "url": "https://x/28",
                 "quote_prefix": S28[:120]},
                {"mark": "S63", "grade": "D", "title": "某帖", "url": "https://x/63"}]}
    base.update(extra)
    return base


def test_写手池里评论行带原话栏_帖子行不带() -> None:
    prompt = build_prompt(get_template("consulting"), _data(), "# 工作稿\n", Path("x.md"))
    pool = prompt.split("# 信息源池", 1)[1].split("\n# ", 1)[0]
    s28 = next(line for line in pool.splitlines() if line.startswith("- S28"))
    s63 = next(line for line in pool.splitlines() if line.startswith("- S63"))
    assert s28.endswith(f"｜原话：{S28[:120]}")
    assert "原话" not in s63
    assert "归题看原话不看标题" in pool.splitlines()[0]


def test_照抄原话栏过逐字闸_改一个字过不了() -> None:
    corpus = quote_corpus(_data(), "# 工作稿\n")
    ok = "> 至少智能体的数据还能全部迁移，就算还原度不高\n> —— 小红书 · 等级 A [S28]\n"
    bad = "> 至少智能体的资料还能全部迁移，就算还原度不高\n> —— 小红书 · 等级 A [S28]\n"
    assert altered_quotes(ok, corpus) == []
    assert altered_quotes(bad, corpus)


def test_写作规则写明原话栏可引_归题看原话() -> None:
    from app.report.polish.skills import shared_rules

    rules = shared_rules()
    assert "原话：" in rules and "归题一律看原话，不看标题" in rules


# ── 货 3 ────────────────────────────────────────────────────────────────────

POOL = [{"mark": f"S{n:02d}", "grade": "B", "title": f"t{n}", "url": f"https://x/{n}",
         "contrast": n > 3} for n in range(1, 7)]


def test_清单只列正文实引_其余折一行并数对照实体() -> None:
    md = sources_table(POOL, cited={2, 5})
    rows = [line for line in md.splitlines() if line.startswith("| S")]
    assert [row.split(" | ")[0] for row in rows] == ["| S02", "| S05"]
    assert "引用池另有 4 条本稿未引用（其中对照实体 2 条）" in md
    assert "只列正文实际引用过的证据" in md
    # 老调用方不给 cited：照列整个池，不折行。
    assert sum(line.startswith("| S") for line in sources_table(POOL).splitlines()) == 6


def test_组装时按写手各节实引裁清单_附录原声表的角标不算(tmp_path: Path) -> None:
    summary = tmp_path / "a.md"
    summary.write_text("正文[S02][S05]。\n", encoding="utf-8")
    appendix = tmp_path / "b.md"
    appendix.write_text("## 方法与样本\n\n无。\n", encoding="utf-8")
    quotes_block = "## 代表原声（逐字摘录，按互动量排序）\n\n| 原声 | 角标 |\n|---|---|\n| x | [S06] |\n"
    md = assemble([("执行摘要", summary), ("附录", appendix)], POOL,
                  appendix_blocks=(quotes_block,))
    listed = [line.split(" | ")[0] for line in md.split("## 信息源清单")[1].splitlines()
              if line.startswith("| S")]
    assert listed == ["| S02", "| S05"]
    assert "引用池另有 4 条" in md


# ── 货 4 ────────────────────────────────────────────────────────────────────

def test_摘要把握度句后注入主张计数_三数之和等于n(tmp_path: Path) -> None:
    line = confidence_line(TABLES)
    assert line == "（程序按交叉验证结论计数）主张 310 条：单源 255 / 偏弱 34 / 多源互证 21。"
    assert 255 + 34 + 21 == TABLES["crossref_mix"]["n"]
    summary = tmp_path / "a.md"
    summary.write_text("1. 发现一[S02]\n\n> 本报告结论的把握度为**低**，主要因为……\n\n收尾句。\n",
                       encoding="utf-8")
    appendix = tmp_path / "b.md"
    appendix.write_text("无。\n", encoding="utf-8")
    md = assemble([("执行摘要", summary), ("附录", appendix)], POOL, tables=TABLES)
    lines = md.splitlines()
    at = next(i for i, text in enumerate(lines) if "把握度为" in text)
    assert lines[at + 2] == line and "收尾句" in lines[at + 4]
    for word in ("SINGLE", "PASS", "WEAK"):
        assert word not in md.split("# 附录")[0], "开篇节不许出现内部口径词（尺子⑦）"


def test_把握度两张表挂附录且算作程序块() -> None:
    block = confidence_tables(TABLES)
    assert block.startswith(CONFIDENCE_HEADING) and CONFIDENCE_HEADING in PROGRAM_APPENDIX_HEADINGS
    assert "| 单源 | 255 | 82.3% |" in block and "| 多源互证 | 21 | 6.8% |" in block
    assert "| A | 16 | 17 |" in block and "| 未评级 | 0 | 374 |" in block
    assert "SINGLE" not in block and "reports.extra" not in block
    assert confidence_tables({}) == "" and confidence_line({}) == ""


def test_正式稿组装把把握度表放进附录程序块() -> None:
    import inspect

    from app.report.polish import run

    source = inspect.getsource(run.polish)
    assert "confidence_tables(" in source and "tables=data.get(\"tables\")" in source


# ── 货 5 ────────────────────────────────────────────────────────────────────

def test_超时有货的段写已入库并参与评级与统计_写手提示词看的也是这张表(monkeypatch) -> None:
    missing = [{"goal_id": "goal-1", "chapter_id": "ch-1", "reason": "timeout",
                "text": "此处缺失：goal-1/ch-1；原因：timeout"}]
    chapters = [{"goal_id": "goal-1", "chapter_id": "ch-1", "chapter_type": "collection",
                 "platforms": ["小红书"], "entity": "豆包", "yielded": 273}]
    md = missing_table(missing, chapters=chapters)
    assert "| 小红书（豆包） | 采到 273 条，已入库并参与评级与统计；" in md
    assert "采集章" not in md and "未纳入" not in md
    # r-50600e09f7dd 工作稿是 JSON 成稿，解析出的缺失行就是上面这个形状。
    monkeypatch.setattr("app.report.render.parse_report",
                        lambda text: {"missing": missing, "conclusions": [], "sections": []})
    prompt = build_prompt(get_template("consulting"), _data(chapters=chapters), "{}", Path("x.md"))
    view = prompt.split("# 工作稿\n", 1)[1]
    assert "采到 273 条，已入库并参与评级与统计" in view
    assert "timeout" not in view and "goal-1/ch-1" not in view
