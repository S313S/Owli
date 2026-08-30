"""§RATE-2 货 2 候选 A（用户 08-31 拍板）：直落库的源要写下「是哪一章调的我」。

RATE-1 整跑 209 行 `agent_name` 为空就出在这里：`app/sources/xhs.py` 采完自己
`upsert_evidence_batch`，载荷只带 `id/report_id/goal_id`，而调用方 `source_mcp`
当时根本没把 agent 名传下去（只传 report_id / goal_id）。
本用例守两件事：xhs 真的把 `agent_name` 写进库；同族**没有**声明这个形参的源
不会被塞一个它不认识的参数（否则就是 TypeError，整章采集当场作废）。
"""

from __future__ import annotations

import asyncio
import functools
from datetime import datetime, timezone
from pathlib import Path

from tests.test_d015_source_persistence import ImmediateGate, _capability, _store


def _note() -> dict:
    return {
        "model_type": "note",
        "note": {
            "id": "note-rate2", "title": "扫地机器人售后体验",
            "desc": "用了半年的真实反馈",
            "type": "normal", "xsec_token": "signed=", "liked_count": 9,
            "comments_count": 3, "collected_count": 2,
            "user": {"nickname": "作者", "userid": "user-1"},
        },
    }


def test_xhs直落库带上章归属(tmp_path: Path) -> None:
    from app.adapters.source_mcp import SourceToolAdapter
    from app.sources import xhs

    store = _store(tmp_path, "r-rate2-xhs")

    def http_get(url, headers, timeout):
        del url, headers, timeout
        return xhs.HttpResponse(200, {
            "code": 200,
            "data": {"code": 200, "success": True,
                     "data": {"items": [_note()]}, "next_page": None},
        })

    # 走真 entrypoint（只把凭证/传输预绑定）——形参表里仍有 agent_name，
    # 这正是 source_mcp 判「要不要传」的依据。
    entrypoint = functools.partial(
        xhs.search, token="runtime-secret", http_get=http_get,
        rate_gate=ImmediateGate(),
        now=lambda: datetime(2026, 8, 31, tzinfo=timezone.utc),
    )
    adapter = SourceToolAdapter({"source.xhs": entrypoint}, store=store)

    asyncio.run(adapter.call(
        "source.xhs", "扫地机器人 售后", "30d", research_id="r-rate2-xhs",
        goal_id="goal-1", agent_id="data-collection-4",
        capability=_capability("xhs"), item_limit=1, on_event=lambda event: None,
    ))

    rows = store.list_evidence("r-rate2-xhs")
    assert len(rows) == 1
    assert rows[0]["agent_name"] == "data-collection-4", (
        "直落库不带章归属，这一章采到的行就没有任何章认领——"
        "评级章物化时看不见它们（RATE-1 整跑 209 行）"
    )


def test_没声明agent_name形参的源不会被塞这个参数(tmp_path: Path) -> None:
    from app.adapters.source_mcp import SourceToolAdapter

    seen: list[dict] = []

    def legacy_source(query, window, **kwargs):
        # 同族五个源（product_hunt / douyin / reddit / web_search）尚未补这一列；
        # 用 **kwargs 放行会把 agent_name 塞给它们 → TypeError → 整章作废。
        seen.append(dict(kwargs))
        return []

    adapter = SourceToolAdapter({"source.douyin": legacy_source}, store=None)
    asyncio.run(adapter.call(
        "source.douyin", "扫地机器人", "30d", research_id="r-rate2-legacy",
        goal_id="goal-1", agent_id="data-collection-9",
        capability=_capability("douyin"), on_event=lambda event: None,
    ))

    assert seen and "agent_name" not in seen[0]
    assert seen[0]["report_id"] == "r-rate2-legacy"
