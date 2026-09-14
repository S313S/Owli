"""§OBS-7-fu：Codex 标价改成实际跑的 gpt-5.6-terra 实价。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "app" / "store" / "schema.sql"

# 新输入 1M + 缓存读 1M + 输出 1M。OpenAI 口径缓存读含在 input_tokens 里，所以 input 记 2M。
CODEX_TURN = {
    "input_tokens": 2_000_000,
    "cached_input_tokens": 1_000_000,
    "cache_creation_input_tokens": 0,
    "cache_write_input_tokens": 0,
    "output_tokens": 1_000_000,
    "reasoning_output_tokens": 300_000,
    "cost_usd": None,
}


def _store(tmp_path: Path):
    from app.store.dao import Store

    database = tmp_path / "owli.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(SCHEMA.read_text(encoding="utf-8"))
    store = Store(database)
    store.create_report(id="r-terra", title="费用", research_question="费用观测",
                        created_at="2026-09-14T00:00:00Z")
    store.ensure_chapters("r-terra", [{"goal_id": "goal-1", "chapter_id": "ch-2"}],
                          updated_at="2026-09-14T00:00:01Z")
    return store


def test_Codex回合按terra实价折算_量在费用汇总返回值(tmp_path: Path) -> None:
    from app.observability.cost import llm_cost_summary
    from app.observability.pricing import priced_usage

    store = _store(tmp_path)
    usage, source = priced_usage("Codex", CODEX_TURN)
    store.record_chapter_usage("r-terra", "goal-1", "ch-2", usage,
                               engine="codex", cost_source=source)

    summary = llm_cost_summary(store, "r-terra")
    # 旧 GPT-5 系折算 1.25 + 0.125 + 10 = 11.375；terra 实价 2.00 + 0.20 + 12.00 = 14.20
    assert summary["estimated_cost_usd"] == pytest.approx(14.20)
    assert summary["by_engine"]["codex"]["estimated_cost_usd"] == pytest.approx(14.20)


def test_Codex缓存写入按2点5计_且不和新输入重复计价() -> None:
    from app.observability.pricing import estimate_cost_usd

    usage = {**CODEX_TURN, "input_tokens": 3_000_000, "cache_write_input_tokens": 1_000_000}
    # 新输入 1M × 2 + 缓存读 1M × 0.2 + 缓存写 1M × 2.5 + 输出 1M × 12
    assert estimate_cost_usd("codex", usage) == pytest.approx(16.70)


def test_型号表按引擎生效_计划型号对不上实际引擎就回落引擎默认价() -> None:
    from app.observability.pricing import ENGINE_PRICES, MODEL_PRICES, estimate_cost_usd

    assert MODEL_PRICES["gpt-5.6-terra"] is ENGINE_PRICES["codex"]
    # Codex 事件不带型号，传进来的是计划里的型号：Claude 型号 / generated / 未知 gpt 型号都回落 terra
    for model in ("claude-opus-4-7", "generated", "gpt-9-unknown", None):
        assert estimate_cost_usd("codex", CODEX_TURN, model=model) == pytest.approx(14.20)
    # 反过来：计划写了 terra、实际让路给 Claude 跑，不能拿 terra 价折 Claude
    claude = {**CODEX_TURN, "input_tokens": 1_000_000}
    assert estimate_cost_usd("claude", claude, model="gpt-5.6-terra") == pytest.approx(
        estimate_cost_usd("claude", claude)
    )


def test_标价表带型号来源与生效日期_费用接口带出(tmp_path: Path) -> None:
    from app.observability.cost import research_cost_summary
    from app.observability.pricing import price_table_note

    note = price_table_note()
    assert research_cost_summary(_store(tmp_path), "r-terra")["llm"]["price_table"] == note
    assert note["engines"]["codex"]["model"] == "gpt-5.6-terra"
    assert note["engines"]["codex"]["effective_from"] == "2026-07-30"
    assert any("gpt-5.6-terra" in url for url in note["engines"]["codex"]["sources"])
