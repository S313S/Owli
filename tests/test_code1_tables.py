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
    # §RPT-2 货 3 往同一个产出里加了 scenario_attitude（场景 × 态度）；
    # audience_attitude 只在身份不明不过半时才出，这份夹具全是「不明」故不在。
    # 这条用例要的是「每张表都用标准壳」，集合跟着长，意图不变。
    assert set(tables) == {"attitude_by_topic", "scenario_counts", "quotes",
                           "scenario_attitude"}
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
    # §RPT-2 货 4③ 往同一个 coverage 里加了「带触发事件条数」（trigger 可空，
    # 写手得知道那张表代表多少条）。这条用例锁的是「分母写进壳里」，键长了意图不变。
    assert table["coverage"] == {"已编码 UGC 条数": 1, "全库证据条数": 2,
                                 "身份不明条数": 1, "带触发事件条数": 0}
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


def _build(evidence):
    from app.report.polish.tables import build_tables

    return build_tables(report={"id": "r-1"}, plan={}, evidence=evidence, claims=[],
                        view={"title": "t", "sources": []})


def test_没有编码时三张表不出_并写明原因():
    """空表比缺表坏：写手会把 n=0 读成「这个维度没人讨论」，那是假结论。"""

    data = _build([{"id": "ev-001", "extra": {"content_kind": "user_opinion"}}])
    for name in ("attitude_by_topic", "scenario_counts", "quotes"):
        assert name not in data["tables"], name
        assert "没有已编码的 UGC" in data["omitted_tables"][name]
        assert "不等于没人讨论" in data["omitted_tables"][name]


def test_有编码但原声筛空时_只缺原声表():
    """编码非 0 而原声为 0 也要挡——整块判空漏得掉这一种。"""

    # 有编码、但没给角标，原声一条都留不下（只挑引得动的）。
    data = _build([_coded(1, topics=["功能与能力"])])
    assert "attitude_by_topic" in data["tables"]
    assert "scenario_counts" in data["tables"]
    assert "quotes" not in data["tables"]
    assert "按这张表自己的口径筛完没有一行" in data["omitted_tables"]["quotes"]


def test_三张表都出得来时_omitted为空():
    # §D-059 货 4 起，原声表还要过一道等级闸（只收 A/B，与正文的引语闸同口径），
    # 所以夹具要给 `grade`——不给等于「未评级」，那一行本来就不许进正文当原声。
    data = _build([dict(_coded(1, topics=["功能与能力"]), citation_no=4, grade="B")])
    assert data["omitted_tables"] == {}
    assert "quotes" in data["tables"]


def test_原声表只收正文引得了的等级():
    """§D-059 货 4：C 级证据不得进原声表。

    ⛔ 这不是收紧口味，是解一个死锁：共用规则 §5.6 步骤 4 与写作期闸
    `run.lowgrade_quotes` 都只许引 A/B 级，而这张表以前不看等级。真机实测
    「回答质量·负」那一格唯一的候选是 C 级，写手要给这格写引语只有它可选，
    引了必被闸打回——**重试 7 次全废、整轮 35.6 分钟没出稿**。
    """
    from app.report.polish.run import QUOTE_GRADES

    assert "C" not in QUOTE_GRADES and set(QUOTE_GRADES) == {"A", "B"}

    c_level = _build([dict(_coded(1, topics=["功能与能力"]), citation_no=4, grade="C")])
    assert "quotes" not in c_level["tables"], "C 级原声不许进表——摆出来写手就会引"

    for grade in ("A", "B"):
        ok = _build([dict(_coded(1, topics=["功能与能力"]), citation_no=4, grade=grade)])
        assert "quotes" in ok["tables"], f"{grade} 级是正文引得了的，不该被挡"


def test_等级筛掉的条数要在表注里交代():
    """⛔ 又加一道闸却不交代，读者会把「这一格没有原声」读成「没人这么说」。

    表注的契约是 `_quotes_footnote` 自己写的：「行数少的时候必须自己交代是筛短的」。
    ⚠️ 等级那一刀要**单独**说，不能并进实体那一刀——两刀砍的不是同一件事：
    一个是「这句没在说研究对象」，一个是「说的是研究对象，但证据只够作旁证」。
    合成一句会让读者以为原声少是因为没人谈，而实情是证据不够硬。
    """
    from app.report.polish.tables import build_tables

    # ⚠️ 表注只在**实体闸真设上**时才出（`dropped["设闸"]`），所以夹具必须给真计划——
    # `plan={}` 的话叫法表是空的、闸不设，整段表注不出，这条用例会绿着什么也没验。
    plan = {"research_question": "大家对豆包的看法", "title": "大家对豆包的看法",
            "entities": [{"id": "豆包", "canonical": "豆包",
                          "names": {"zh": "豆包", "en": "Doubao", "aliases": []}}]}
    data = build_tables(
        report={"id": "r-1"}, plan=plan, claims=[], view={"title": "t", "sources": []},
        evidence=[
            dict(_coded(1, topics=["功能与能力"], quote="豆包挺好用的，省了不少事。"),
                 citation_no=4, grade="B"),
            # 点名了研究对象，但证据只够作旁证 → 该被等级那一刀砍掉并计数
            dict(_coded(2, topics=["回答质量"], attitude="负",
                        quote="豆包答得很敷衍，没法用。"), citation_no=5, grade="C"),
            # 谁都没点名 → 该被实体那一刀砍掉，两刀要分开说
            dict(_coded(3, topics=["回答质量"], attitude="负",
                        quote="隔壁那家真香，谁用谁知道。"), citation_no=6, grade="B"),
        ])
    basis = data["tables"]["quotes"]["basis"]

    assert "只够作旁证（C 级）" in basis, "等级那一刀必须交代"
    assert "同一把尺子" in basis, "要说清和正文用的是同一个口径"
    assert "没提到研究对象" in basis, "实体那一刀原样保留，两刀分开说"


def test_没给等级表时不按等级筛():
    """备料与离线核数要看全量，与 `entity_names` 为空同族。"""
    from app.reliability.coding import coding_tables

    rows = [dict(_coded(1, topics=["功能与能力"]), citation_no=4)]
    assert coding_tables(rows, citations={"ev-001": 4})["quotes"], "不给等级表就不该过滤"


def test_从库里真读出来的行也认得出编码(tmp_path):
    """用例喂的形状必须和生产喂的形状一样，否则用例绿着而生产是红的。

    `Store.list_evidence` 给的是解好的 dict，裸 sqlite 读出来的是 JSON 字符串。
    只认 dict 的话，裸读的调用方会**静默**拿到 0 条编码——三张表整块不出，
    全程零报错，看起来像「编码没做」，其实是类型没对上。
    """

    import json
    import sqlite3

    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    conn.execute("create table evidence (id text, report_id text, extra text)")
    coding = {"coding_version": "v1", "audience": "不明", "scenario": "学习",
              "attitude": "正", "topics": ["功能与能力"], "quote": "好用"}
    conn.execute("insert into evidence values (?,?,?)",
                 ("ev-001", "r-1", json.dumps({"content_kind": "user_opinion",
                                               "coding": coding}, ensure_ascii=False)))
    conn.commit()
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("select * from evidence")]

    assert isinstance(rows[0]["extra"], str), "前提：裸 sqlite 读出来就是字符串"
    assert len(coded_rows(rows)) == 1
    assert coded_rows(rows)[0]["coding"]["attitude"] == "正"
    # 同一份数据换成解好的 dict，认出来的编码必须一模一样（`extra` 本身一个是
    # 字符串一个是 dict，所以比 coding 而不是比整行）。
    parsed = [{**r, "extra": json.loads(r["extra"])} for r in rows]
    assert [r["coding"] for r in coded_rows(parsed)] == \
           [r["coding"] for r in coded_rows(rows)]


def test_extra是坏字符串时不炸():
    assert coded_rows([{"id": "ev-1", "extra": "{不是 json"}]) == []
    assert coded_rows([{"id": "ev-1", "extra": "null"}]) == []
    assert coded_rows([{"id": "ev-1", "extra": ""}]) == []
