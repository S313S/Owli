from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "app" / "store" / "schema.sql"


def _database(tmp_path: Path) -> Path:
    database = tmp_path / "owli.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    return database


def _report(
    database: Path,
    report_id: str,
    title: str,
    *,
    question: str | None = None,
    summary_line: str = "全部 goal 已完成",
    completed_at: str = "2026-08-25T10:00:00+00:00",
) -> None:
    from app.store.dao import Store

    store = Store(database)
    store.create_report(
        id=report_id,
        title=title,
        research_question=question or title,
        created_at="2026-08-25T09:00:00+00:00",
        status="completed",
        completed_at=completed_at,
        summary_line=summary_line,
    )
    assert store.backfill_recall_index() >= 1


def test_长查询走trigram_or并按bm25返回top_n(tmp_path: Path) -> None:
    from app.store.recall import RecallRepository

    database = _database(tmp_path)
    _report(database, "r-openai", "OpenAI vs Claude Code")
    _report(database, "r-tea", "茶叶品牌小红书声量分析")

    result = RecallRepository(database).search(
        "想比较 Claude Code 和 OpenAI 的差异", top_n=1
    )

    assert result.query_mode == "fts5_bm25"
    assert [item.report_id for item in result.candidates] == ["r-openai"]
    assert result.candidates[0].bm25_score is not None
    assert result.candidates[0].bm25_score < 0


def test_fts命中不足top_n时补齐completed报告供llm判重(tmp_path: Path) -> None:
    from app.store.recall import RecallRepository

    database = _database(tmp_path)
    _report(database, "r-openai", "OpenAI vs Claude Code")
    _report(database, "r-tea", "茶叶品牌小红书声量分析")

    result = RecallRepository(database).search(
        "OpenAI 与 Claude Code 的开发体验", top_n=3
    )

    assert [item.report_id for item in result.candidates] == [
        "r-openai", "r-tea"
    ]
    assert result.candidates[0].bm25_score is not None
    assert result.candidates[1].bm25_score is None
    assert result.candidates[1].keyword_score == 0


def test_两字查询固定走like且trigram原路径为零(tmp_path: Path) -> None:
    from app.store.recall import RecallRepository

    database = _database(tmp_path)
    _report(
        database,
        "r-feishu",
        "飞书协同办公产品竞品分析",
        question="飞书有哪些值得复用的自动化能力？",
    )
    with sqlite3.connect(database) as connection:
        trigram_count = connection.execute(
            "SELECT count(*) FROM recall_fts WHERE recall_fts MATCH ?",
            ('"飞书"',),
        ).fetchone()[0]

    result = RecallRepository(database).search("飞书", top_n=3)

    assert trigram_count == 0
    assert result.query_mode == "like"
    assert [item.report_id for item in result.candidates] == ["r-feishu"]
    assert result.candidates[0].bm25_score is None
    assert result.candidates[0].keyword_score > 0


def test_like转义百分号与下划线不会意外全表命中(tmp_path: Path) -> None:
    from app.store.recall import RecallRepository

    database = _database(tmp_path)
    _report(database, "r-any", "任意历史报告")

    assert RecallRepository(database).search("%_", top_n=3).candidates == ()


def test_fts零真实命中时跳过主引擎判重(tmp_path: Path) -> None:
    from app.store.recall import RecallRepository, RecallService

    database = _database(tmp_path)
    _report(database, "r-tea", "茶叶品牌小红书声量分析")
    judge_calls = []

    async def judge(query, candidates):
        judge_calls.append((query, candidates))
        return ()

    result = asyncio.run(
        RecallService(RecallRepository(database), judge=judge).recall(
            "OpenAI 与 Claude Code 开发工具对比"
        )
    )

    assert result.query_mode == "fts5_bm25"
    assert len(result.candidates) == 1
    assert all(item.bm25_score is None for item in result.candidates)
    assert judge_calls == []
    assert result.matches == ()
    assert result.degraded is False
    assert result.degrade_reason is None


def test_fts有真实bm25命中时仍调用主引擎判重(tmp_path: Path) -> None:
    from app.store.recall import DuplicateDecision, RecallRepository, RecallService

    database = _database(tmp_path)
    _report(database, "r-openai", "OpenAI vs Claude Code")
    judge_calls = []

    async def judge(query, candidates):
        judge_calls.append((query, candidates))
        return (
            DuplicateDecision(
                report_id=candidates[0].report_id,
                same_item=True,
                confidence="高",
                reason="开发工具与对比目标一致。",
                reusable_elements=("报告骨架",),
            ),
        )

    result = asyncio.run(
        RecallService(RecallRepository(database), judge=judge).recall(
            "OpenAI 与 Claude Code 开发工具对比"
        )
    )

    assert result.query_mode == "fts5_bm25"
    assert any(item.bm25_score is not None for item in result.candidates)
    assert len(judge_calls) == 1
    assert len(result.matches) == 1
    assert result.degraded is False


def test_like真实命中时仍调用主引擎判重(tmp_path: Path) -> None:
    from app.store.recall import DuplicateDecision, RecallRepository, RecallService

    database = _database(tmp_path)
    _report(database, "r-feishu", "飞书协同办公产品竞品分析")
    judge_calls = []

    async def judge(query, candidates):
        judge_calls.append((query, candidates))
        return (
            DuplicateDecision(
                report_id=candidates[0].report_id,
                same_item=True,
                confidence="高",
                reason="飞书研究对象一致。",
                reusable_elements=("报告骨架",),
            ),
        )

    result = asyncio.run(
        RecallService(RecallRepository(database), judge=judge).recall("飞书")
    )

    assert result.query_mode == "like"
    assert result.candidates[0].bm25_score is None
    assert result.candidates[0].keyword_score > 0
    assert len(judge_calls) == 1
    assert len(result.matches) == 1
    assert result.degraded is False


def test_主引擎判重保留正反结论与理由(tmp_path: Path) -> None:
    from app.store.recall import DuplicateDecision, RecallRepository, RecallService

    database = _database(tmp_path)
    _report(database, "r-openai", "OpenAI vs Claude Code")
    _report(database, "r-tea", "茶叶品牌小红书声量分析")

    async def judge(query, candidates):
        assert query == "对比 OpenAI 与 Claude Code 的开发体验"
        assert [item.report_id for item in candidates] == ["r-openai", "r-tea"]
        return (
            DuplicateDecision(
                report_id="r-openai",
                same_item=True,
                confidence="高",
                reason="比较对象与决策目标一致，可复用原有对比框架。",
                reusable_elements=("报告骨架", "采集方式"),
            ),
            DuplicateDecision(
                report_id="r-tea",
                same_item=False,
                confidence="高",
                reason="研究对象与决策目标均不同，不能复用。",
                reusable_elements=(),
            ),
        )

    result = asyncio.run(
        RecallService(RecallRepository(database), judge=judge).recall(
            "对比 OpenAI 与 Claude Code 的开发体验"
        )
    )

    assert result.degraded is False
    assert result.degrade_reason is None
    assert len(result.matches) == 2
    assert result.matches[0].same_item is True
    assert result.matches[0].reason == "比较对象与决策目标一致，可复用原有对比框架。"
    assert result.matches[0].match_label == "主引擎语义判断"
    assert result.matches[1].same_item is False
    assert result.matches[1].reason == "研究对象与决策目标均不同，不能复用。"


def test_主引擎空判重产物按结构化失败退化(tmp_path: Path) -> None:
    from app.store.recall import RecallRepository, RecallService

    database = _database(tmp_path)
    _report(database, "r-openai", "OpenAI vs Claude Code")

    async def empty(query, candidates):
        del query, candidates
        return ()

    result = asyncio.run(
        RecallService(RecallRepository(database), judge=empty).recall(
            "OpenAI 与 Claude Code 竞品对比"
        )
    )

    assert result.degraded is True
    assert result.matches[0].match_label == "关键词粗匹配"
    assert "至少返回一条" in (result.degrade_reason or "")


def test_主引擎超时不阻塞并退化为关键词粗匹配(tmp_path: Path) -> None:
    from app.store.recall import RecallRepository, RecallService

    database = _database(tmp_path)
    _report(database, "r-openai", "OpenAI vs Claude Code")

    async def hanging(query, candidates):
        del query, candidates
        await asyncio.Event().wait()

    result = asyncio.run(
        RecallService(
            RecallRepository(database),
            judge=hanging,
            judge_timeout_seconds=0.01,
        ).recall("OpenAI 与 Claude Code 竞品对比")
    )

    assert result.degraded is True
    assert result.matches[0].match_label == "关键词粗匹配"
    assert "超过 0.01 秒" in (result.degrade_reason or "")


def test_主引擎断连时不报错并退化为bm25粗匹配(tmp_path: Path) -> None:
    from app.store.recall import RecallRepository, RecallService

    database = _database(tmp_path)
    _report(database, "r-openai", "OpenAI vs Claude Code")
    _report(database, "r-tea", "茶叶品牌小红书声量分析")

    async def disconnected(query, candidates):
        del query, candidates
        raise ConnectionError("simulated disconnect")

    result = asyncio.run(
        RecallService(RecallRepository(database), judge=disconnected).recall(
            "OpenAI 与 Claude Code 竞品对比"
        )
    )

    assert result.query_mode == "fts5_bm25"
    assert result.degraded is True
    assert result.degrade_reason == "ConnectionError: simulated disconnect"
    assert [item.candidate.report_id for item in result.matches] == ["r-openai"]
    assert result.matches[0].same_item is None
    assert result.matches[0].match_label == "关键词粗匹配"
    assert result.matches[0].candidate.bm25_score is not None


def test_主引擎返回粗筛外id时按结构化失败退化(tmp_path: Path) -> None:
    from app.store.recall import DuplicateDecision, RecallRepository, RecallService

    database = _database(tmp_path)
    _report(database, "r-openai", "OpenAI vs Claude Code")

    async def invalid(query, candidates):
        del query, candidates
        return (
            DuplicateDecision(
                report_id="r-outside",
                same_item=False,
                confidence="低",
                reason="越界候选",
                reusable_elements=(),
            ),
        )

    result = asyncio.run(
        RecallService(RecallRepository(database), judge=invalid).recall(
            "OpenAI 与 Claude Code 竞品对比"
        )
    )

    assert result.degraded is True
    assert result.matches[0].match_label == "关键词粗匹配"
    assert "粗筛候选之外" in (result.degrade_reason or "")


def test_主引擎判重器使用无工具结构化短调用并解析结论(tmp_path: Path) -> None:
    from app.adapters.contracts import PlanningSegmentResult
    from app.adapters.recall import PrimaryEngineRecallJudge
    from app.store.recall import RecallRepository

    database = _database(tmp_path)
    _report(database, "r-openai", "OpenAI vs Claude Code")
    candidate = RecallRepository(database).search(
        "OpenAI 与 Claude Code 竞品对比", top_n=1
    ).candidates[0]

    class Route:
        request = None

        async def run_planning_segment(self, request):
            self.request = request
            return PlanningSegmentResult(
                text=json.dumps({
                    "judgements": [{
                        "report_id": "r-openai",
                        "same_item": True,
                        "confidence": "高",
                        "reason": "对象一致，旧报告结构可直接复用。",
                        "reusable_elements": ["报告骨架"],
                    }]
                }, ensure_ascii=False),
                completed=True,
            )

    route = Route()
    decisions = asyncio.run(
        PrimaryEngineRecallJudge(route)(
            "比较两款开发工具", (candidate,)
        )
    )

    assert decisions[0].report_id == "r-openai"
    assert decisions[0].same_item is True
    assert route.request.output_schema["properties"]["judgements"]["maxItems"] == 3
    assert route.request.output_schema["properties"]["judgements"]["minItems"] == 1
    assert "不得向用户提问" in route.request.prompt
    assert "OpenAI vs Claude Code" in route.request.prompt
    assert route.request.output_path is None


def test_主引擎结构化短调用断连保留原始原因(tmp_path: Path) -> None:
    from app.adapters.contracts import PlanningSegmentResult
    from app.adapters.recall import PrimaryEngineRecallJudge, PrimaryEngineUnavailable
    from app.store.recall import RecallRepository

    database = _database(tmp_path)
    _report(database, "r-openai", "OpenAI vs Claude Code")
    candidate = RecallRepository(database).search(
        "OpenAI 与 Claude Code 竞品对比", top_n=1
    ).candidates[0]

    class DisconnectedRoute:
        async def run_planning_segment(self, request):
            del request
            return PlanningSegmentResult(
                text="",
                completed=False,
                transport_interrupted=True,
                error="socket closed",
                cause="transport",
            )

    try:
        asyncio.run(
            PrimaryEngineRecallJudge(DisconnectedRoute())("查询", (candidate,))
        )
    except PrimaryEngineUnavailable as exc:
        assert str(exc) == "transport: socket closed"
    else:
        raise AssertionError("断连必须归类为主引擎不可用")


def test_创建研究接口立即返回且判重候选随后进入卡片(tmp_path: Path) -> None:
    import httpx

    from app.api.main import create_app
    from app.store.recall import (
        DuplicateDecision,
        RecallRepository,
        RecallService,
    )

    database = _database(tmp_path)
    _report(database, "r-openai", "OpenAI vs Claude Code")

    async def judge(query, candidates):
        del query
        return (
            DuplicateDecision(
                report_id=candidates[0].report_id,
                same_item=True,
                confidence="高",
                reason="比较对象与报告骨架一致。",
                reusable_elements=("报告骨架",),
            ),
        )

    application = create_app(
        database,
        SCHEMA_PATH,
        engine_probe=lambda: {},
        recall_service=RecallService(RecallRepository(database), judge=judge),
    )

    async def no_background_prepare(research_id, query, *, scale):
        del research_id, query, scale

    application.state.runtime.prepare_research = no_background_prepare

    async def request():
        async with application.router.lifespan_context(application):
            transport = httpx.ASGITransport(app=application)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/researches",
                    json={"query": "对比 OpenAI 与 Claude Code 的开发体验"},
                    headers={"X-Request-ID": "m5b-api-1"},
                )
                research_id = response.json()["data"]["research_id"]
                for _ in range(100):
                    cards = application.state.researches[research_id]["cards"]
                    if cards:
                        return response, cards[0]
                    await asyncio.sleep(0)
                raise AssertionError("判重候选卡片未在限定轮次内出现")

    response, card = asyncio.run(request())

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert "similar" not in data
    assert data["recall_status"] == "pending"
    assert card["card_type"] == "HISTORY_REUSE"
    assert card["title"] == "OpenAI vs Claude Code"
    assert card["target"]["source_research_id"] == "r-openai"
    assert card["target"]["completed_at"] == "2026-08-25T10:00:00+00:00"
    assert card["target"]["match_label"] == "主引擎语义判断"
    assert "比较对象与报告骨架一致。" in card["body"]
