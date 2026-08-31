"""§M6-c 货 1：login_required 判据接线——读池路径认「最新批次」。

判据（提货单已拍口径 3）：池批次 manifest `status=failed` 且
`failure.reason='login_required'` → 事件 `source_unavailable` 带
`reason='login_required'`（新增 reason 取值，沿用既有事件形状不新建类型）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from tests.test_m6b_precollect_pool import _row, _write_batch

LOGIN_MANIFEST = {
    "platform": "weibo", "status": "failed",
    "keywords": ["茶叶"], "item_count": 0,
    "failure": {"reason": "login_required"},
}


def _fixed_now():
    return datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)


def test_load_evidence给出最新批次读数(tmp_path: Path) -> None:
    from app.precollect import load_evidence

    _write_batch(tmp_path, "20260901-1000-茶叶", [_row("n1")])
    _write_batch(tmp_path, "20260901-1100-茶叶", [], manifest=LOGIN_MANIFEST)
    result = load_evidence("weibo", root=tmp_path)

    assert result.latest_batch_id == "20260901-1100-茶叶"
    assert result.latest_failure_reason == "login_required"
    # 老批次的货照常读得到——通知不吞数据。
    assert len(result.items) == 1


def test_最新批次正常时不报登录失败(tmp_path: Path) -> None:
    from app.precollect import load_evidence

    _write_batch(tmp_path, "20260901-1000-茶叶", [], manifest=LOGIN_MANIFEST)
    _write_batch(tmp_path, "20260901-1100-茶叶", [_row("n1")])
    result = load_evidence("weibo", root=tmp_path)

    # 老批次的失败残骸不算：latest_failure_reason 只看最新批次。
    assert result.latest_batch_id == "20260901-1100-茶叶"
    assert result.latest_failure_reason is None
    assert result.failure_reasons == ("login_required",)


def test_latest_login_failure只认最新批次(tmp_path: Path) -> None:
    from app.precollect import latest_login_failure

    assert latest_login_failure("weibo", root=tmp_path) is None
    _write_batch(tmp_path, "20260901-1000-茶叶", [], manifest=LOGIN_MANIFEST)
    assert latest_login_failure("weibo", root=tmp_path) == "20260901-1000-茶叶"
    _write_batch(tmp_path, "20260901-1100-茶叶", [_row("n1")])
    assert latest_login_failure("weibo", root=tmp_path) is None


def test_池空且登录挂时事件reason写login_required(tmp_path: Path) -> None:
    from app.sources import weibo

    _write_batch(tmp_path, "20260901-1100-茶叶", [], manifest=LOGIN_MANIFEST)
    events: list[dict] = []
    returned = weibo.search(
        "茶叶", "", on_event=events.append, pool_root=tmp_path, now=_fixed_now,
    )

    assert returned == []
    assert len(events) == 1
    data = events[0]["data"]
    assert events[0]["type"] == "source_unavailable"
    assert data["reason"] == "login_required"
    assert data["closed_reason"] == "login_required"
    assert data["batch_id"] == "20260901-1100-茶叶"
    # 货 3 重试要按原口径重放这次读取，query/window/limit 必须在场。
    assert data["query"] == "茶叶" and data["limit"] == 20


def test_老批次有货时照发登录通知且不吞数据(tmp_path: Path) -> None:
    from app.sources import weibo

    _write_batch(tmp_path, "20260901-1000-茶叶", [_row("n1"), _row("n2")])
    _write_batch(tmp_path, "20260901-1100-茶叶", [], manifest=LOGIN_MANIFEST)
    events: list[dict] = []
    returned = weibo.search(
        "茶叶", "", limit=10, on_event=events.append,
        pool_root=tmp_path, now=_fixed_now,
    )

    assert len(returned) == 2
    kinds = [event["type"] for event in events]
    assert kinds == ["source_unavailable", "source_usage_reconciled"]
    assert events[0]["data"]["reason"] == "login_required"
    assert events[0]["data"]["batch_id"] == "20260901-1100-茶叶"


def test_最新批次正常时读池不发登录事件(tmp_path: Path) -> None:
    from app.sources import weibo

    _write_batch(tmp_path, "20260901-1000-茶叶", [], manifest=LOGIN_MANIFEST)
    _write_batch(tmp_path, "20260901-1100-茶叶", [_row("n1")])
    events: list[dict] = []
    returned = weibo.search(
        "茶叶", "", on_event=events.append, pool_root=tmp_path, now=_fixed_now,
    )

    assert len(returned) == 1
    assert [event["type"] for event in events] == ["source_usage_reconciled"]
