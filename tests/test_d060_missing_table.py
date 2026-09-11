"""§D-060 货 2：附录「哪些没采到」表要说真话。

零引擎。夹具照 r-3e04f808dffd 的四行 missing 与计划快照形状造，先造红再造绿。
根因：`_MISSING_REASON` 把 timeout 一律译成「采集超时没跑完」，不看落库条数——
§D-039 之后 timeout 的语义是「超时判 missing、已落库产物不作废」；「缺的是哪一段」
按 goal 取目标整句截 34 字，缺的单位却是单源子章，三行同目标长得一模一样。
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

from app.report.polish.run import missing_table
from app.report.polish.tables import chapter_rows

ROOT = Path(__file__).resolve().parents[1]


def _forbidden_hits(text: str) -> list[str]:
    spec = importlib.util.spec_from_file_location(
        "cp", ROOT / "scripts" / "acceptance" / "rpt1" / "check_polished.py")
    cp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cp)
    return [m.group(0) for pattern in cp.FORBIDDEN for m in re.finditer(pattern, text)]


def _agent(agent_id, chapter_id, kind, display, sources=(), entity=""):
    return {"agent_id": agent_id, "display_name": display, "entity": entity,
            "capability": {"sources": list(sources)},
            "chapter": {"chapter_id": chapter_id, "chapter_type": kind}}


PLAN = {"goals": [
    {"goal_id": "goal-1", "title": "豆包官方产品定位与功能资料采集",
     "objective": "从豆包官网、字节跳动官方公告、应用市场页与Doubao英文官方页采集产品定位。",
     "agents": [_agent("data-collection", "ch-1", "collection", "小红书数据抓取·豆包", ["xhs"], "豆包"),
                _agent("reliability-audit", "ch-2", "audit", "可靠度审计")]},
    {"goal_id": "goal-2", "title": "国内社媒与测评渠道对豆包的用户口碑采集",
     "objective": "从知乎、微博、小红书、B站、贴吧、App Store与安卓应用市场评论采集口碑。",
     "agents": [_agent("data-collection-3", "ch-1", "collection", "微博数据抓取·Doubao", ["weibo"], "Doubao"),
                _agent("data-collection-4", "ch-3", "collection", "微信公众号数据抓取·文心一言",
                       ["wechat_mp"], "文心一言")]},
    {"goal_id": "goal-3", "title": "国内同类AI助手竞品对照素材采集",
     "objective": "采集DeepSeek、Kimi、文心一言的产品定位、能力侧重与国内用户口碑要点。",
     "agents": [_agent("data-collection-6", "ch-3", "collection", "Reddit 数据抓取·豆包", ["reddit"], "豆包"),
                _agent("report-writing-3", "ch-6", "report", "报告撰写")]},
]}
OBJECTIVES = [{"goal_id": g["goal_id"], "objective": g["objective"]} for g in PLAN["goals"]]
MISSING = [
    {"goal_id": "goal-1", "chapter_id": "ch-1", "reason": "timeout"},
    {"goal_id": "goal-2", "chapter_id": "ch-3", "reason": "empty_result"},
    {"goal_id": "goal-3", "chapter_id": "ch-3", "reason": "timeout"},
    {"goal_id": "goal-3", "chapter_id": "ch-6/sec-1", "reason": "conclusion_invalid"},
]


def _rows():
    """证据行：小红书 296 条记在 goal-2 名下（实测就是这样标错的），Reddit 111 条；公众号 0。"""
    rows = [{"agent_name": "data-collection", "goal_id": "goal-2", "platform": "xhs",
             "citation_no": 23 if i < 12 else None} for i in range(296)]
    rows += [{"agent_name": "data-collection-6", "goal_id": "goal-3", "platform": "reddit",
              "citation_no": None} for _ in range(111)]
    rows += [{"agent_name": "data-collection-3", "goal_id": "goal-2", "platform": "weibo",
              "citation_no": 33} for _ in range(27)]
    return rows


def _lines(md: str) -> list[str]:
    return [ln for ln in md.splitlines() if ln.startswith("| ") and "缺的是哪一段" not in ln]


# ── 造红：旧口径对本研究四行的字面，与 09-11 成稿第 ③ 行相同 ─────────────────
def test_old_wording_calls_111_reddit_rows_not_collected():
    md = missing_table(MISSING, OBJECTIVES)
    assert _lines(md)[2] == "| 采集DeepSeek、Kimi、文心一言的产品定位、能力侧重与国内用… | 这一段采集超时没跑完 |"
    assert _lines(md)[2].split("|")[1] == _lines(md)[3].split("|")[1], "③④ 两行段落名一模一样"


def test_new_wording_says_collected_but_timed_out():
    chapters = chapter_rows(PLAN, _rows())
    md = missing_table(MISSING, OBJECTIVES, chapters=chapters)
    lines = _lines(md)
    assert lines[2] == "| Reddit（豆包） | 采到 111 条，但整理步骤超时，未纳入本章分析 |"
    assert lines[0] == "| 小红书（豆包） | 采到 296 条，但整理步骤超时，未纳入本章分析 |"


def test_yield_is_counted_by_agent_not_by_the_mislabelled_goal():
    """证据表把小红书记在 goal-2、计划记 goal-1——按 agent_name 数才是 296，按 goal 数是 0。"""
    chapters = {(c["goal_id"], c["chapter_id"]): c for c in chapter_rows(PLAN, _rows())}
    assert chapters[("goal-1", "ch-1")]["yielded"] == 296
    assert chapters[("goal-1", "ch-1")]["cited"] == 12


def test_a_real_gap_still_says_not_collected_with_the_channel_name():
    """不能把真缺改没了：公众号×文心一言 0 条，仍写「没采到」，段落名变成渠道（实体）。"""
    md = missing_table(MISSING, OBJECTIVES, chapters=chapter_rows(PLAN, _rows()))
    assert _lines(md)[1] == "| 微信公众号（文心一言） | 这一段跑完了但没采到任何内容 |"


def test_timeout_with_zero_rows_keeps_the_old_reason():
    rows = [r for r in _rows() if r["agent_name"] != "data-collection-6"]
    md = missing_table(MISSING, OBJECTIVES, chapters=chapter_rows(PLAN, rows))
    assert _lines(md)[2] == "| Reddit（豆包） | 这一段采集超时没跑完 |"


def test_report_section_names_the_goal_and_the_section_not_the_ids():
    md = missing_table(MISSING, OBJECTIVES, chapters=chapter_rows(PLAN, _rows()))
    assert _lines(md)[3].startswith(
        "| 「国内同类AI助手竞品对照素材采集」的报告·第 1 节（豆包官方产品定位与功能资料采集） |")
    assert _forbidden_hits(md) == [], _forbidden_hits(md)


def test_without_chapters_the_table_is_byte_for_byte_unchanged():
    """老调用方、老产物：不给 chapters 一个字都不变。"""
    assert missing_table(MISSING, OBJECTIVES) == missing_table(MISSING, OBJECTIVES, chapters=())


def test_unknown_chapter_falls_back_to_the_objective_sentence():
    md = missing_table([{"goal_id": "goal-9", "chapter_id": "ch-1", "reason": "timeout"}],
                       OBJECTIVES, chapters=chapter_rows(PLAN, _rows()))
    assert "第 1 段" in md and _forbidden_hits(md) == []


def test_chapter_rows_use_the_platform_display_name():
    rows = {c["agent_id"]: c for c in chapter_rows(PLAN, [])}
    assert rows["data-collection-4"]["platforms"] == ["微信公众号"]
    assert rows["data-collection-6"]["platforms"] == ["Reddit"]
    assert rows["report-writing-3"]["chapter_type"] == "report"
