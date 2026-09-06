"""§D-052：四片全写成，写手却把池里三分之一证据丢了（覆盖 21/30）。

货 0 造红实证（沙盒 8976，同库同底料各一轮）：含 §D-051 货 6 那段的 A 轮
27/30（片 3 只引 5/8），剪掉那段的 B 轮 30/30（四片零漏）。连同现场
r-6be31e934d1b（含货 6，21/30）与 r-13d61d236bff（无货 6，30/30），
四轮读数按「含不含货 6」干净分成两组。
"""

from __future__ import annotations

from app.orchestrator.sectioning import _shard_notice


def test_d052_货6那段把管辖范围点死在章节编号上():
    """「不要给编号」被写手读成连证据角标也别给；改后它只管章节编号。"""
    notice = _shard_notice(3, 4, 8, 6, "- 前面写过的")

    # 改后的正文：管辖范围写死，角标必须照引。
    assert "只管章节编号，不管证据角标" in notice
    assert "`[S01]` 这类角标是读者查证的唯一入口，该引的一个都不许省。" in notice
    # 原来那句光秃秃的「不要给编号」不许再留着——它正是被误读的那一句。
    assert "不要给编号。" not in notice

    # §D-051 货 6 那道词表锁一条都不许掉（R7 禁运行时章号与 shard）。
    for word in ("「本片」「分片」「第 N 片」", "goal-1/ch-1",
                 "ch-6/sec-1", "shard", "运行时章节编号"):
        assert word in notice


def test_d052_硬约束补上池内每条都要用这条下限():
    """原来三条硬约束全是上限，写手写完 5 条就交卷不算违规。"""
    notice = _shard_notice(3, 4, 8, 6, "")

    assert "4. 本片池里的**每一条**证据都要在 `## 信息源` 出现" in notice
    assert "至少被一条 `## 结论` 列表项引用" in notice
    # 与「最多 items 条」不打架：一条结论可以带多个角标。
    assert "一条结论可以带多个角标，与第 1 条的条数上限不冲突" in notice
    # 无关证据也要交代，不许静默丢掉。
    assert "就在结论里写明为什么不采信，同样带上它的角标" in notice


def test_d052_第1片的全节三段顺延成第5条():
    """新增的下限插在第 4 条，第 1 片那条只属于第 1 片的约束跟着顺延。"""
    first = _shard_notice(1, 4, 7, 6, "")
    later = _shard_notice(2, 4, 8, 6, "")

    assert "5. 全节口径的三段合计不超过 6 行。" in first
    assert "4. 全节口径" not in first
    assert "5. " not in later.split("【本片硬约束")[1]


def test_d052_提示词没被写长_片墙钟扛得住():
    """片墙钟 300 s 硬顶，提示词加活按片翻倍撞墙钟（CODE-1 货 3 踩过）。

    本卡只许加 ≤6 行：货 6 那段改写 +2 行，硬约束新增第 4 条 +1 行。
    """
    assert _shard_notice(3, 4, 8, 6, "").count("\n") <= 20
    assert _shard_notice(1, 4, 7, 6, "").count("\n") <= 20
