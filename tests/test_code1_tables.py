"""§CODE-1 货 2 备料：聚合表与比例句式闸词（都不碰 polish/，等解禁后被它调用）。"""

from __future__ import annotations

from app.reliability.coding import (
    CODING_VERSION,
    TOPIC_NONE,
    coded_rows,
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


def test_主题闭集就是词表的键():
    """两份词表迟早分叉，且分叉是静默的——改了键名，老数据会落在闭集外。"""

    from app.report.polish.lexicon import TOPIC_LEXICON
    from app.reliability.coding import TOPICS

    assert TOPICS == tuple(TOPIC_LEXICON)


def test_库里已编码的主题都在闭集内():
    """锁住「已落库的编码 ⊆ 当前词表键」：将来改词表把老数据打成越界，这条先红。"""

    from app.reliability.coding import TOPICS

    rows = [
        {"id": "ev-1", "extra": {"coding": {
            "coding_version": "v1", "audience": "不明", "scenario": "学习",
            "attitude": "正", "topics": ["功能与能力", "竞品对比"], "quote": "好用",
        }}},
        {"id": "ev-2", "extra": {"coding": {
            "coding_version": "v1", "audience": "不明", "scenario": "其他",
            "attitude": "中", "topics": [], "quote": "",
        }}},
    ]
    used = {t for row in coded_rows(rows) for t in (row["coding"].get("topics") or [])}
    assert used <= set(TOPICS), f"越界主题：{sorted(used - set(TOPICS))}"


def _coded(index: int, *, topics, attitude="正", scenario="学习", quote="好用"):
    return {"id": f"ev-{index:03d}", "platform": "xhs", "extra": {"coding": {
        "coding_version": "v1", "audience": "不明", "scenario": scenario,
        "attitude": attitude, "topics": topics, "quote": quote}}}


def test_三张表用的是正式稿标准壳():
    """壳与 polish/tables.py:_table 一字不差，否则写手那头要另写分支。"""

    from app.report.polish.tables import _table
    from app.reliability.coding import polish_tables

    standard = set(_table("x", "t", (), [], n=0, basis="", coverage={}))
    tables = polish_tables([_coded(1, topics=["功能与能力"])], citations={"ev-001": 4})
    assert set(tables) == {"attitude_by_topic", "scenario_counts", "quotes"}
    for name, table in tables.items():
        assert set(table) == standard, name
        assert table["name"] == name
        # marks 是行内一列，不是壳字段——另六张表都这么摆。
        assert "marks" not in standard
        assert all(isinstance(row.get("marks"), list) for row in table["rows"]), name


def test_角标形态与其余六表一致():
    """出 S04 不出 [S04]：形态两套，写手和尺子都会对不上。"""

    from app.reliability.coding import polish_tables

    tables = polish_tables([_coded(1, topics=["功能与能力"])], citations={"ev-001": 4})
    assert tables["attitude_by_topic"]["rows"][0]["marks"] == ["S04"]


def test_没角标的原声不进表():
    """写手引不动的原声，摆出来只会诱导它裸引（尺子③ 会判红，且判得对）。"""

    from app.reliability.coding import polish_tables

    rows = [_coded(1, topics=["功能与能力"], quote="引得动"),
            _coded(2, topics=["功能与能力"], quote="引不动")]
    quotes = polish_tables(rows, citations={"ev-001": 4})["quotes"]["rows"]
    assert [row["原声"] for row in quotes] == ["引得动"]
    assert all(row["marks"] for row in quotes)


def test_分母写进壳里防误读():
    """n 是已编码 UGC 条数，不是全库条数——写手只看得见 title 与 basis。"""

    from app.reliability.coding import polish_tables

    rows = [_coded(1, topics=["功能与能力"]), {"id": "ev-002", "extra": {}}]
    table = polish_tables(rows, citations={"ev-001": 4})["scenario_counts"]
    assert table["n"] == 1
    assert table["coverage"] == {"已编码 UGC 条数": 1, "全库证据条数": 2, "身份不明条数": 1}
    assert "不是全库 2 条证据" in table["basis"]


def test_闸词判的是推及全网_不是判百分号():
    """占比数据照写，写成人群断言才红——一刀切禁百分号，假红会比真红还多。"""

    # 合法：表里的占比、附录交底的覆盖率，都没有人群主语。
    assert ratio_phrase_offenders("小红书被引占比 15%，微博 8%。") == []
    assert ratio_phrase_offenders("287 条编码里 260 条看不出身份（90%）。") == []
    assert ratio_phrase_offenders("抽检 30 条，一致 28 条。") == []
    # 违规：同句里有人群主语，就是用户拍甲禁的那种句式。
    assert ratio_phrase_offenders("用户里有 62% 给了正面评价。") == ["62%"]
    assert ratio_phrase_offenders("网友百分之六十认为好用。") == ["百分之六十"]
    # 无条件违规：跟有没有数字无关。
    assert ratio_phrase_offenders("大多数用户觉得不错。") == ["大多数用户"]
    # 跨句不误伤：占比在前一句，人群主语在后一句。
    assert ratio_phrase_offenders("被引占比 15%。用户反馈以正面为主。") == []
