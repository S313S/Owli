"""§M6-b 货 3：微博薄源——池有货入库、池空报 missing。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from tests.test_m6b_precollect_pool import _row, _store, _write_batch


ROOT = Path(__file__).resolve().parents[1]


def _fixed_now():
    return datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)


def test_池有货时入库_platform与章归属都对(tmp_path: Path) -> None:
    from app.sources import weibo

    pool = tmp_path / "pool"
    _write_batch(pool, "20260901-1100-茶叶", [_row(f"n{i}") for i in range(4)])
    report_id = "r-m6b-weibo"
    store, database = _store(tmp_path, report_id)
    events: list[dict] = []

    returned = weibo.search(
        "茶叶", "30d", limit=10, store=store, report_id=report_id,
        goal_id="goal-1", agent_name="微博数据抓取·茶叶",
        on_event=events.append, pool_root=pool, now=_fixed_now,
    )

    assert len(returned) == 4
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        rows = list(connection.execute(
            "SELECT platform, agent_name, fetch_method, grade, source_keyword "
            "FROM evidence WHERE report_id = ?", (report_id,),
        ))
    assert len(rows) == 4
    assert {row["platform"] for row in rows} == {"weibo"}
    assert {row["agent_name"] for row in rows} == {"微博数据抓取·茶叶"}
    assert {row["fetch_method"] for row in rows} == {"media_crawler"}
    assert {row["grade"] for row in rows} == {"C"}
    assert [event["type"] for event in events] == ["source_usage_reconciled"]


def test_池空时发source_unavailable并说明原因(tmp_path: Path) -> None:
    from app.sources import weibo

    events: list[dict] = []
    returned = weibo.search(
        "茶叶", "30d", limit=10, on_event=events.append,
        pool_root=tmp_path / "empty-pool", now=_fixed_now,
    )

    assert returned == []
    assert len(events) == 1
    data = events[0]["data"]
    assert events[0]["type"] == "source_unavailable"
    assert data["source"] == "weibo"
    assert data["closed_reason"] == "precollect_pool_empty"
    assert data["task_continues"] is True


def test_批次标login_required时原因透到事件里(tmp_path: Path) -> None:
    from app.sources import weibo

    pool = tmp_path / "pool"
    _write_batch(
        pool, "20260901-1101-茶叶", [],
        manifest={"platform": "weibo", "status": "failed",
                  "failure": {"reason": "login_required"}},
    )
    events: list[dict] = []
    assert weibo.search(
        "茶叶", "", on_event=events.append, pool_root=pool, now=_fixed_now,
    ) == []
    assert events[0]["data"]["closed_reason"] == "login_required"


def test_时间窗把老行筛掉时也要说清楚不假绿(tmp_path: Path) -> None:
    """[[verdict-is-data-not-http200]]：0 条也得有个说得出口的原因。

    §SRC-1 的抖音教训是「自家 window 校验打回 25% 的调用」却查不出来。
    这里把「池里有货但都在窗外」与「池里根本没货」分成两个 closed_reason。
    """

    from app.sources import weibo

    pool = tmp_path / "pool"
    _write_batch(pool, "20260901-1102-茶叶", [_row("old", create_time=1756684800)])
    events: list[dict] = []

    assert weibo.search(
        "茶叶", "30d", on_event=events.append, pool_root=pool, now=_fixed_now,
    ) == []
    data = events[0]["data"]
    assert data["closed_reason"] == "precollect_no_match"
    assert data["rows_seen"] == 1 and data["dropped_by_window"] == 1

    # 不给时间窗就该取回来——证明上面那条确实是窗筛掉的，不是别的毛病。
    assert len(weibo.search("茶叶", "", pool_root=pool, now=_fixed_now)) == 1


def test_探活用通配词量池的死活而不是某个关键词(tmp_path: Path) -> None:
    import asyncio

    from app.sources import weibo
    from app.sources_probe import PROBE_QUERIES, probe_sources

    pool = tmp_path / "pool"
    _write_batch(pool, "20260901-1103-茶叶", [_row("n0")])
    assert PROBE_QUERIES["weibo"] == weibo.POOL_WILDCARD

    def entrypoint(query: str, window: str = "", **kwargs):
        return weibo.search(query, window, pool_root=pool, now=_fixed_now, **kwargs)

    results = asyncio.run(probe_sources(
        ["weibo"], registry={"weibo": entrypoint},
        env_path=tmp_path / "absent.env",
    ))
    # 池里只有「茶叶」，缺省探活词是「AI 助手」——通配词让它量的是池不是词。
    assert results["weibo"]["ok"] is True and results["weibo"]["items"] == 1
