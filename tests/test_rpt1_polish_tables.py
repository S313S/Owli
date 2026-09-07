"""§RPT-1 货 1：正式稿确定性数据表。

两层：合成夹具锁语义（永远跑），夜跑库副本锁真实读数（库不在就跳过）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.api.delivery import evidence_view
from app.report.polish.tables import build_tables, collect_inputs

NIGHTLY_DB = Path(__file__).resolve().parents[1] / "var" / "rpt1-8956.db"
NIGHTLY_RUNS = Path(__file__).resolve().parents[1] / "var" / "runs"
NIGHTLY_IDS = ("r-b10812f664d2", "r-3e04f808dffd", "r-045acebc352b")


def _evidence(**over):
    row = {"id": "ev-1", "platform": "xhs", "kind": "post", "citation_no": None,
           "title": "豆包很好用", "content_excerpt": "免费又流畅", "grade": "B",
           "published_at": "2026-08-01T00:00:00Z", "extra": "{}"}
    row.update(over)
    return row


PLAN = {"research_question": "国内大家对豆包的看法", "subjects": ["豆包", "Doubao", "Kimi"],
        "entities": [
            {"id": "豆包", "canonical": "豆包",
             "names": {"zh": "豆包", "en": "Doubao", "aliases": ["Doubao", "豆包AI"]}},
            {"id": "Doubao", "canonical": "豆包", "names": {"zh": "豆包", "en": "Doubao"}},
            {"id": "Kimi", "canonical": "Kimi", "names": {"zh": "Kimi", "en": "Kimi"}}],
        "goals": [{"goal_id": "goal-1", "objective": "采官方定位"}]}


def _build(evidence, claims=(), sources=()):
    return build_tables(report={"id": "r-t", "title": "T"}, plan=PLAN, evidence=list(evidence),
                        claims=list(claims), view={"title": "T", "sources": list(sources)})


def test_subject_that_is_already_an_alias_does_not_become_a_second_entity():
    """`subjects` 里的 `Doubao` 已是 canonical=豆包 的别名，不得再拆出一行。"""
    rows = _build([_evidence()])["tables"]["entity_mentions"]["rows"]
    assert [r["实体"] for r in rows] == ["豆包", "Kimi"]
    assert rows[0]["提及条数"] == 1 and rows[1]["提及条数"] == 0


def test_platform_mix_n_equals_evidence_view_total():
    """货 1 判据：表的 n 与 `evidence_view().counts` 对得上。"""
    evidence = [_evidence(id="ev-1", citation_no=1), _evidence(id="ev-2", platform="reddit"),
                _evidence(id="ev-3", platform="reddit", kind="comment")]
    tables = _build(evidence)["tables"]
    counts = evidence_view([dict(e) for e in evidence])["counts"]
    assert tables["platform_mix"]["n"] == counts["total"] == 3
    by_platform = {r["平台"]: r["采集条数"] for r in tables["platform_mix"]["rows"]}
    assert by_platform == counts["by_platform"]
    assert sum(r["被引条数"] for r in tables["platform_mix"]["rows"]) == counts["cited"] == 1


def test_timeline_is_dropped_when_published_at_coverage_is_thin():
    """发布时间覆盖不足三成宁可整表不出，免得写手拿 3 条数据讲趋势。"""
    evidence = [_evidence(id=f"ev-{i}", published_at=None) for i in range(9)]
    evidence.append(_evidence(id="ev-9", published_at="2026-08-01T00:00:00Z"))
    assert "timeline" not in _build(evidence)["tables"]


def test_timeline_folds_months_beyond_the_last_twelve():
    """>12 个月时最早的那些合并成一行「更早」，柱子数封在 13 根内。"""
    evidence = [_evidence(id=f"ev-{i}", published_at=f"20{20 + i // 12:02d}-{i % 12 + 1:02d}-01")
                for i in range(20)]
    rows = _build(evidence)["tables"]["timeline"]["rows"]
    assert len(rows) == 13 and rows[0]["月份"].startswith("更早")
    assert sum(r["证据条数"] for r in rows) == 20


def test_topic_polarity_counts_word_hits_not_sentiment():
    """正负词同现的一条要同时进两栏并记入「正负同现」，不做二选一的情感判定。"""
    evidence = [_evidence(id="ev-1", title="豆包功能很好用", content_excerpt="但是经常胡说")]
    row = next(r for r in _build(evidence)["tables"]["topic_polarity"]["rows"]
               if r["主题"] == "功能与能力")
    assert (row["提及条数"], row["含正向词"], row["含负向词"], row["正负同现"]) == (1, 1, 1, 1)


def test_only_cited_evidence_produces_marks():
    """未被引用的证据不产生角标——正式稿只能引工作稿信息源池里的号。"""
    evidence = [_evidence(id="ev-1", citation_no=7), _evidence(id="ev-2", citation_no=None)]
    rows = _build(evidence)["tables"]["platform_mix"]["rows"]
    assert rows[0]["marks"] == ["S07"]


class _Store:
    def __init__(self, database: Path) -> None:
        import sqlite3

        self.conn = sqlite3.connect(database)
        self.conn.row_factory = sqlite3.Row

    def get_report(self, research_id: str):
        row = self.conn.execute("select * from reports where id=?", (research_id,)).fetchone()
        return dict(row) if row else None

    def list_evidence(self, research_id: str):
        return [dict(r) for r in
                self.conn.execute("select * from evidence where report_id=?", (research_id,))]


@pytest.mark.skipif(not NIGHTLY_DB.exists(), reason="夜跑库副本不在本 worktree")
@pytest.mark.parametrize("research_id", NIGHTLY_IDS)
def test_nightly_reports_all_produce_every_table(research_id: str):
    """三份 completed 成稿各出全表，且 n 与 `evidence_view().counts.total` 相等。"""
    store = _Store(NIGHTLY_DB)
    report = store.get_report(research_id)
    path = Path(report["report_path"])
    text = (NIGHTLY_RUNS / research_id / "goals" / path.parent.name / path.name).read_text("utf-8")
    data = collect_inputs(store, research_id, text)
    assert set(data["tables"]) == {"platform_mix", "grade_mix", "crossref_mix", "entity_mentions",
                                   "topic_polarity", "entity_dimension", "timeline"}
    total = evidence_view(store.list_evidence(research_id))["counts"]["total"]
    assert data["tables"]["platform_mix"]["n"] == total == data["counts"]["evidence"]
    assert all(table["rows"] for table in data["tables"].values())
    assert json.dumps(data, ensure_ascii=False)  # 全表可 JSON 序列化，才能落 tables.json


# —— §CODE-1 货 2 接线时要守的契约（表由 CODE-1 自己加，本包只锁这一条） ——


def test_numbers_in_a_table_under_the_tables_key_count_as_sourced():
    """CODE-1 必须把三张表挂在 `tables` 键下——尺子 ④ 的白名单只从 tables/counts 递归收数。
    挂顶层的话，附录那句「287 条里 260 条看不出身份」会被 ④ 判成没出处。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "check_polished",
        Path(__file__).resolve().parents[1] / "scripts/acceptance/rpt1/check_polished.py")
    check_polished = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(check_polished)
    allowed: set[str] = set()
    check_polished._numbers_from({"attitude_by_topic": {"rows": [{"条数": 287}]}}, allowed)
    assert "287" in allowed


# —— §RPT-2 货 1 的读者身份两字段：本包负责把它取出来并投喂给写手 ——

@pytest.mark.parametrize("plan_over, role, stake", [
    ({"audience_role": "竞品团队", "audience_stake": "想知道这对我意味着什么"},
     "竞品团队", "想知道这对我意味着什么"),
    ({"audience": {"role": "本产品团队"}}, "本产品团队", ""),
    ({}, "不明", ""),
])
def test_audience_is_read_from_the_plan_with_不明_as_the_default(plan_over, role, stake):
    """没答不是错：两处都没有就落「不明」，正式稿照写，只是不按读者身份分行。"""
    data = build_tables(report={"id": "r-t", "title": "T"}, plan={**PLAN, **plan_over},
                        evidence=[_evidence()], claims=[], view={"title": "T", "sources": []})
    assert data["audience_role"] == role
    assert data["audience_stake"] == stake


def test_audience_reaches_the_writer_prompt(tmp_path):
    """取出来还得投喂出去——写手看不到就等于没接。"""
    from app.report.polish.run import build_prompt
    from app.report.polish.skills import get_template

    data = build_tables(report={"id": "r-t", "title": "T"},
                        plan={**PLAN, "audience_role": "投资与分析", "audience_stake": "值不值得投"},
                        evidence=[_evidence()], claims=[], view={"title": "T", "sources": []})
    prompt = build_prompt(get_template("consulting"), data, "# 报告\n", tmp_path / "s.md",
                          parts=[("执行摘要", tmp_path / "s.md")], current="执行摘要")
    assert "# 这份报告给谁看\n投资与分析" in prompt
    assert "他们最想知道的：值不值得投" in prompt
