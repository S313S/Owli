"""§M6-b 货 5：探活门禁 block 档不得被误标成「引擎不可用」。

M6-a 关账后追验发现：`SourceProbeBlocked` 不是 `PlanGenerationError` 的子类，
`api/main.py` 的失败分诊便把它落到 else 分支——研究状态 `unavailable`、
标签「引擎不可用」。真因是某个源探不通，引擎本身好得很，诊断指错了方向。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "app" / "store" / "schema.sql"


async def _run_with_failure(tmp_path: Path, exception: BaseException) -> dict[str, Any]:
    from app.api.main import create_app
    from app.orchestrator.runtime import RuntimeCoordinator

    original = RuntimeCoordinator.prepare_research

    async def failing(self, research_id: str, query: str, **kwargs):
        raise exception

    RuntimeCoordinator.prepare_research = failing  # type: ignore[assignment]
    try:
        application = create_app(
            tmp_path / "owli.db", SCHEMA_PATH,
            engine_probe=lambda: {}, runs_root=tmp_path / "runs",
        )
        async with application.router.lifespan_context(application):
            transport = httpx.ASGITransport(app=application)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                created = await client.post(
                    "/api/researches", json={"query": "茶叶领域社媒竞品洞察"},
                    headers={"X-Request-ID": "m6b-probe-gate"},
                )
                assert created.status_code == 200, created.text
                research_id = created.json()["data"]["research_id"]
                for _ in range(400):
                    response = await client.get(f"/api/researches/{research_id}")
                    state = response.json()["data"]
                    if state["status"] in {"failed", "unavailable"}:
                        return state
                    await asyncio.sleep(0)
        raise AssertionError(f"研究未落终态；实际={state['status']}")
    finally:
        RuntimeCoordinator.prepare_research = original  # type: ignore[assignment]


def test_block档探活未通过标failed且标签指向探活(tmp_path: Path) -> None:
    from app.sources_probe import SourceProbeBlocked

    blocked = SourceProbeBlocked({
        "mode": "block", "blocked": True, "all_ok": False,
        "failures": {"weibo": "precollect_pool_empty"},
    })
    state = asyncio.run(_run_with_failure(tmp_path, blocked))

    assert state["status"] == "failed"
    assert state["status_label"] == "起跑前探活未通过"
    # 原因串不许在分诊里被抹平：哪个源、为什么，要能读回来。
    assert "weibo" in state["progress"]["summary"]


def test_其它后台异常仍是引擎不可用不退化(tmp_path: Path) -> None:
    state = asyncio.run(_run_with_failure(tmp_path, RuntimeError("引擎连不上")))

    assert state["status"] == "unavailable"
    assert state["status_label"] == "引擎不可用"


def test_规划失败仍标规划失败(tmp_path: Path) -> None:
    from app.plan.generate import PlanGenerationError

    state = asyncio.run(_run_with_failure(tmp_path, PlanGenerationError("连续 3 次")))

    assert state["status"] == "failed"
    assert state["status_label"] == "规划失败"
