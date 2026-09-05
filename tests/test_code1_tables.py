"""§CODE-1 货 2 备料：聚合表与比例句式闸词（都不碰 polish/，等解禁后被它调用）。"""

from __future__ import annotations

from app.reliability.coding import (
    CODING_VERSION,
    TOPIC_NONE,
    coding_tables,
    ratio_phrase_offenders,
)


def _row(index: int, *, attitude="正", topics=("功能与能力",), scenario="学习",
         audience="不明", quote="很好用", liked=0):
    return {
        "id": f"ev-{index:03d}", "platform": "xhs",
        "raw_metrics": {"liked_count": liked, "comments_count": 0,
                        "collected_count": 0},
        "extra": {"content_kind": "user_opinion", "coding": {
            "coding_version": CODING_VERSION, "audience": audience,
            "scenario": scenario, "attitude": attitude,
            "topics": list(topics), "quote": quote,
        }},
    }


def test_场景表条数与已编码条数相等():
    rows = [_row(i, scenario="学习" if i % 2 else "办公") for i in range(10)]
    tables = coding_tables(rows)
    assert tables["coded_rows"] == 10
    assert tables["reconciliation"]["scenario_sum"] == 10
    assert sum(r["count"] for r in tables["scenario_counts"]) == 10


def test_一条命中多主题时主题表算命中次数():
    rows = [_row(0, topics=("功能与能力", "价格与付费")), _row(1, topics=())]
    tables = coding_tables(rows)
    assert tables["reconciliation"]["topic_hit_sum"] == 3
    assert tables["reconciliation"]["distinct_rows"] == 2
    # 无主题的行不许从主表里消失，否则永远对不上账
    assert any(r["topic"] == TOPIC_NONE for r in tables["attitude_by_topic"])


def test_原声按互动量降序且每格最多三条():
    rows = [_row(i, quote=f"原声{i}", liked=i) for i in range(6)]
    quotes = coding_tables(rows)["quotes"]
    assert len(quotes) == 3
    assert [q["quote"] for q in quotes] == ["原声5", "原声4", "原声3"]
    assert quotes[0]["engagement"] == 5


def test_有角标表就填角标():
    rows = [_row(0)]
    assert coding_tables(rows)["quotes"][0]["citation"] is None
    marked = coding_tables(rows, citations={"ev-000": 7})
    assert marked["quotes"][0]["citation"] == "[S07]"


def test_人群不出表只出附录一句():
    rows = [_row(i, audience="不明" if i else "学生") for i in range(10)]
    tables = coding_tables(rows)
    assert "audience_by_scenario" not in tables and "audience" not in tables
    assert tables["audience_note"] == "10 条编码里 9 条看不出发帖人身份（90%）"
    assert "包终端复核" in tables["method_note"] and "条数" in tables["method_note"]


def test_闸词只打自己写的比例句_不打引用原文():
    assert ratio_phrase_offenders("用户普遍认为好用，30% 的人这么说") == [
        "用户普遍", "30%"]
    assert ratio_phrase_offenders("本节 4 条编码为正向") == []
    # 小红书话题名里就带「百分之一」，打红它是冤枉写手——本包重放实测踩过
    assert ratio_phrase_offenders(
        "小红书「人类对豆包的开发不足百分之一」话题下") == []
    assert ratio_phrase_offenders(
        "[#人类对豆包的开发不足百分之一](https://x.com/a?b=1)") == []


def test_空语料不炸():
    tables = coding_tables([])
    assert tables["coded_rows"] == 0 and tables["quotes"] == []
    assert tables["audience_note"] == "无已编码证据"
