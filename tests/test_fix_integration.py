"""§FIX：节化墙钟与适配器证据入库的集成回归。"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from tests.test_m3h_ledger import _store


def _capability(source_id: str):
    from app.adapters.capability import Capability

    return Capability(
        tools=(f"source.{source_id}",),
        sources=(source_id,),
        network="sources_only",
    )


def test_source桥按签名透传Store身份与新源limit() -> None:
    from app.adapters.source_mcp import SourceToolAdapter

    received: dict[str, object] = {}
    store = object()

    def fake_xhs(
        query, window, *, limit, store, report_id, goal_id, on_event=None,
    ):
        received.update(
            query=query, window=window, limit=limit, store=store,
            report_id=report_id, goal_id=goal_id, on_event=on_event,
        )
        return []

    adapter = SourceToolAdapter({"source.xhs": fake_xhs}, store=store)
    result = asyncio.run(adapter.call(
        "source.xhs", "飞书", "30d",
        research_id="r-fix", goal_id="goal-1", agent_id="collector",
        capability=_capability("xhs"), item_limit=17,
    ))

    assert result == []
    assert received["store"] is store
    assert received["report_id"] == "r-fix"
    assert received["goal_id"] == "goal-1"
    assert received["limit"] == 17


def test_不接收Store的源也按适配器返回体即时入库(tmp_path: Path) -> None:
    from app.adapters.source_mcp import SourceToolAdapter

    store = _store(tmp_path)

    def fake_web_search(query, window, *, max_results):
        del query, window
        assert max_results == 9
        return [{
            "platform": "web_search",
            "platform_item_id": "page-1",
            "permalink": "https://example.com/page-1",
            "fetched_at": "2026-08-27T00:00:00+00:00",
            "title": "真实标题",
            "content_excerpt": "真实摘录",
            "author_name": "真实作者",
            "raw_metrics": {"score": 8},
        }]

    adapter = SourceToolAdapter(
        {"source.web_search": fake_web_search}, store=store,
    )
    asyncio.run(adapter.call(
        "source.web_search", "飞书", "30d",
        research_id="r-ledger", goal_id="goal-1", agent_id="collector",
        capability=_capability("web_search"), item_limit=9,
    ))

    rows = store.list_evidence("r-ledger")
    assert len(rows) == 1
    assert rows[0]["agent_name"] == "collector"
    assert rows[0]["title"] == "真实标题"
    assert rows[0]["raw_metrics"] == {"score": 8}
    assert rows[0]["rated_by"] == "baseline:web_search@v1"


def test_规划器换措辞仍归一四字段且不覆盖适配器真值(tmp_path: Path) -> None:
    from app.orchestrator.runtime import RuntimeCoordinator
    from app.store.evidence_artifacts import load_evidence_payloads

    store = _store(tmp_path)
    permalink = "https://www.xiaohongshu.com/explore/note-1"
    store.add_evidence(
        id="ev-adapter", report_id="r-ledger", goal_id="goal-1",
        agent_name=None, platform="xhs", platform_item_id="note-1",
        permalink=permalink, fetched_at="2026-08-27T00:00:00+00:00",
        title="适配器标题", content_excerpt="适配器正文",
        author_name="适配器作者", raw_metrics={"liked_count": 88},
    )
    runs_root = tmp_path / "runs"
    artifact = runs_root / "r-ledger/goals/goal-1/evidence.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text(json.dumps([
        {
            "platform": "xhs", "platform_item_id": "note-1",
            "permalink": permalink,
            "fetched_at": "2026-08-27T00:01:00+00:00",
            "text": "规划器改名后的正文", "author": "规划器作者",
            "like_count": 7, "comment_count": 3,
        },
        {
            "platform": "xhs", "platform_item_id": "empty",
            "permalink": "https://www.xiaohongshu.com/explore/empty",
            "fetched_at": "2026-08-27T00:01:00+00:00",
        },
    ], ensure_ascii=False), encoding="utf-8")

    payloads = load_evidence_payloads(
        artifact, report_id="r-ledger", goal_id="goal-1",
        agent_name="data-collection-xhs", platform_hint="xhs",
    )
    assert len(payloads) == 1, "四字段全空的证据行必须拒收"
    assert all(payloads[0][field] for field in (
        "title", "content_excerpt", "author_name", "raw_metrics",
    ))

    runtime = RuntimeCoordinator(
        store=store, event_buffer=SimpleNamespace(), researches={}, cards={},
        adapter_factory=lambda: object(), runs_root=runs_root,
        routing_utc_clock=lambda: datetime.now(timezone.utc),
    )
    goal = SimpleNamespace(
        goal_id="goal-1",
        agents=[SimpleNamespace(
            agent_id="data-collection-xhs",
            output={"format": "json", "path": "goals/goal-1/evidence.json"},
            capability={"sources": ["xhs"]},
        )],
    )
    store.ensure_chapters(
        "r-ledger", [{"goal_id": "goal-1", "chapter_id": "data-collection-xhs"}],
        updated_at="2026-08-27T00:00:00Z",
    )
    store.finish_chapter(
        "r-ledger", "goal-1", "data-collection-xhs",
        status="done", reason=None, actual_output_path=str(artifact), actual_count=2,
        updated_at="2026-08-27T00:00:01Z",
    )
    runtime._persist_goal_evidence(SimpleNamespace(research_id="r-ledger"), goal)

    rows = store.list_evidence("r-ledger")
    assert len(rows) == 1
    assert rows[0]["title"] == "适配器标题"
    assert rows[0]["content_excerpt"] == "适配器正文"
    assert rows[0]["author_name"] == "适配器作者"
    assert rows[0]["raw_metrics"] == {"liked_count": 88}


def test_产物存在但章账本未_done_时不提前投影(tmp_path: Path) -> None:
    from app.orchestrator.runtime import RuntimeCoordinator

    store = _store(tmp_path)
    runs_root = tmp_path / "runs"
    artifact = runs_root / "r-ledger/goals/goal-1/evidence.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text(json.dumps([{
        "platform": "web_search",
        "permalink": "https://example.com/pending",
        "fetched_at": "2026-08-27T00:00:00+00:00",
        "title": "未完成产物",
    }], ensure_ascii=False), encoding="utf-8")
    store.ensure_chapters(
        "r-ledger", [{"goal_id": "goal-1", "chapter_id": "data-collection"}],
        updated_at="2026-08-27T00:00:00Z",
    )
    runtime = RuntimeCoordinator(
        store=store, event_buffer=SimpleNamespace(), researches={}, cards={},
        adapter_factory=lambda: object(), runs_root=runs_root,
        routing_utc_clock=lambda: datetime.now(timezone.utc),
    )
    goal = SimpleNamespace(
        goal_id="goal-1",
        agents=[SimpleNamespace(
            agent_id="data-collection",
            chapter={"chapter_id": "data-collection"},
            output={"format": "json", "path": "goals/goal-1/evidence.json"},
            capability={"sources": ["web_search"]},
        )],
    )

    runtime._persist_goal_evidence(SimpleNamespace(research_id="r-ledger"), goal)

    assert store.list_evidence("r-ledger") == []


def test_MCP配置携带局部Store路径且三新源都映射limit(tmp_path: Path) -> None:
    from app.adapters.source_mcp import SourceToolAdapter, stdio_server_config

    config = stdio_server_config(
        ("xhs",), store_path=tmp_path / "owli.db", environ={},
    )
    assert config["args"][-2:] == ["--store-path", str(tmp_path / "owli.db")]

    for source_id in ("xhs", "douyin", "reddit"):
        received: list[int] = []

        def source(query, window, *, limit):
            del query, window
            received.append(limit)
            return []

        adapter = SourceToolAdapter({f"source.{source_id}": source})
        asyncio.run(adapter.call(
            f"source.{source_id}", "查询", "30d",
            research_id="r", goal_id="goal-1", agent_id="collector",
            capability=_capability(source_id), item_limit=23,
        ))
        assert received == [23]
