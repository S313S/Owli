from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path

import httpx
import pytest

from app.api.main import create_app
from app.plan.store import load_plan
from app.store.dao import Store
from app.store.recall import RecallCandidate, RecallMatch, RecallResult
from tests.plan_factory import make_plan_dict


SCHEMA_PATH = Path(__file__).resolve().parents[1] / "app" / "store" / "schema.sql"


def _source_plan(source_id: str) -> dict:
    plan = make_plan_dict()
    plan["research_id"] = source_id
    plan["status"] = "completed"
    plan["approved_at"] = "2026-08-25T10:30:00+00:00"
    return plan


def _database(path: Path, source_id: str = "r-history-plan") -> Path:
    with sqlite3.connect(path) as connection:
        connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    store = Store(path)
    store.create_report(
        id=source_id,
        title="OpenAI vs Claude Code",
        research_question="比较 OpenAI 与 Claude Code 的开发体验",
        created_at="2026-08-25T10:00:00+00:00",
        completed_at="2026-08-25T10:30:00+00:00",
        status="completed",
        summary_line="两种编码 Agent 的能力与工作流比较。",
        plan_snapshot=_source_plan(source_id),
    )
    return path


def _recall_result(source_id: str = "r-history-plan", *, degraded: bool = False) -> RecallResult:
    candidate = RecallCandidate(
        report_id=source_id,
        title="OpenAI vs Claude Code",
        research_question="比较 OpenAI 与 Claude Code 的开发体验",
        summary_line="两种编码 Agent 的能力与工作流比较。",
        tags=(),
        sources=("hacker_news", "web_search"),
        completed_at="2026-08-25T10:30:00+00:00",
        bm25_score=-8.5,
        keyword_score=8.5,
    )
    match = RecallMatch(
        candidate=candidate,
        same_item=None if degraded else True,
        confidence=None if degraded else "高",
        reason=(
            "主引擎不可用，结果未经语义判断。"
            if degraded
            else "研究对象与比较目标一致，可复用报告骨架。"
        ),
        reusable_elements=() if degraded else ("报告骨架",),
        match_label="关键词粗匹配" if degraded else "主引擎语义判断",
    )
    return RecallResult(
        query_mode="fts5_bm25",
        candidates=(candidate,),
        matches=(match,),
        degraded=degraded,
        degrade_reason="ConnectionError: 断连" if degraded else None,
    )


class ControlledRecall:
    def __init__(self, result: RecallResult) -> None:
        self.result = result
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = asyncio.Event()

    async def recall(self, query: str) -> RecallResult:
        assert query
        self.started.set()
        await self.release.wait()
        self.finished.set()
        return self.result


async def _wait_until(predicate, *, rounds: int = 100) -> None:
    for _ in range(rounds):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("异步状态未在限定轮次内出现")


def test_创建研究立即返回且真实候选通过_SSE_卡片后到(tmp_path: Path) -> None:
    async def scenario() -> None:
        recall = ControlledRecall(_recall_result())
        application = create_app(
            _database(tmp_path / "owli.db"),
            SCHEMA_PATH,
            engine_probe=lambda: {},
            recall_service=recall,
        )

        prepare_started = asyncio.Event()

        async def prepare(research_id: str, query: str, *, scale: str):
            del research_id, query, scale
            prepare_started.set()

        application.state.runtime.prepare_research = prepare

        async with application.router.lifespan_context(application):
            transport = httpx.ASGITransport(app=application)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                started_at = time.perf_counter()
                response = await asyncio.wait_for(
                    client.post(
                        "/api/researches",
                        json={"query": "OpenAI 与 Claude Code 哪个更适合开发"},
                        headers={"X-Request-ID": "m5c-create-async"},
                    ),
                    timeout=0.25,
                )
                elapsed = time.perf_counter() - started_at

                assert response.status_code == 200, response.text
                assert elapsed < 0.25
                data = response.json()["data"]
                assert data["similar"] == []
                assert data["recall_status"] == "pending"
                research_id = data["research_id"]

                await asyncio.wait_for(recall.started.wait(), timeout=0.25)
                assert not prepare_started.is_set(), "历史检查完成前不该生成全新计划"
                recall.release.set()
                await asyncio.wait_for(recall.finished.wait(), timeout=0.25)
                await _wait_until(lambda: bool(application.state.researches[research_id]["cards"]))

                card = application.state.researches[research_id]["cards"][0]
                assert card["card_type"] == "HISTORY_REUSE"
                assert card["blocking"] == "research"
                assert "更快、已验证" in card["body"]
                assert card["target"]["match_label"] == "主引擎语义判断"
                assert [(item["type"], item["id"]) for item in card["actions"]] == [
                    ("CHOICE_2", "reuse"),
                    ("CHOICE_2", "new"),
                ]
                await asyncio.wait_for(prepare_started.wait(), timeout=0.25)

    asyncio.run(scenario())


def test_关键词粗匹配由后端标记原样进入卡片(tmp_path: Path) -> None:
    async def scenario() -> None:
        recall = ControlledRecall(_recall_result(degraded=True))
        application = create_app(
            _database(tmp_path / "owli.db"),
            SCHEMA_PATH,
            engine_probe=lambda: {},
            recall_service=recall,
        )
        application.state.runtime.prepare_research = lambda *args, **kwargs: None

        async with application.router.lifespan_context(application):
            transport = httpx.ASGITransport(app=application)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    "/api/researches",
                    json={"query": "OpenAI 与 Claude Code"},
                    headers={"X-Request-ID": "m5c-degraded"},
                )
                research_id = response.json()["data"]["research_id"]
                await recall.started.wait()
                recall.release.set()
                await _wait_until(lambda: bool(application.state.researches[research_id]["cards"]))
                card = application.state.researches[research_id]["cards"][0]
                assert card["target"]["match_label"] == "关键词粗匹配"

    asyncio.run(scenario())


def test_复用分支落成历史计划模板_新建分支才启动规划(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = _database(tmp_path / "owli.db")
        recall = ControlledRecall(_recall_result())
        application = create_app(
            database,
            SCHEMA_PATH,
            engine_probe=lambda: {},
            recall_service=recall,
        )
        prepared: list[str] = []

        async def prepare(
            research_id: str,
            query: str,
            *,
            scale: str,
        ):
            del query, scale
            prepared.append(research_id)

        application.state.runtime.prepare_research = prepare

        async with application.router.lifespan_context(application):
            transport = httpx.ASGITransport(app=application)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                reuse_create = await client.post(
                    "/api/researches",
                    json={"query": "比较两个编码 Agent"},
                    headers={"X-Request-ID": "m5c-reuse-create"},
                )
                reuse_id = reuse_create.json()["data"]["research_id"]
                await recall.started.wait()
                recall.release.set()
                await _wait_until(lambda: bool(application.state.researches[reuse_id]["cards"]))
                reuse_card = application.state.researches[reuse_id]["cards"][0]
                reuse_response = await client.post(
                    f"/api/cards/{reuse_card['card_id']}/respond",
                    json={"action": "reuse", "payload": {"choice": "reuse"}},
                    headers={"X-Request-ID": "m5c-reuse-choice"},
                )
                assert reuse_response.status_code == 200, reuse_response.text
                reused_plan = load_plan(Store(database), reuse_id)
                assert reused_plan is not None
                assert reused_plan.baseline_source == "reused:r-history-plan"
                assert reused_plan.research_question == "比较两个编码 Agent"
                assert "飞书" not in reused_plan.to_json()

                second_recall = ControlledRecall(_recall_result())
                application.state.recall_service = second_recall
                # create_app 闭包读取同一个服务对象；就地切结果，避免重建服务与数据库。
                recall.started = asyncio.Event()
                recall.release = asyncio.Event()
                recall.finished = asyncio.Event()
                new_create = await client.post(
                    "/api/researches",
                    json={"query": "坚持生成一份全新计划"},
                    headers={"X-Request-ID": "m5c-new-create"},
                )
                new_id = new_create.json()["data"]["research_id"]
                await recall.started.wait()
                recall.release.set()
                await _wait_until(lambda: bool(application.state.researches[new_id]["cards"]))
                await _wait_until(lambda: new_id in prepared)
                new_card = application.state.researches[new_id]["cards"][0]
                new_response = await client.post(
                    f"/api/cards/{new_card['card_id']}/respond",
                    json={"action": "new", "payload": {"choice": "new"}},
                    headers={"X-Request-ID": "m5c-new-choice"},
                )
                assert new_response.status_code == 200, new_response.text
                assert prepared.count(new_id) == 1
                assert load_plan(Store(database), new_id) is None

    asyncio.run(scenario())


def test_两个标签页同时复用只允许一个请求落盘(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = _database(tmp_path / "owli.db")
        recall = ControlledRecall(_recall_result())
        application = create_app(
            database,
            SCHEMA_PATH,
            engine_probe=lambda: {},
            recall_service=recall,
        )
        prepared = asyncio.Event()

        async def prepare(
            research_id: str,
            query: str,
            *,
            scale: str,
        ) -> None:
            del research_id, query, scale
            prepared.set()

        application.state.runtime.prepare_research = prepare

        async with application.router.lifespan_context(application):
            transport = httpx.ASGITransport(app=application)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                created = await client.post(
                    "/api/researches",
                    json={"query": "并发选择复用"},
                    headers={"X-Request-ID": "m5c-concurrent-create"},
                )
                research_id = created.json()["data"]["research_id"]
                await recall.started.wait()
                recall.release.set()
                await _wait_until(lambda: bool(application.state.researches[research_id]["cards"]))
                card_id = application.state.researches[research_id]["cards"][0]["card_id"]

                responses = await asyncio.gather(*[
                    client.post(
                        f"/api/cards/{card_id}/respond",
                        json={"action": "reuse", "payload": {"choice": "reuse"}},
                        headers={"X-Request-ID": f"m5c-concurrent-{index}"},
                    )
                    for index in range(2)
                ])

                assert sorted(item.status_code for item in responses) == [200, 409]
                await asyncio.wait_for(prepared.wait(), timeout=0.25)
                assert load_plan(Store(database), research_id) is not None

    asyncio.run(scenario())


def test_无人值守模式也不会越过待选历史卡自动批准(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        database = _database(tmp_path / "owli.db")
        recall = ControlledRecall(_recall_result())

        async def generated(query, store, adapter, **kwargs):
            del adapter, kwargs
            report = store.get_drafting_report(query)
            raw = _source_plan(str(report["id"]))
            raw.update({
                "title": query,
                "research_question": query,
                "status": "awaiting_review",
                "approved_at": None,
                "baseline": None,
            })
            for question in raw["decision_balance"]:
                question["answer"] = None
                question["answered_at"] = None
            from app.plan.model import Plan
            return Plan.from_dict(raw)

        monkeypatch.setattr("app.orchestrator.runtime.generate_plan", generated)
        application = create_app(
            database,
            SCHEMA_PATH,
            engine_probe=lambda: {},
            recall_service=recall,
            auto_confirm=True,
        )

        async with application.router.lifespan_context(application):
            transport = httpx.ASGITransport(app=application)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                created = await client.post(
                    "/api/researches",
                    json={"query": "有历史候选时不可自动批准"},
                    headers={"X-Request-ID": "m5c-auto-confirm-guard"},
                )
                research_id = created.json()["data"]["research_id"]
                await recall.started.wait()
                recall.release.set()
                await _wait_until(
                    lambda: application.state.researches[research_id]["status"]
                    == "awaiting_review"
                )
                state = application.state.researches[research_id]
                history = [
                    item for item in state["cards"]
                    if item["card_type"] == "HISTORY_REUSE"
                ]
                questions = [
                    item for item in state["cards"]
                    if item["card_type"] == "QUESTION"
                ]
                assert history and history[0]["status"] == "pending"
                assert questions and questions[0]["status"] == "pending"
                assert state["status"] == "awaiting_review"

    asyncio.run(scenario())
