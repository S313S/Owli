"""§OBS-7 货 2：正式稿 / 收尾回填这类不进章账本的引擎调用也要记账。"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from app.adapters.events import ItemKind, NormalizedEvent

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "app" / "store" / "schema.sql"
CODEX_USAGE = {
    "input_tokens": 1_000_000, "cached_input_tokens": 800_000,
    "cache_creation_input_tokens": 0, "cache_write_input_tokens": 0,
    "output_tokens": 10_000, "reasoning_output_tokens": 2_000, "cost_usd": None,
}


def _store(tmp_path: Path):
    from app.store.dao import Store

    database = tmp_path / "owli.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(SCHEMA.read_text(encoding="utf-8"))
    store = Store(database)
    store.create_report(id="r-off", title="账外", research_question="账外记账",
                        created_at="2026-09-13T00:00:00Z")
    return store


def _done(engine: str, usage: dict) -> NormalizedEvent:
    return NormalizedEvent(engine=engine, thread_id="t", turn_id="u", item_kind=ItemKind.DONE,
                           text="", is_error=False, raw={}, usage=usage)


class _Inner:
    timeout_seconds = 123

    def __init__(self) -> None:
        self.forwarded = []

    async def run(self, task, ctx, on_event=None):
        await on_event(_done("Codex", CODEX_USAGE))
        await on_event(_done("Claude", {**CODEX_USAGE, "cost_usd": 0.3}))
        await on_event({"type": "progress"})
        return "ok"


def test_套壳记账_事件照转_属性透传_按路径分桶(tmp_path: Path) -> None:
    from app.observability.cost import OFFLEDGER_KEY, UsageMeteringAdapter
    from app.store.dao import Store  # noqa: F401

    store = _store(tmp_path)
    inner = _Inner()
    seen = []

    async def on_event(event):
        seen.append(event)

    adapter = UsageMeteringAdapter(inner, store=store, research_id="r-off", path_name="polish:consulting")
    assert adapter.timeout_seconds == 123
    assert asyncio.run(adapter.run(object(), None, on_event=on_event)) == "ok"
    assert len(seen) == 3                              # usage 事件与 dict 事件都照转

    # on_event=None 的调用方（回填就是这样传的）也要记上
    asyncio.run(UsageMeteringAdapter(inner, store=store, research_id="r-off",
                                     path_name="reliability_backfill").run(object(), None))

    extra = store.get_report("r-off")["extra"]
    polish = extra[OFFLEDGER_KEY]["polish:consulting"]
    assert polish["calls"] == 2
    assert polish["costed_calls"] == 1 and polish["estimated_calls"] == 1
    assert polish["by_engine"]["codex"]["estimated_cost_usd"] == pytest.approx(0.45)
    assert extra[OFFLEDGER_KEY]["reliability_backfill"]["calls"] == 2


def test_记账失败不连累引擎调用(tmp_path: Path) -> None:
    from app.observability.cost import UsageMeteringAdapter

    class BrokenStore:
        def _connect(self):
            raise RuntimeError("库不可用")

    result = asyncio.run(UsageMeteringAdapter(_Inner(), store=BrokenStore(), research_id="r-off",
                                              path_name="polish:x").run(object(), None))
    assert result == "ok"


def test_正式稿与收尾回填两条生产路径都套了记账壳() -> None:
    polish = (ROOT / "app" / "report" / "polish" / "run.py").read_text(encoding="utf-8")
    runtime = (ROOT / "app" / "orchestrator" / "runtime.py").read_text(encoding="utf-8")
    assert 'UsageMeteringAdapter(adapter, store=store, research_id=research_id,\n                                   path_name=f"polish:{skill.name}")' in polish
    assert 'path_name="reliability_backfill"' in runtime
