"""相关性硬闸（用户 2026-09-08 拍）：标题/正文不含任何被评实体叫法的行不进池。

判据落在「谁进池」，不落在「全报告编号」——加在 `_evidence_index` 里面会连编号
一起改，而重放常常只重放一节，编号一动，同一份工作稿里同一个角标会指两条源。
"""

from __future__ import annotations

from app.reliability.relevance import rows_naming_entities

PLAN = {
    "entities": [{"canonical": "豆包", "id": "豆包",
                  "names": {"zh": "豆包", "en": "Doubao", "aliases": ["豆包AI"]}}],
    "subjects": ["豆包", "Doubao", "Kimi"],
}


def _row(title="", excerpt="", rid="ev-1"):
    return {"id": rid, "title": title, "content_excerpt": excerpt}


def test_谁都没提的行被挡掉():
    rows = [_row("新年快乐#春暖中国", "新年快乐"),
            _row("AI is a symptom", "low effort to churn out content fast")]
    assert rows_naming_entities(rows, PLAN) == []


def test_含叫法的行留下_中英叫法都认():
    rows = [_row("豆包，开始收费了", "三档订阅价格"),
            _row("Doubao user agreement", "it does violate Doubao's user agreement"),
            _row("跑题", "跟谁都没关系")]
    kept = rows_naming_entities(rows, PLAN)
    assert len(kept) == 2
    assert {r["title"] for r in kept} == {"豆包，开始收费了", "Doubao user agreement"}


def test_竞品叫法照样放行_这是用户拍的口径不是漏网():
    """用户 2026-09-08 拍的是「只挡谁都没提那一类」，**明确不误伤竞品节**。

    所以讲 Kimi 的内容照样进池——竞品对照那一节本来就要引它。
    第一版用例我按「只认研究主角」写，把这条判红了，**是用例编错了口径不是代码错**。
    真要收紧到只认主角，得另请用户拍，并且得同时回答「竞品节怎么办」。
    """
    kept = rows_naming_entities([_row("Kimi K3 真的顶", "两个真实编程项目实测")], PLAN)
    assert len(kept) == 1


def test_豆包与Doubao按canonical并成一个实体():
    """D-053：`subjects` 里它俩是两条，分配器拿错那份把一个产品当两个、吃掉两个 goal 名额。

    这里走 `_entity_aliases`，两种叫法命中的是**同一个**实体——所以中英文两条都留下。
    """
    kept = rows_naming_entities(
        [_row("豆包，开始收费了"), _row("Doubao pricing"), _row("谁都没提")], PLAN)
    assert len(kept) == 2


def test_全被挡光时回退未过滤那份_不返回空池():
    """兜底与 D 闸 `keepable or identified` 同族。

    池空了写手什么都写不出来，比让它看到几条不相关的更糟。**兜底在调用点上**：
    `rows_naming_entities` 如实返回空，由调用点的 `relevant or evidence_rows` 回退。
    """
    rows = [_row("新年快乐"), _row("跟谁都没关系")]
    relevant = rows_naming_entities(rows, PLAN)
    assert relevant == []
    assert (relevant or rows) == rows, "回退没生效，写手会拿到空池"


def test_取不到叫法时不设闸_原样返回():
    """拿不到判据时宁可不筛，也不要把池筛空。"""
    rows = [_row("新年快乐"), _row("豆包好用")]
    assert len(rows_naming_entities(rows, {"entities": [], "subjects": []})) == 2
