"""§M6-d 货 4：公众号薄源与导入器（platform 参数化复用 M6-b）。

判据落在**库行**上，不落在「函数没抛错」上（[[verdict-on-db-not-log]]）：
池有货 → 库里查得到 platform=wechat_mp 的行且 agent_name 是调用章；
池空 → 发 source_unavailable 并说得出 closed_reason，不静默报成功。
"""

from __future__ import annotations

from pathlib import Path

from tests.test_m6d_wechat_pool import PERMANENT, _row, _write_batch


ROOT = Path(__file__).resolve().parents[1]


def _store(tmp_path: Path, report_id: str):
    from app.store.dao import Store
    from app.store.schema import initialize_database_if_empty

    database = tmp_path / "owli.db"
    initialize_database_if_empty(database, ROOT / "app/store/schema.sql")
    store = Store(database)
    store.create_report(
        id=report_id, title="M6-d 公众号入池入库",
        research_question="池里的公众号长文能不能原样入库",
        created_at="2026-09-02T00:00:00Z",
    )
    return store, database


def test_薄源池有货则入库且章归属写在行上(tmp_path: Path) -> None:
    from app.sources.wechat_mp import search

    pool = tmp_path / "pool"
    _write_batch(pool, "20260902-1100-茶叶", [_row(f"a{i}") for i in range(4)])
    store, database = _store(tmp_path, "r-mp-1")

    events: list[dict] = []
    items = search(
        "茶叶", limit=10, store=store, report_id="r-mp-1", goal_id="goal-1",
        agent_name="cn-collector", on_event=events.append, pool_root=pool,
    )

    assert len(items) == 4
    rows = store.list_evidence("r-mp-1")
    assert [row["platform"] for row in rows] == ["wechat_mp"] * 4
    assert {row["agent_name"] for row in rows} == {"cn-collector"}
    assert all(row["permalink"].startswith("https://mp.weixin.qq.com/s/")
               for row in rows)
    reconciled = [e for e in events if e["type"] == "source_usage_reconciled"]
    assert reconciled and reconciled[0]["data"]["source"] == "wechat_mp"
    assert reconciled[0]["data"]["provider"] == "owli_precollect"


def test_薄源池空发不可用并说得出原因(tmp_path: Path) -> None:
    from app.sources.wechat_mp import search

    events: list[dict] = []
    items = search("茶叶", limit=10, on_event=events.append, pool_root=tmp_path)

    assert items == []
    unavailable = [e for e in events if e["type"] == "source_unavailable"]
    assert unavailable
    assert unavailable[0]["data"]["closed_reason"] == "precollect_pool_empty"
    assert unavailable[0]["data"]["source"] == "wechat_mp"


def test_薄源通配词读整池是探活口径(tmp_path: Path) -> None:
    """池里有哪些词是预采集时定的；拿固定词探池型源必然空手，量的不是源坏了。"""

    from app.sources.wechat_mp import POOL_WILDCARD, search

    pool = tmp_path / "pool"
    _write_batch(pool, "20260902-1101-普洱", [_row(
        "b1", source_keyword="普洱", title="普洱行情", content="普洱行情走高。" * 20)])

    assert search("茶叶", limit=5, pool_root=pool) == []
    assert len(search(POOL_WILDCARD, limit=5, pool_root=pool)) == 1


def test_导入器platform参数化直接吃公众号且幂等重导库行不变(tmp_path: Path) -> None:
    from app.precollect_import import run

    pool = tmp_path / "pool"
    _write_batch(pool, "20260902-1200-茶叶", [_row(f"c{i}") for i in range(10)])
    store, database = _store(tmp_path, "r-mp-2")
    argv = [
        "--platform", "wechat_mp", "--report-id", "r-mp-2",
        "--goal-id", "goal-1", "--agent-name", "cn-collector",
        "--query", "茶叶", "--pool-root", str(pool),
        "--database", str(database), "--no-prune",
    ]

    assert run(argv) == 0
    first = store.list_evidence("r-mp-2")
    assert len(first) == 10
    assert {row["fetch_method"] for row in first} == {"browser_agent"}

    # 幂等：同一批重导，去重键仍是 report_id+platform+platform_item_id。
    assert run(argv) == 0
    assert len(store.list_evidence("r-mp-2")) == 10


def test_导入器撞上临时链缺快照当场红而不是导进去一半(tmp_path: Path) -> None:
    from app.precollect import PoolContractError
    from app.precollect_import import run
    from tests.test_m6d_wechat_pool import TEMPORARY

    import pytest

    pool = tmp_path / "pool"
    _write_batch(pool, "20260902-1201-茶叶", [
        _row("ok1"), _row("bad1", url=TEMPORARY),
    ])
    store, database = _store(tmp_path, "r-mp-3")
    with pytest.raises(PoolContractError):
        run([
            "--platform", "wechat_mp", "--report-id", "r-mp-3",
            "--goal-id", "goal-1", "--agent-name", "cn-collector",
            "--pool-root", str(pool), "--database", str(database), "--no-prune",
        ])
    # 整批拒收：不许「好的那条进去了、坏的那条丢了」这种半成功。
    assert store.list_evidence("r-mp-3") == []


def test_池空导入器退出码2且打出closed_reason(tmp_path: Path, capsys) -> None:
    from app.precollect_import import run

    store, database = _store(tmp_path, "r-mp-4")
    code = run([
        "--platform", "wechat_mp", "--report-id", "r-mp-4",
        "--goal-id", "goal-1", "--agent-name", "cn-collector",
        "--pool-root", str(tmp_path / "empty"), "--database", str(database),
    ])

    assert code == 2
    assert "precollect_pool_empty" in capsys.readouterr().out


def test_最新批次未登录时发的事件形状能被登录卡认出来(tmp_path: Path) -> None:
    """§M6-c 的 LOGIN_REPAIR 卡是平台无关的：它按 source+batch_id 发卡。

    公众号将来走客户端搜一搜必然要登录态，这条钉住「新平台白拿这条链路」，
    而不是等真机撞上才发现事件里少个 batch_id 所以卡永远发不出来。
    """

    import json

    from app.precollect import LOGIN_REQUIRED_REASON
    from app.sources.wechat_mp import search

    pool = tmp_path / "pool"
    directory = pool / "wechat_mp" / "20260902-1300-茶叶"
    directory.mkdir(parents=True)
    (directory / "manifest.json").write_text(json.dumps({
        "platform": "wechat_mp", "status": "failed",
        "failure": {"reason": LOGIN_REQUIRED_REASON, "detail": "搜一搜未登录"},
    }, ensure_ascii=False), encoding="utf-8")

    events: list[dict] = []
    assert search("茶叶", limit=5, on_event=events.append, pool_root=pool) == []
    data = next(e["data"] for e in events if e["type"] == "source_unavailable")
    # 发卡的三个必要字段：runtime 缺 source 或 batch_id 就直接 return 不发卡。
    assert data["reason"] == LOGIN_REQUIRED_REASON
    assert data["source"] == "wechat_mp"
    assert data["batch_id"] == "20260902-1300-茶叶"
    assert data["query"] == "茶叶" and data["limit"] == 5
