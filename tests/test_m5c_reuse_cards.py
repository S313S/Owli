from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path

import httpx
import pytest

from app.api.main import create_app
from app.plan.store import load_plan
from app.orchestrator.runtime import _replace_reused_subjects
from app.store.dao import Store
from app.store.recall import RecallCandidate, RecallMatch, RecallResult
from tests.plan_factory import make_plan_dict


SCHEMA_PATH = Path(__file__).resolve().parents[1] / "app" / "store" / "schema.sql"


def _source_plan(source_id: str) -> dict:
    plan = make_plan_dict()
    plan["research_id"] = source_id
    plan["status"] = "completed"
    plan["approved_at"] = "2026-08-25T10:30:00+00:00"
    plan["market_profile_justification"] = "旧题目的市场说明哨兵"
    plan["subjects"] = ["旧题目的研究实体哨兵", "旧题目的第二实体哨兵"]
    plan["decision_balance"] = [{
        "q_id": "q-history",
        "question": "旧题目的研究实体哨兵 vs 旧题目的第二实体哨兵优先服务哪类判断？",
        "options": ["产品路线", "市场话术"],
        "input_type": "single",
        "answer": "产品路线",
        "affects": ["goal-1", "goal-2"],
        "answered_at": "2026-08-25T10:20:00+00:00",
    }]
    for goal_index, goal in enumerate(plan["goals"]):
        goal["title"] = (
            f"历史方法阶段 {goal_index + 1} · "
            "旧题目的研究实体哨兵与旧题目的第二实体哨兵"
        )
        goal["objective"] = (
            "围绕旧题目的研究实体哨兵与旧题目的第二实体哨兵整理可复核的方法证据。"
        )
        goal["deliverable"]["description"] = "旧题目的研究实体哨兵方法产物。"
        goal["acceptance"] = ["旧题目的研究实体哨兵至少有 3 条可追溯记录"]
        goal["intervention"]["prompt"] = "请核对旧题目的研究实体哨兵方法产物，是否继续？"
        for agent in goal["agents"]:
            agent["task"] = (
                "采集旧题目的研究实体哨兵与旧题目的第二实体哨兵的方法证据。"
            )
            agent["prompt"]["body"] = "查询旧题目的研究实体哨兵并按来源交叉核对。"
            if goal_index == 0:
                agent["entity"] = "旧题目的采集实体哨兵"
                agent["chapter"]["chapter_type"] = "collection"
                agent["chapter"]["closing"]["entities"] = ["旧题目的采集实体哨兵"]
            else:
                agent["entity"] = "旧题目的 Agent 实体哨兵"
                agent["chapter"]["closing"]["entities"] = ["旧题目的章节实体哨兵"]
            agent["chapter"]["closing"]["notes"] = {"legacy": "旧题目的章节哨兵"}
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


def test_实体替换不会再次改写当前题目中的同名实体() -> None:
    assert _replace_reused_subjects(
        "Notion vs Obsidian 的方法对比",
        ["Notion", "Obsidian"],
        "Notion vs Logseq",
    ) == "Notion vs Logseq 的方法对比"
    assert _replace_reused_subjects(
        "汇集「Notion」与「Obsidian」的官方定位",
        ["Notion", "Obsidian"],
        "Coda vs Logseq",
    ) == "汇集「Coda vs Logseq」的官方定位"
    assert _replace_reused_subjects(
        "历史计划未声明实体",
        [],
        "Notion vs Logseq",
    ) == "历史计划未声明实体"


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
                assert "similar" not in data
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
                assert reused_plan.use_case == "other"
                assert reused_plan.subjects == []
                assert reused_plan.market_profile_justification == (
                    "沿用同一研究事项历史计划的市场范围配置，用户需在计划编辑器核对。"
                )
                assert [goal.title for goal in reused_plan.goals] == [
                    "历史方法阶段 1 · 旧题目的研究实体哨兵与旧题目的第二实体哨兵",
                    "历史方法阶段 2 · 旧题目的研究实体哨兵与旧题目的第二实体哨兵",
                    "历史方法阶段 3 · 旧题目的研究实体哨兵与旧题目的第二实体哨兵",
                ]
                for goal in reused_plan.goals:
                    method_fields = [
                        goal.objective,
                        goal.deliverable["description"],
                        *goal.acceptance,
                        goal.intervention["prompt"],
                    ]
                    assert all("比较两个编码 Agent" in value for value in method_fields)
                    assert all(value.count("比较两个编码 Agent") == 1 for value in method_fields)
                    assert all("旧题目的研究实体哨兵" not in value for value in method_fields)
                    assert all("旧题目的第二实体哨兵" not in value for value in method_fields)
                    assert goal.status == "pending"
                    for agent in goal.agents:
                        assert "比较两个编码 Agent" in agent.task
                        assert agent.task.count("比较两个编码 Agent") == 1
                        assert "旧题目的研究实体哨兵" not in agent.task
                        assert "旧题目的第二实体哨兵" not in agent.task
                        assert "比较两个编码 Agent" in agent.prompt["body"]
                        assert "旧题目的研究实体哨兵" not in agent.prompt["body"]
                        assert "只复用方法与来源配置，不沿用旧报告结论" in agent.prompt["body"]
                        assert agent.chapter["closing"]["notes"] == {}
                        assert set(agent.origin.values()) == {"generated"}
                        assert agent.status == "queued"
                assert reused_plan.decision_balance == [{
                    "q_id": "q-history",
                    "question": "比较两个编码 Agent优先服务哪类判断？",
                    "options": ["产品路线", "市场话术"],
                    "input_type": "single",
                    "answer": None,
                    "affects": ["goal-1", "goal-2"],
                    "answered_at": None,
                }]
                first_agent = reused_plan.goals[0].agents[0]
                assert first_agent.entity == "比较两个编码 Agent"
                assert first_agent.chapter["closing"]["entities"] == ["比较两个编码 Agent"]
                for goal in reused_plan.goals[1:]:
                    for agent in goal.agents:
                        assert agent.entity is None
                        assert agent.chapter["closing"]["entities"] == []

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


def test_历史选择已很快回答时仍保留人工审核闸门(
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
        prepare_entered = asyncio.Event()
        prepare_release = asyncio.Event()
        original_prepare = application.state.runtime.prepare_research

        async def delayed_prepare(research_id: str, query: str, *, scale: str):
            prepare_entered.set()
            await prepare_release.wait()
            return await original_prepare(research_id, query, scale=scale)

        application.state.runtime.prepare_research = delayed_prepare

        async with application.router.lifespan_context(application):
            transport = httpx.ASGITransport(app=application)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                created = await client.post(
                    "/api/researches",
                    json={"query": "快速选择新建也必须人工审核"},
                    headers={"X-Request-ID": "m5c-fast-new-guard"},
                )
                research_id = created.json()["data"]["research_id"]
                await recall.started.wait()
                recall.release.set()
                await _wait_until(lambda: bool(application.state.researches[research_id]["cards"]))
                await prepare_entered.wait()
                card = application.state.researches[research_id]["cards"][0]
                answered = await client.post(
                    f"/api/cards/{card['card_id']}/respond",
                    json={"action": "new", "payload": {"choice": "new"}},
                    headers={"X-Request-ID": "m5c-fast-new-choice"},
                )
                assert answered.status_code == 200, answered.text
                prepare_release.set()
                await _wait_until(
                    lambda: application.state.researches[research_id]["status"] == "awaiting_review"
                )
                state = application.state.researches[research_id]
                questions = [
                    item for item in state["cards"]
                    if item["card_type"] == "QUESTION"
                ]
                assert questions and questions[0]["status"] == "pending"
                assert state["status"] == "awaiting_review"

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
