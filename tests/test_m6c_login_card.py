"""§M6-c 货 2/3：LOGIN_REPAIR 发卡（幂等）与答复钩子（重试一次/两败 degraded）。

发卡方 = 读池组件的 login_required 事件经 runtime 事件管道转译，不是 scheduler；
卡行落库 = `card_update` 事件写进 events 表。答复钩子三条路：
「已补登录」且池已好 → 恢复并重导入；池仍坏 → 第二败 degraded 停手无第三张卡；
「跳过」→ 直接 degraded 记录。
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any

from tests.test_m6b_precollect_pool import _row, _store, _write_batch
from tests.test_m6c_pool_login_signal import LOGIN_MANIFEST

BATCH = "20260901-1100-茶叶"


def async_test(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return wrapper


def _login_payload(batch_id: str = BATCH) -> dict[str, Any]:
    """形状照 §货 1 weibo.search 实发的事件，不另造。"""

    return {
        "type": "source_unavailable",
        "data": {
            "source": "weibo", "reason": "login_required",
            "closed_reason": "login_required", "batch_id": batch_id,
            "query": "茶叶", "window": "", "limit": 20,
            "provider": "media_crawler", "task_continues": True,
        },
    }


def _runtime(tmp_path: Path, report_id: str = "r-m6c"):
    from app.api.events import ResearchEventBuffer
    from app.orchestrator.runtime import RuntimeCoordinator

    store, database = _store(tmp_path, report_id)
    events = ResearchEventBuffer(store=store)
    researches: dict[str, dict[str, Any]] = {
        report_id: {"research_id": report_id, "cards": [], "goals": [], "events": []},
    }
    cards: dict[str, Any] = {}
    runtime = RuntimeCoordinator(
        store=store, event_buffer=events, researches=researches, cards=cards,
        auto_confirm=False, runs_root=tmp_path / "runs",
        routing_utc_clock=lambda: datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    return runtime, store, database, researches, cards


def _event_rows(store, report_id: str, event_type: str) -> list[dict[str, Any]]:
    return [
        dict(row["payload"])
        for row in store.list_events_window(report_id, created_since="", limit=500)
        if row["payload"].get("type") == event_type
    ]


@async_test
async def test_login事件发卡_两轮造红只出一张(tmp_path: Path) -> None:
    from app.plan.cards import CardBlocking, CardStatus, CardType

    runtime, store, _, researches, cards = _runtime(tmp_path)
    for _ in range(2):  # 同一批次两轮事件 → 幂等闸只放一张卡
        await runtime._maybe_issue_login_repair_card(
            "r-m6c", "goal-1", "微博数据抓取·茶叶", _login_payload(),
        )

    login_cards = [
        card for card in cards.values()
        if card.card_type is CardType.LOGIN_REPAIR
    ]
    assert len(login_cards) == 1
    card = login_cards[0]
    assert card.card_id == f"r-m6c-login-weibo-{BATCH}"
    assert card.blocking is CardBlocking.NONE and card.status is CardStatus.PENDING
    assert [action["id"] for action in card.actions] == ["relogin", "skip"]
    assert [action["label"] for action in card.actions] == ["已补登录", "跳过"]
    assert card.goal_id == "goal-1" and card.target["batch_id"] == BATCH
    # 人话操作指引：登录窗登好 + 重跑预采集这两步必须说给人听。
    assert "登录助手" in card.body and "重跑" in card.body
    # 卡行落库：card_update 事件在 events 表恰一行；state 里也只挂一张。
    assert len(_event_rows(store, "r-m6c", "card_update")) == 1
    assert len(researches["r-m6c"]["cards"]) == 1


@async_test
async def test_reason不是login_required或缺batch_id不发卡(tmp_path: Path) -> None:
    runtime, store, _, _, cards = _runtime(tmp_path)
    other = _login_payload()
    other["data"]["reason"] = "tool_unavailable"
    await runtime._maybe_issue_login_repair_card("r-m6c", "g", "a", other)
    missing = _login_payload()
    del missing["data"]["batch_id"]
    await runtime._maybe_issue_login_repair_card("r-m6c", "g", "a", missing)

    assert cards == {}
    assert _event_rows(store, "r-m6c", "card_update") == []


def _issue_card(runtime, cards, researches, pool: Path, *, report_id: str = "r-m6c"):
    """注册一张带 pool_root 的登录卡（夹具指路用；真发卡路径已在上面验过）。"""

    from app.login_repair import build_login_repair_card

    card = build_login_repair_card(
        research_id=report_id, goal_id="goal-1", agent_id="微博数据抓取·茶叶",
        platform="weibo", batch_id=BATCH, query="茶叶", window="", limit=20,
        created_at="2026-09-01T00:00:00+00:00", pool_root=str(pool),
    )
    cards[card.card_id] = card
    researches[report_id]["cards"].append(card.to_dict())
    runtime._login_repair.note_issued(report_id, "weibo", BATCH)
    return card


@async_test
async def test_已补登录且池已好_恢复并重导入(tmp_path: Path) -> None:
    runtime, store, database, researches, cards = _runtime(tmp_path)
    pool = tmp_path / "pool"
    _write_batch(pool, BATCH, [], manifest=LOGIN_MANIFEST)
    card = _issue_card(runtime, cards, researches, pool)
    # 用户在本机登好并重跑了预采集：池里出现更新的成功批次。
    _write_batch(pool, "20260901-1200-茶叶", [_row("n1"), _row("n2")])

    resolved = await runtime.respond_card(
        card.card_id, action="relogin", payload={"choice": "relogin"},
    )

    assert resolved.status.value == "answered"
    assert resolved.result["outcome"] == "recovered"
    assert resolved.result["imported"] == 2
    with sqlite3.connect(database) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM evidence WHERE report_id = 'r-m6c'"
        ).fetchone()[0]
    assert count == 2  # 重导入真的落了库
    assert not runtime._login_repair.is_degraded("r-m6c", "weibo")
    # 恢复后，将来**新的**失败批次还能再发卡。
    assert runtime._login_repair.should_issue("r-m6c", "weibo", "20260902-0900-茶叶")
    # 答复后的卡状态回写 state 并再发一条 card_update。
    assert researches["r-m6c"]["cards"][0]["status"] == "answered"
    assert len(_event_rows(store, "r-m6c", "card_update")) == 1


@async_test
async def test_池仍坏_第二败degraded停手且无第三张卡(tmp_path: Path) -> None:
    runtime, store, _, researches, cards = _runtime(tmp_path)
    pool = tmp_path / "pool"
    _write_batch(pool, BATCH, [], manifest=LOGIN_MANIFEST)  # 池没修好
    card = _issue_card(runtime, cards, researches, pool)

    resolved = await runtime.respond_card(
        card.card_id, action="relogin", payload={"choice": "relogin"},
    )

    assert resolved.result["outcome"] == "degraded_after_retry"
    assert resolved.result["failed_batch_id"] == BATCH
    assert runtime._login_repair.is_degraded("r-m6c", "weibo")
    # degraded 记录沿用 source_unavailable 形状落库。
    degraded = [
        event for event in _event_rows(store, "r-m6c", "source_unavailable")
        if event["data"].get("closed_reason") == "login_repair_degraded"
    ]
    assert len(degraded) == 1 and degraded[0]["data"]["cause"] == "retry_failed"
    # 停手：新批次的 login_required 事件不再出第三张卡。
    await runtime._maybe_issue_login_repair_card(
        "r-m6c", "goal-1", "微博数据抓取·茶叶", _login_payload("20260901-1300-茶叶"),
    )
    assert len(cards) == 1
    # 重复答复幂等返回，不炸不重跑。
    again = await runtime.respond_card(
        card.card_id, action="relogin", payload={"choice": "relogin"},
    )
    assert again.result["outcome"] == "degraded_after_retry"


@async_test
async def test_跳过_直接degraded记录不重试(tmp_path: Path) -> None:
    runtime, store, database, researches, cards = _runtime(tmp_path)
    pool = tmp_path / "pool"
    _write_batch(pool, BATCH, [], manifest=LOGIN_MANIFEST)
    card = _issue_card(runtime, cards, researches, pool)

    resolved = await runtime.respond_card(
        card.card_id, action="skip", payload={"choice": "skip"},
    )

    assert resolved.result["outcome"] == "degraded_skipped"
    assert runtime._login_repair.is_degraded("r-m6c", "weibo")
    with sqlite3.connect(database) as connection:
        count = connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]
    assert count == 0  # 跳过不重试、不导入
    degraded = [
        event for event in _event_rows(store, "r-m6c", "source_unavailable")
        if event["data"].get("closed_reason") == "login_repair_degraded"
    ]
    assert degraded[0]["data"]["cause"] == "user_skipped"
