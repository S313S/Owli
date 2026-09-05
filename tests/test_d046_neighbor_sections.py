"""§D-046：点名补一节，同章已完成的邻节被连带重写。

两次现场（缺陷卡 docs/acceptance/loop/defects/D-046.md）：补 `ch-6/sec-2` 毁了
`sec-1`；补 `ch-6/sec-1` 把 `sec-2`（19 637 B）与 `sec-3`（24 142 B）双双打回
一百来字节的占位。

**根因是两层，且只有第二层坏**：
① 复位边界是好的——`_is_replayed` 对 `only_chapters=["ch-6/sec-1"]` 只认
   `ch-6` 与 `ch-6/sec-1`，邻节行按 done 原样搬过去、盘上产物一个字没动。
   本文件第一组用例把这条钉住，防以后有人「顺手」把同章节一起复位。
② 复位父章会把 `ch-6` 从「账本已 done」集合里拿掉，于是节化撰写重算邻节的
   可见证据池时跨 goal 那一截够不着了（sec-2 底料池 30 条 weibo+web_search+reddit，
   补节轮只剩 15 条 weibo）→ 老角标一律判越池 → `stale_done` 把整节复位、
   连片产物一起删、从头重写。**补一节 = 整章 N 节重新抽签。**
   第二组用例要求：已 done 且盘上产物还在的节跳过、不调引擎、字节不变。
"""

from __future__ import annotations


# ---------- ① 复位边界只含点名节与父章 ----------

def _rows() -> list[dict]:
    """一章三节，sec-1 是靶子，sec-2 / sec-3 是已写好的邻节。"""

    return [
        {"goal_id": "goal-3", "chapter_id": "ch-6", "status": "done"},
        {"goal_id": "goal-3", "chapter_id": "ch-6/sec-1", "status": "done"},
        {"goal_id": "goal-3", "chapter_id": "ch-6/sec-2", "status": "done"},
        {"goal_id": "goal-3", "chapter_id": "ch-6/sec-3", "status": "done"},
        {"goal_id": "goal-3", "chapter_id": "ch-5", "status": "done"},
    ]


def _reset_set(only_chapters: list[str]) -> set[str]:
    from app.replay.import_research import _is_replayed, _replay_chapters

    wanted = _replay_chapters(only_chapters)
    return {
        str(row["chapter_id"])
        for row in _rows()
        if _is_replayed(row, {"goal-3"}, wanted, reset_done=False)
    }


def test_d046_点名一节只复位它和父章_同章邻节不在复位集合里() -> None:
    assert _reset_set(["ch-6/sec-1"]) == {"ch-6", "ch-6/sec-1"}


def test_d046_点名整章才连子节一起复位() -> None:
    """点名 `ch-6`（不带 `/sec-N`）是「这章整个重做」，与补一节是两回事。"""

    assert _reset_set(["ch-6"]) == {
        "ch-6", "ch-6/sec-1", "ch-6/sec-2", "ch-6/sec-3",
    }


def test_d046_点名的节不牵连同一个goal里别的章() -> None:
    assert "ch-5" not in _reset_set(["ch-6/sec-1"])
