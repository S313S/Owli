"""§RPT-2 货 3 下半：洞察层两张交叉表。

**这一层只验代码正确性，全用夹具、不碰任何真库。** 真底料读数要等 CODE-1 的编码
导入落地——我这份 09-06 拷的库副本里 `extra.coding` 一条都没有，拿它的读数当证据
只会得出「表能出数」这种假绿（空表不报错）。
"""

from __future__ import annotations

from app.reliability.coding import CODING_VERSION, polish_tables
from app.report.polish.skills import load_templates
from app.report.polish.tables import TABLE_NAMES


def _row(index: int, *, attitude="正", scenario="学习", audience="不明"):
    return {
        "id": f"ev-{index:03d}", "platform": "xhs",
        "raw_metrics": {"liked_count": index, "comments_count": 0, "collected_count": 0},
        "extra": {"content_kind": "user_opinion", "coding": {
            "coding_version": CODING_VERSION, "audience": audience,
            "scenario": scenario, "attitude": attitude,
            "topics": ["功能与能力"], "quote": "很好用",
        }},
    }


def test_场景态度表各行相加等于已编码条数():
    """`scenario_counts` 只答「在什么场景下被谈」，这张答「在那儿是夸还是骂」。"""
    rows = [_row(i, attitude="正" if i % 2 else "负",
                 scenario="学习" if i < 6 else "办公") for i in range(10)]
    table = polish_tables(rows)["scenario_attitude"]
    assert table["columns"] == ["场景", "态度", "条数"]
    assert sum(r["条数"] for r in table["rows"]) == table["n"] == 10
    assert {(r["场景"], r["态度"]) for r in table["rows"]} == {
        ("学习", "正"), ("学习", "负"), ("办公", "正"), ("办公", "负")}


def test_人群过半不明时整张不出表():
    """底料实测 287 条里 260 条不明。摆出来是一格独大的表，
    读者会把「没标出身份」读成「这类人最多」——那是把没数据写成假结论。"""
    rows = [_row(i, audience="不明" if i else "学生") for i in range(10)]
    tables = polish_tables(rows)
    assert "audience_attitude" not in tables
    # 不出表不是丢了这个数：它在 coverage 里，附录照写
    assert tables["scenario_attitude"]["coverage"]["身份不明条数"] == 9


def test_人群不明不过半时出表且对得上账():
    rows = [_row(i, audience="不明" if i < 4 else "学生") for i in range(10)]
    table = polish_tables(rows)["audience_attitude"]
    assert table["columns"] == ["人群", "态度", "条数"]
    assert sum(r["条数"] for r in table["rows"]) == table["n"] == 10
    assert "占比不到一半才出这张表" in table["basis"]


def test_角标只标进了池的那些():
    rows = [_row(i) for i in range(4)]
    table = polish_tables(rows, citations={"ev-000": 7})["scenario_attitude"]
    assert [r["marks"] for r in table["rows"]] == [["S07"]]


def test_两张表三处齐():
    """挂表 + 进 TABLE_NAMES + 写进 SKILL 的 tables 行，少一处要么判红、
    要么写手根本看不见且不报错（CODE-1 立的口径）。"""
    for name in ("scenario_attitude", "audience_attitude"):
        assert name in TABLE_NAMES, name
        for template in load_templates():
            assert name in template.tables, f"{template.name} 缺 {name}"
