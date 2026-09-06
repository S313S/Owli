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


def _undercite_new_shard(monkeypatch, *, shard: int, keep: int, times: int):
    """把指定新写片的前 ``times`` 稿改成只引本片池前 ``keep`` 条证据。"""
    import json
    from collections.abc import Callable

    import app.orchestrator.sectioning as sectioning
    from tests.test_d031_write_sharding import _pool_from_body

    original: Callable = sectioning._run_before_section_deadline
    calls: list[str] = []
    prompts: list[str] = []
    seen = 0

    async def spy(adapter, task, ctx, on_event, deadline):
        nonlocal seen
        result = await original(adapter, task, ctx, on_event, deadline)
        name = task.output_path.name
        calls.append(name)
        if f".part.{shard}." not in name:
            return result
        seen += 1
        prompts.append(task.body)
        if seen <= times:
            payload = json.loads(task.output_path.read_text(encoding="utf-8"))
            items = _pool_from_body(task.body)["items"][:keep]
            payload["claims"][0]["evidence"] = [
                {"permalink": item["permalink"]} for item in items
            ]
            task.output_path.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8",
            )
        return result

    monkeypatch.setattr(sectioning, "_run_before_section_deadline", spy)
    return calls, prompts


def test_d052_片池十条只引三条_发事件并定向重写一次(tmp_path, monkeypatch):
    """真机后两片的形态：拿 7 用 4、拿 8 用 3，四片全成、账上全绿。"""
    from tests.test_d031_write_sharding import _shard_run

    calls, prompts = _undercite_new_shard(monkeypatch, shard=2, keep=3, times=1)

    result, store, _, events, _ = _shard_run(tmp_path, evidence=30)

    assert result.succeeded is True
    # 只重写第 2 片，第 1、3 片各跑一次。
    assert calls == [
        "sec-1.part.1.md", "sec-1.part.2.md",
        "sec-1.part.2.md", "sec-1.part.3.md",
    ]
    undercited = [e for e in events if e["type"] == "write_shard_undercited"]
    assert len(undercited) == 1
    assert undercited[0]["is_error"] is True
    data = undercited[0]["data"]
    assert (data["shard"], data["pool"], data["cited"]) == (2, 10, 3)
    assert data["missing"] == [f"[S{index:02d}]" for index in range(14, 21)]
    assert data["missing_total"] == 7
    assert data["attempt"] == 1
    assert "accepted" not in data
    # 重写的 prompt 点名漏掉的角标，并把「每条都要用」讲一遍。
    assert "[S14]" in prompts[1] and "一条结论都没引到" in prompts[1]
    assert "至少被一条 `## 结论` 列表项引用" in prompts[1]
    assert store.list_chapters("r-ledger")[0]["status"] == "done"


def test_d052_重写后仍少引_接受这一稿但把读数留在事件里(tmp_path, monkeypatch):
    """上限 1 次：再付一次多半还是同一个取舍，把时间留给后面的片。"""
    from tests.test_d031_write_sharding import _shard_run

    calls, _ = _undercite_new_shard(monkeypatch, shard=2, keep=3, times=3)

    result, store, _, events, _ = _shard_run(tmp_path, evidence=30)

    assert result.succeeded is True
    # 只重写一次就收手，节级重试不因为少引被触发。
    assert calls == [
        "sec-1.part.1.md", "sec-1.part.2.md",
        "sec-1.part.2.md", "sec-1.part.3.md",
    ]
    undercited = [e["data"] for e in events if e["type"] == "write_shard_undercited"]
    assert [(item["attempt"], item.get("accepted")) for item in undercited] == [
        (1, None), (1, True),
    ]
    assert undercited[1]["cited"] == 3
    # 片仍算成，节照旧 done——少引不作废好稿。
    assert [e["data"]["succeeded"] for e in events
            if e["type"] == "write_shard_finished"] == [True, True, True]
    assert store.list_chapters("r-ledger")[0]["status"] == "done"


def test_d052_只漏一条不重写_按写手取舍放过(tmp_path, monkeypatch):
    """判据线 ≥27/30 本身留了余量；为一条证据再付一次引擎不划算。"""
    from tests.test_d031_write_sharding import _shard_run

    calls, _ = _undercite_new_shard(monkeypatch, shard=2, keep=9, times=1)

    result, _, _, events, _ = _shard_run(tmp_path, evidence=30)

    assert result.succeeded is True
    assert calls == [f"sec-1.part.{k}.md" for k in (1, 2, 3)]
    undercited = [e["data"] for e in events if e["type"] == "write_shard_undercited"]
    assert [(item["shard"], item["cited"], item.get("accepted")) for item in
            undercited] == [(2, 9, True)]
