"""§M6-a 货 4：起跑前探活门禁化——探不通的源不许静默滑过去。

X-1 货 4 只做了「报数」（`/api/sources/probe`），结果没人消费；
M6 接 MediaCrawler 后登录态一失效，整跑会照常起跑然后整章空手而归。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.orchestrator.runtime import RuntimeCoordinator
from app.sources_probe import SourceProbeBlocked, gate_report, probe_gate_mode

RESEARCH_ID = "r-m6a-gate"
_FAILED = {"xhs": {"ok": False, "items": 0, "failure": "empty"},
           "web_search": {"ok": True, "items": 2, "failure": None}}


def _coordinator(tmp_path, published: list, probe):
    async def publish(research_id, payload):
        published.append(payload)

    return RuntimeCoordinator(
        store=SimpleNamespace(), event_buffer=SimpleNamespace(publish=publish),
        researches={}, cards={}, runs_root=tmp_path / "runs", auto_confirm=True,
        routing_utc_clock=lambda: datetime(2026, 9, 1, tzinfo=timezone.utc),
        source_probe=probe,
    )


def test_门禁档位认不出的写法一律当off() -> None:
    assert probe_gate_mode({}) == "off"
    assert probe_gate_mode({"OWLI_SOURCE_PROBE_GATE": "Block"}) == "block"
    assert probe_gate_mode({"OWLI_SOURCE_PROBE_GATE": "yes"}) == "off"


def test_探活失败在warn档留下事件痕迹但不挡起跑(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OWLI_SOURCE_PROBE_GATE", "warn")
    published: list = []

    async def probe():
        return _FAILED

    coordinator = _coordinator(tmp_path, published, probe)
    asyncio.run(coordinator._gate_on_source_probe(RESEARCH_ID))

    assert [item["type"] for item in published] == ["source_probe_gate"]
    data = published[0]["data"]
    assert data["degraded"] == ["xhs"] and data["failures"] == {"xhs": "empty"}
    assert data["blocked"] is False


def test_探活失败在block档挡住起跑并带上原因(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OWLI_SOURCE_PROBE_GATE", "block")
    published: list = []

    async def probe():
        return _FAILED

    coordinator = _coordinator(tmp_path, published, probe)

    with pytest.raises(SourceProbeBlocked, match="xhs=empty"):
        asyncio.run(coordinator._gate_on_source_probe(RESEARCH_ID))

    # 挡之前先留痕：事件已经发出去了，不是只抛个异常了事。
    assert published[0]["data"]["blocked"] is True


def test_全通过时不挡但仍留一条通过痕迹(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OWLI_SOURCE_PROBE_GATE", "block")
    published: list = []

    async def probe():
        return {"web_search": {"ok": True, "items": 2, "failure": None}}

    coordinator = _coordinator(tmp_path, published, probe)
    asyncio.run(coordinator._gate_on_source_probe(RESEARCH_ID))

    assert published[0]["data"]["degraded"] == []
    assert published[0]["data"]["blocked"] is False


def test_探活器不在场就什么都不做(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OWLI_SOURCE_PROBE_GATE", "block")
    published: list = []

    coordinator = _coordinator(tmp_path, published, None)
    asyncio.run(coordinator._gate_on_source_probe(RESEARCH_ID))

    assert published == []


def test_探活器自己抛错只降级成告警不拖垮研究(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OWLI_SOURCE_PROBE_GATE", "block")
    published: list = []

    async def probe():
        raise RuntimeError("探活器坏了")

    coordinator = _coordinator(tmp_path, published, probe)
    asyncio.run(coordinator._gate_on_source_probe(RESEARCH_ID))

    assert published[0]["data"] == {
        "research_id": RESEARCH_ID, "mode": "block", "probed": [],
        "degraded": [], "failures": {}, "blocked": False,
    }


def test_门禁结论按取到数据判不按HTTP判() -> None:
    report = gate_report(
        {"douyin": {"ok": False, "items": 0, "failure": None}}, mode="warn",
    )

    assert report["failures"] == {"douyin": "unknown"}
    assert report["blocked"] is False
