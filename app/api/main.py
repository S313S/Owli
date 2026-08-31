"""Owli M0 的单进程 FastAPI 入口。"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Any, Awaitable, Callable, Literal, Optional

from fastapi import Body, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.adapters.selfcheck import (
    RuntimeConfigCheckError,
    SchemaCheckError,
    initialize_and_check,
    probe_engines,
    validate_runtime_config,
)
from app.adapters.recall import PrimaryEngineRecallJudge
from app.api.delivery import register_delivery_routes
from app.api.events import ResearchEventBuffer
from app.config import ResearchScaleConfig, load_research_scale_config
from app.orchestrator.background import guard_task
from app.orchestrator.runtime import RuntimeCoordinator
from app.plan.generate import PlanGenerationError
from app.plan.cards import (
    Card,
    CardActionType,
    CardBlocking,
    CardStatus,
    CardType,
)
from app.plan.editing import (
    PlanApprovalRejected,
    PlanEditRejected,
    PlanLintRejected,
    apply_edit,
    approve,
    reset,
)
from app.plan.model import Plan
from app.sources_probe import probe_gate_mode
from app.plan.store import PlanRevisionConflict, load_plan, save_plan
from app.replay.import_research import ReplayImportError, import_research
from app.store.dao import Store
from app.store.recall import RecallRepository, RecallResult, RecallService


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE_PATH = ROOT / "var" / "owli.db"
DEFAULT_SCHEMA_PATH = ROOT / "app" / "store" / "schema.sql"
DEFAULT_FRONTEND_DIST = ROOT / "web" / "dist"


class ResearchRequest(BaseModel):
    query: str
    scale: Literal["fast", "standard"] = "standard"


class ResetPlanRequest(BaseModel):
    scope: str
    target_id: str | None = None


class ResetAgentRequest(BaseModel):
    agent_id: str


class CardResponseRequest(BaseModel):
    action: str
    payload: Any


class ReplayRequest(BaseModel):
    """§RP-1 阶段重放：以跑过的研究为底，从指定 goal 起跑。

    **重放不作关账证据**——它拿旧证据旧产物、跑当下的代码，两者不同源；
    用来迭代与诊断，关账仍要一轮从规划起的干净整跑。
    """

    source_research_id: str
    from_goal: str | None = None
    #: 连 done 的章一起复位（这一段整个重做）；默认只复位没做完的章。
    reset_done: bool = False
    #: 底料在另一个库里时给绝对路径；不给就用本服务自己的库与 runs 目录。
    source_database: str | None = None
    source_runs: str | None = None


class TestFixtureRequest(BaseModel):
    unanswered: bool = False


def create_app(
    database_path: str | Path = DEFAULT_DATABASE_PATH,
    schema_path: str | Path = DEFAULT_SCHEMA_PATH,
    frontend_dist: str | Path = DEFAULT_FRONTEND_DIST,
    event_buffer: ResearchEventBuffer | None = None,
    adapter_factory: Callable[[], Any] | None = None,
    runs_root: str | Path | None = None,
    auto_confirm: bool | None = None,
    engine_probe: Callable[[], dict[str, dict[str, Any]]] | None = None,
    enable_test_routes: bool | None = None,
    routing_utc_clock: Callable[[], datetime] = _utc_now,
    scale_config: ResearchScaleConfig | None = None,
    recall_service: RecallService | None = None,
) -> FastAPI:
    database = Path(database_path)
    schema = Path(schema_path)
    frontend = Path(frontend_dist)
    test_routes_enabled = (
        os.getenv("OWLI_ENABLE_TEST_ROUTES") == "1"
        if enable_test_routes is None
        else enable_test_routes
    )
    store = Store(database)
    events = event_buffer or ResearchEventBuffer(
        max_events=2000, max_age_seconds=3600
    )
    researches: dict[str, dict[str, Any]] = {}
    cards: dict[str, Card] = {}
    request_cache: dict[tuple[str, str], tuple[int, dict[str, Any]]] = {}
    background_tasks: set[asyncio.Task[Any]] = set()
    plan_tasks: dict[str, asyncio.Task[Any]] = {}
    history_choice_locks: dict[str, asyncio.Lock] = {}

    async def default_recall_judge(query: str, candidates: Any) -> Any:
        if adapter_factory is None:
            from app.adapters.routing import RoutedAdapter

            adapter = RoutedAdapter(utc_clock=routing_utc_clock)
        else:
            adapter = adapter_factory()
        return await PrimaryEngineRecallJudge(adapter)(query, candidates)

    product_recall_service = recall_service or RecallService(
        RecallRepository(database),
        judge=default_recall_judge,
    )

    async def publish_plan_event(event: Any) -> None:
        """把规划期事件（分段落盘/重试）投进 SSE，规划过程对外可见。

        没有这条线时规划期对工作板与终端监视器完全静默，只能在失败后
        靠 progress.summary 事后取证（2026-08-21 6b 验收实录）。
        """
        research_id = str(getattr(event, "thread_id", "") or "")
        if not research_id:
            return
        item_kind = getattr(getattr(event, "item_kind", None), "value", None) or str(
            getattr(event, "item_kind", "") or "thinking"
        )
        raw = getattr(event, "raw", None)
        await events.publish(
            research_id,
            {
                "type": "normalized_event",
                "raw": raw if isinstance(raw, dict) else None,
                "data": {
                    "goal_id": "planning",
                    "agent_id": str(getattr(event, "turn_id", "") or "plan"),
                    "item_kind": item_kind,
                    "text": str(getattr(event, "text", "")),
                    "is_error": bool(getattr(event, "is_error", False)),
                },
            },
        )

    async def _probe_all_sources() -> dict:
        """门禁用的探活器；按模块属性取，用例打桩才拦得住（§M6-a 货 4）。"""
        from app import sources_probe

        return await sources_probe.probe_sources()

    store.on_plan_event = publish_plan_event
    product_scale_config = scale_config or load_research_scale_config()
    runtime = RuntimeCoordinator(
        store=store,
        event_buffer=events,
        researches=researches,
        cards=cards,
        adapter_factory=adapter_factory,
        runs_root=runs_root or ROOT / "runs",
        auto_confirm=auto_confirm,
        routing_utc_clock=routing_utc_clock,
        scale_config=product_scale_config,
        # §M6-a 货 4：门禁开着才注入探活器；off（默认）连探活都不发起。
        source_probe=(
            None if probe_gate_mode() == "off" else _probe_all_sources
        ),
    )

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        try:
            events.bind_to_running_loop()
            application.state.runtime_config_check = validate_runtime_config(
                product_scale_config
            )
            application.state.schema_check = initialize_and_check(database, schema)
            events.bind_store(store)
            application.state.engine_checks = (engine_probe or probe_engines)()
            application.state.rehydrated_researches = (
                await runtime.rehydrate_running_researches()
            )
        except (RuntimeConfigCheckError, SchemaCheckError) as error:
            print(str(error), file=sys.stderr)
            raise
        yield

    application = FastAPI(title="Owli", lifespan=lifespan)
    application.state.event_buffer = events
    application.state.researches = researches
    application.state.background_tasks = background_tasks
    application.state.store = store
    application.state.cards = cards
    application.state.request_cache = request_cache
    application.state.runtime = runtime
    application.state.recall_service = product_recall_service

    def envelope(data: Any = None) -> dict[str, Any]:
        return {"ok": True, "data": data, "error": None}

    def error_envelope(code: str, message: str, details: Any = None) -> dict[str, Any]:
        return {
            "ok": False,
            "data": None,
            "error": {"code": code, "message": message, "details": details},
        }

    def similar_payload(result: RecallResult) -> list[dict[str, Any]]:
        return [
            {
                "id": item.candidate.report_id,
                "title": item.candidate.title,
                "summary_line": item.candidate.summary_line,
                "completed_at": item.candidate.completed_at,
                "similarity_reason": item.reason,
                "same_item": item.same_item,
                "confidence": item.confidence,
                "reusable_elements": list(item.reusable_elements),
                "tags": list(item.candidate.tags),
                "sources": list(item.candidate.sources),
                "match_label": item.match_label,
                "query_mode": result.query_mode,
                "bm25_score": item.candidate.bm25_score,
                "keyword_score": item.candidate.keyword_score,
            }
            for item in result.matches
        ]

    def read_historical_report(
        research_id: str, report_path: str | None
    ) -> str | None:
        """只读历史报告正文；路径必须仍位于该研究的 runs 白名单内。"""
        if not report_path:
            return None
        raw_path = Path(report_path)
        candidates = [raw_path] if raw_path.is_absolute() else [
            ROOT / raw_path,
            runtime.runs_root.parent / raw_path,
            runtime.runs_root / raw_path,
            runtime.runs_root / research_id / raw_path,
        ]
        allowed_root = (runtime.runs_root / research_id).resolve()
        for candidate in candidates:
            resolved = candidate.resolve()
            if not resolved.is_relative_to(allowed_root) or not resolved.is_file():
                continue
            try:
                return resolved.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                return None
        return None

    def historical_snapshot(research_id: str) -> dict[str, Any] | None:
        """从 Store 事实重建历史只读 DTO，不创建任何运行态对象。"""
        report = store.get_report(research_id)
        if report is None:
            return None
        chapters = store.list_chapters(research_id)
        chapters_by_goal: dict[str, list[dict[str, Any]]] = {}
        for chapter in chapters:
            chapters_by_goal.setdefault(str(chapter["goal_id"]), []).append(chapter)

        plan_snapshot = report.get("plan_snapshot")
        planned_goals = (
            plan_snapshot.get("goals", []) if isinstance(plan_snapshot, dict) else []
        )
        goal_specs: list[tuple[str, str]] = []
        seen_goal_ids: set[str] = set()
        for goal in planned_goals:
            if not isinstance(goal, dict):
                continue
            goal_id = str(goal.get("goal_id", "")).strip()
            if not goal_id or goal_id in seen_goal_ids:
                continue
            seen_goal_ids.add(goal_id)
            goal_specs.append((goal_id, str(goal.get("title") or goal_id)))
        for goal_id in chapters_by_goal:
            if goal_id not in seen_goal_ids:
                goal_specs.append((goal_id, goal_id))

        goals: list[dict[str, Any]] = []
        for goal_id, title in goal_specs:
            rows = chapters_by_goal.get(goal_id, [])
            statuses = {str(row["status"]) for row in rows}
            if statuses & {"missing", "deferred"}:
                status = "failed"
                summary = "章节账本存在缺失项"
            elif rows and statuses == {"done"}:
                status = "done"
                summary = "全部章节已完成"
            elif "running" in statuses:
                status = "running"
                summary = "章节账本记录为运行中"
            else:
                status = "queued"
                summary = "章节账本尚未进入终态"
            goals.append(
                {
                    "id": goal_id,
                    "title": title,
                    "status": status,
                    "summary": summary,
                    "agents": [],
                }
            )

        missing = [
            {
                "goal_id": str(row["goal_id"]),
                "chapter_id": str(row["chapter_id"]),
                "status": str(row["status"]),
                "reason": row.get("reason"),
                "error": row.get("engine_error") or row.get("conclusion_error"),
            }
            for row in chapters
            if row["status"] in {"missing", "deferred"}
        ]
        summary = report.get("summary")
        summary_line = report.get("summary_line") or summary or "历史研究只读快照"
        status = str(report["status"])
        status_labels = {
            "running": "运行中",
            "completed": "已完成",
            "failed": "执行失败",
            "archived": "已归档",
        }
        report_path = report.get("report_path")
        return {
            "research_id": research_id,
            "title": report["title"],
            "status": status,
            "status_label": status_labels.get(status, status),
            "snapshot_source": "store",
            "progress": {
                "done": sum(goal["status"] in {"done", "failed"} for goal in goals),
                "total": len(goals),
                "summary": summary_line,
            },
            "usage": store.aggregate_research_usage(research_id),
            "report_path": report_path,
            "report_content": read_historical_report(research_id, report_path),
            "exports": (report.get("extra") or {}).get("exports") or [],
            "feishu": {"status": report.get("feishu_sync_status"), **((report.get("extra") or {}).get("feishu") or {})},
            "summary": summary,
            "summary_line": report.get("summary_line"),
            "actions": [],
            "goals": goals,
            "chapters": chapters,
            "missing": missing,
            "cards": [],
            "events": [],
        }

    def cached(scope: str, request_id: str) -> JSONResponse | None:
        result = request_cache.get((scope, request_id))
        if result is None:
            return None
        status_code, body = result
        return JSONResponse(copy.deepcopy(body), status_code=status_code)

    def remember(
        scope: str,
        request_id: str,
        body: dict[str, Any],
        *,
        status_code: int = 200,
    ) -> JSONResponse:
        request_cache[(scope, request_id)] = (status_code, copy.deepcopy(body))
        return JSONResponse(body, status_code=status_code)

    async def run_in_background(
        research_id: str,
        operation: Awaitable[Any],
    ) -> None:
        """消化编排器边界外的意外异常，避免任务永远卡在 running。"""
        try:
            await operation
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            state = researches[research_id]
            raw = {"exception": type(exc).__name__, "message": str(exc)}
            planning_failed = isinstance(exc, PlanGenerationError)
            try:
                if store.get_report(research_id) is not None:
                    store.finish_report(
                        research_id,
                        status="failed",
                        completed_at=runtime.now_iso(),
                        summary="规划失败",
                        summary_line=str(exc),
                    )
            except Exception as storage_exc:
                raw = {
                    "original": raw,
                    "storage_finalize_error": {
                        "exception": type(storage_exc).__name__,
                        "message": str(storage_exc),
                    },
                }
            state["status"] = "failed" if planning_failed else "unavailable"
            state["status_label"] = "规划失败" if planning_failed else "引擎不可用"
            state["actions"] = []
            state["progress"]["summary"] = (
                f"后台编排异常：{type(exc).__name__}: {exc}"
            )
            if state["goals"]:
                state["goals"][0]["status"] = "failed"
                state["goals"][0]["summary"] = state["progress"]["summary"]
            await events.publish(
                research_id,
                {
                    "type": "agent_update",
                    "data": {
                        "goal_id": "goal-1",
                        "agent_id": "orchestrator",
                        "engine": "Owli",
                        "status": "failed",
                        "activity": state["progress"]["summary"],
                    },
                },
            )
            await events.publish(
                research_id,
                {
                    "type": "error",
                    "raw": raw,
                    "data": {
                        "goal_id": "goal-1",
                        "agent_id": "orchestrator",
                        "status": state["status"],
                        "reason": {
                            "phase": "planning",
                            "kind": type(exc).__name__,
                            "message": str(exc),
                        },
                        "summary": state["progress"]["summary"],
                    },
                },
            )
            await events.publish(
                research_id,
                {"type": "progress", "data": dict(state["progress"])},
            )
            await events.publish(
                research_id,
                {
                    "type": "research_update",
                    "data": {
                        "status": state["status"],
                        "status_label": state["status_label"],
                        "actions": [],
                        "goals": state["goals"],
                    },
                },
            )

    def track_background(name: str, operation: Awaitable[Any]) -> asyncio.Task[Any]:
        task = asyncio.create_task(operation, name=name)
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)
        # D-013 货 2：后台任务的异常必须留痕，不许只剩解释器那句 never retrieved
        return guard_task(task, logger=logger, context="HTTP 后台任务")

    def start_plan_generation(
        research_id: str,
        *,
        start_gate: asyncio.Event | None = None,
    ) -> None:
        active = plan_tasks.get(research_id)
        if active is not None and not active.done():
            return
        report = store.get_report(research_id)
        if report is None:
            raise RuntimeError("待规划研究不存在")
        query = str(report["research_question"])
        extra = report.get("extra") if isinstance(report.get("extra"), dict) else {}
        scale = str(extra.get("scale", "standard"))
        state = researches[research_id]
        state["status"] = "planning"
        state["status_label"] = "正在生成计划"
        state["progress"]["summary"] = "正在生成全新调研计划"
        prepare_kwargs: dict[str, Any] = {"scale": scale}

        async def prepare_in_background() -> None:
            if start_gate is not None:
                await start_gate.wait()
            await run_in_background(
                research_id,
                runtime.prepare_research(
                    research_id,
                    query,
                    **prepare_kwargs,
                ),
            )

        task = track_background(
            f"owli:{research_id}",
            prepare_in_background(),
        )
        plan_tasks[research_id] = task

        def clear(completed: asyncio.Task[Any]) -> None:
            if plan_tasks.get(research_id) is completed:
                plan_tasks.pop(research_id, None)

        task.add_done_callback(clear)

    def create_reuse_card(
        research_id: str,
        item: dict[str, Any],
        index: int,
    ) -> Card:
        reusable = "、".join(item["reusable_elements"]) or "历史结论与信息源"
        card = Card(
            card_id=f"{research_id}-history-{index}",
            card_type=CardType.HISTORY_REUSE,
            research_id=research_id,
            goal_id=None,
            agent_id=None,
            title=str(item["title"]),
            body=(
                "复用这份历史调研会更快、已验证。"
                f"可复用：{reusable}；匹配理由：{item['similarity_reason']}"
            ),
            target={
                "source_research_id": item["id"],
                "display_name": item["title"],
                "completed_at": item["completed_at"],
                "summary_line": item.get("summary_line"),
                "sources": list(item["sources"]),
                "match_label": item["match_label"],
                "similarity_reason": item["similarity_reason"],
            },
            actions=[
                {
                    "type": CardActionType.CHOICE_2.value,
                    "id": "reuse",
                    "label": "复用这条历史",
                    "value": "reuse",
                },
                {
                    "type": CardActionType.CHOICE_2.value,
                    "id": "new",
                    "label": "坚持新建",
                    "value": "new",
                },
            ],
            blocking=CardBlocking.RESEARCH,
            deadline=None,
            status=CardStatus.PENDING,
            result=None,
            created_at=runtime.now_iso(),
            resolved_at=None,
        )
        cards[card.card_id] = card
        researches[research_id]["cards"].append(card.to_dict())
        return card

    async def recall_then_continue(
        research_id: str,
        query: str,
    ) -> None:
        try:
            result = await product_recall_service.recall(query)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await events.publish(
                research_id,
                {
                    "type": "reuse_check_complete",
                    "data": {
                        "has_matches": False,
                        "message": "历史匹配不可用，已继续生成全新计划",
                        "reason": f"{type(exc).__name__}: {exc}",
                    },
                },
            )
            start_plan_generation(research_id)
            return

        candidates = [
            item for item in similar_payload(result)
            if item["same_item"] is not False
        ]
        await events.publish(
            research_id,
            {
                "type": "reuse_check_complete",
                "data": {"has_matches": bool(candidates), "count": len(candidates)},
            },
        )
        if not candidates:
            start_plan_generation(research_id)
            return
        reuse_cards = [
            create_reuse_card(research_id, item, index)
            for index, item in enumerate(candidates, start=1)
        ]
        # 先登记预生成任务，再把第一张可点击卡发给浏览器，避免用户抢先点击时
        # 还找不到待取消任务，随后又被全新计划覆盖。
        plan_start_gate = asyncio.Event()
        start_plan_generation(research_id, start_gate=plan_start_gate)
        try:
            for card in reuse_cards:
                await events.publish(research_id, card.to_event())
        finally:
            plan_start_gate.set()

    async def run_recall_in_background(research_id: str, query: str) -> None:
        try:
            await recall_then_continue(research_id, query)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await events.publish(
                research_id,
                {
                    "type": "reuse_check_complete",
                    "data": {
                        "has_matches": False,
                        "message": "历史匹配处理异常，已继续生成全新计划",
                        "reason": f"{type(exc).__name__}: {exc}",
                    },
                },
            )
            start_plan_generation(research_id)

    @application.get("/api/health")
    async def health() -> dict:
        return {
            "ok": True,
            "data": {
                "schema": application.state.schema_check,
                "engines": application.state.engine_checks,
            },
            "error": None,
        }

    @application.get("/api/sources/probe")
    async def probe_sources_route(sources: str | None = None) -> dict:
        """§X-1 货 4 起跑前探活；判据是取到数据不是 HTTP 200。

        §M6-a 货 4：本路由只报数，挡不挡起跑由 OWLI_SOURCE_PROBE_GATE 决定。
        """
        from app.sources_probe import probe_sources

        wanted = [s.strip() for s in (sources or "").split(",") if s.strip()] or None
        try:
            results = await probe_sources(wanted)
        except KeyError as exc:
            return JSONResponse(error_envelope("unknown_source", str(exc)), status_code=400)
        return envelope({
            "sources": results,
            "all_ok": all(item["ok"] for item in results.values()) if results else False,
        })

    @application.post("/api/researches")
    async def create_research(
        request: ResearchRequest,
        x_request_id: str = Header(..., alias="X-Request-ID"),
    ) -> Any:
        scope = "create_research"
        hit = cached(scope, x_request_id)
        if hit is not None:
            return hit
        query = request.query.strip()
        if not query:
            raise HTTPException(status_code=422, detail="需求文本不能为空")
        research_id = f"r-{uuid.uuid4().hex[:12]}"
        researches[research_id] = runtime.initial_state(research_id, query)
        created_at = runtime.now_iso()
        store.create_report(
            id=research_id,
            title=query[:40],
            research_question=query,
            created_at=created_at,
            status="running",
            extra={"plan_generated_at": created_at, "scale": request.scale},
        )
        await events.publish(
            research_id,
            {"type": "research_snapshot", "data": researches[research_id]},
        )
        track_background(
            f"owli:recall:{research_id}",
            run_recall_in_background(research_id, query),
        )
        response = envelope({
            "research_id": research_id,
            "recall_status": "pending",
        })
        request_cache[(scope, x_request_id)] = (200, copy.deepcopy(response))
        return response

    @application.post("/api/researches/replay")
    async def replay_research(
        request: ReplayRequest,
        x_request_id: str = Header(..., alias="X-Request-ID"),
    ) -> Any:
        """以旧研究为底建一个新 research，并从指定 goal 起跑。

        **新 id、不就地改**：源那一行是要对照的基线，且它可能已经在
        `_schedulers` 里；就地起跑会被 `_claim_execution` 挡下。「旧那套谁来停」
        的答案是不停也不换——新 id 起新的一套，源 research 一个字不动。
        起跑仍然只走 `runtime.start_research`（本项目第三条启动路径也走那道闸）。
        """

        scope = "replay_research"
        hit = cached(scope, x_request_id)
        if hit is not None:
            return hit
        try:
            imported = import_research(
                store=store,
                source_database=Path(request.source_database or database),
                source_runs=Path(request.source_runs or runtime.runs_root),
                source_research_id=request.source_research_id,
                runs_root=runtime.runs_root,
                now_iso=runtime.now_iso(),
                from_goal=request.from_goal,
                reset_done=request.reset_done,
            )
        except ReplayImportError as error:
            return remember(
                scope, x_request_id,
                error_envelope("replay_source_invalid", str(error)),
                status_code=422,
            )
        plan = load_plan(store, imported.research_id)
        if plan is None:
            return remember(
                scope, x_request_id,
                error_envelope("replay_source_invalid", "底料没有计划快照"),
                status_code=422,
            )
        runtime._adapters[plan.research_id] = runtime.adapter_factory()
        researches[plan.research_id] = runtime._state_from_plan(plan)
        researches[plan.research_id]["status"] = "approved"
        researches[plan.research_id]["status_label"] = "计划已冻结"
        await events.publish(
            plan.research_id,
            {"type": "research_snapshot", "data": researches[plan.research_id]},
        )
        track_background(
            f"owli:scheduler:{plan.research_id}",
            run_in_background(plan.research_id, runtime.start_research(plan)),
        )
        return remember(scope, x_request_id, envelope({
            "research_id": imported.research_id,
            "replay_of": imported.source_research_id,
            "from_goal": imported.from_goal,
            "evidence_copied": imported.evidence_copied,
            "chapters_copied": imported.chapters_copied,
            "chapters_reset": list(imported.chapters_reset),
        }))

    @application.get("/api/researches/{research_id}")
    async def get_research(research_id: str) -> dict:
        if research_id in researches:
            # 状态以调度器为准：收尾之前也不得回报与调度器相反的状态
            state = runtime.sync_state_with_scheduler(research_id)
            return {"ok": True, "data": state, "error": None}
        state = historical_snapshot(research_id)
        if state is None:
            raise HTTPException(status_code=404, detail="调研任务不存在")
        return {"ok": True, "data": state, "error": None}

    @application.get("/api/researches/{research_id}/chapters")
    async def get_research_chapters(research_id: str) -> dict:
        if research_id not in researches and store.get_report(research_id) is None:
            raise HTTPException(status_code=404, detail="调研任务不存在")
        return envelope({"chapters": store.list_chapters(research_id)})

    def required_plan(research_id: str) -> Plan | JSONResponse:
        try:
            plan = load_plan(store, research_id)
        except KeyError:
            return JSONResponse(
                error_envelope("plan_not_found", "调研计划不存在，请返回入口重新发起"),
                status_code=404,
            )
        if plan is None:
            return JSONResponse(
                error_envelope("plan_not_ready", "计划仍在生成中，请稍后重新加载"),
                status_code=404,
            )
        return plan

    @application.get("/api/researches/{research_id}/plan")
    async def get_research_plan(research_id: str) -> Any:
        plan = required_plan(research_id)
        return plan if isinstance(plan, JSONResponse) else envelope(plan.to_dict())

    @application.put("/api/researches/{research_id}/plan")
    async def put_research_plan(
        research_id: str,
        submitted: dict[str, Any] = Body(...),
    ) -> Any:
        current = required_plan(research_id)
        if isinstance(current, JSONResponse):
            return current
        try:
            updated, lint_result = apply_edit(
                store,
                current,
                submitted,
                at=runtime.now_iso(),
                completed_goal_ids=runtime.completed_goal_ids(research_id),
                runs_root=runtime.runs_root,
            )
            runtime.update_plan(updated)
            await runtime.sync_question_cards(updated)
        except PlanRevisionConflict:
            return JSONResponse(
                error_envelope(
                    "plan_revision_conflict",
                    "计划已被更新。你的这次修改没有保存，请重新加载后再改一次，避免互相覆盖",
                ),
                status_code=409,
            )
        except PlanEditRejected as error:
            return JSONResponse(
                error_envelope("field_not_editable", str(error), error.details),
                status_code=422,
            )
        except PlanLintRejected as error:
            return JSONResponse(
                error_envelope("plan_lint_failed", str(error), error.result["errors"]),
                status_code=422,
            )
        except (TypeError, ValueError) as error:
            return JSONResponse(
                error_envelope("invalid_plan", f"计划字段不合法：{error}"),
                status_code=422,
            )
        data = updated.to_dict()
        data["lint"] = lint_result
        return envelope(data)

    async def reset_plan_response(
        research_id: str,
        *,
        scope: str,
        target_id: str | None,
        request_id: str,
        cache_scope: str,
    ) -> JSONResponse:
        hit = cached(cache_scope, request_id)
        if hit is not None:
            return hit
        current = required_plan(research_id)
        if isinstance(current, JSONResponse):
            return current
        try:
            updated = reset(
                store,
                current,
                scope=scope,
                target_id=target_id,
                at=runtime.now_iso(),
            )
        except PlanRevisionConflict:
            return remember(
                cache_scope,
                request_id,
                error_envelope(
                    "plan_revision_conflict",
                    "计划已被更新，恢复操作没有生效；请重新加载后再试",
                ),
                status_code=409,
            )
        except PlanLintRejected as error:
            return remember(
                cache_scope,
                request_id,
                error_envelope("plan_lint_failed", str(error), error.result["errors"]),
                status_code=422,
            )
        except (PlanEditRejected, TypeError, ValueError) as error:
            details = error.details if isinstance(error, PlanEditRejected) else None
            return remember(
                cache_scope,
                request_id,
                error_envelope("reset_rejected", str(error), details),
                status_code=422,
            )
        return remember(cache_scope, request_id, envelope(updated.to_dict()))

    @application.post("/api/researches/{research_id}/plan/reset")
    async def reset_research_plan(
        research_id: str,
        request: ResetPlanRequest,
        x_request_id: str = Header(..., alias="X-Request-ID"),
    ) -> JSONResponse:
        return await reset_plan_response(
            research_id,
            scope=request.scope,
            target_id=request.target_id,
            request_id=x_request_id,
            cache_scope=f"reset:{research_id}",
        )

    @application.post("/api/researches/{research_id}/plan/reset-agent")
    async def reset_research_agent(
        research_id: str,
        request: ResetAgentRequest,
        x_request_id: str = Header(..., alias="X-Request-ID"),
    ) -> JSONResponse:
        return await reset_plan_response(
            research_id,
            scope="agent",
            target_id=request.agent_id,
            request_id=x_request_id,
            cache_scope=f"reset-agent:{research_id}",
        )

    @application.post("/api/researches/{research_id}/plan/approve")
    async def approve_research_plan(
        research_id: str,
        x_request_id: str = Header(..., alias="X-Request-ID"),
    ) -> JSONResponse:
        scope = f"approve:{research_id}"
        hit = cached(scope, x_request_id)
        if hit is not None:
            return hit
        current = required_plan(research_id)
        if isinstance(current, JSONResponse):
            return current
        if runtime.scheduler_for(research_id) is not None:
            # 这条研究已经起跑过了（`OWLI_AUTO_CONFIRM=1` 时 prepare_research 自己批准并起跑）。
            # 重复批准**幂等返回已批准的计划 + 200**，不起第二套执行器（缺陷 D-021）；
            # 判据用 `scheduler_for()` 而不是 `plan.status`——两条启动路径都会把 status
            # 写成 approved，靠它分不出「批准过了」和「已经在跑了」。
            logger.info(
                "研究已在运行，批准请求幂等返回（未起第二套执行器）：research_id=%s",
                research_id,
            )
            return remember(
                scope,
                x_request_id,
                envelope({
                    "status": current.status,
                    "approved_at": current.approved_at,
                    "plan_rev": current.plan_rev,
                }),
            )
        try:
            updated = approve(store, current, at=runtime.now_iso())
        except PlanApprovalRejected as error:
            return remember(
                scope,
                x_request_id,
                error_envelope("questions_unanswered", str(error)),
                status_code=422,
            )
        except PlanLintRejected as error:
            return remember(
                scope,
                x_request_id,
                error_envelope("plan_lint_failed", str(error), error.result["errors"]),
                status_code=422,
            )
        except PlanRevisionConflict:
            return remember(
                scope,
                x_request_id,
                error_envelope(
                    "plan_revision_conflict",
                    "计划已被更新，批准操作没有生效；请重新加载后再试",
                ),
                status_code=409,
            )
        state = researches.get(research_id)
        if state is not None:
            state["status"] = "approved"
            state["status_label"] = "计划已冻结"
            state["actions"] = runtime.running_actions(research_id)
            await events.publish(
                research_id,
                {
                    "type": "research_update",
                    "data": {
                        "status": "approved",
                        "status_label": "计划已冻结",
                        "actions": state["actions"],
                    },
                },
            )
        task = track_background(
            f"owli:scheduler:{research_id}",
            run_in_background(research_id, runtime.start_research(updated)),
        )
        return remember(
            scope,
            x_request_id,
            envelope({
                "status": updated.status,
                "approved_at": updated.approved_at,
                "plan_rev": updated.plan_rev,
            }),
        )

    async def change_state(research_id: str, status: str, label: str) -> dict:
        state = researches.get(research_id)
        if state is None:
            raise HTTPException(status_code=404, detail="调研任务不存在")
        state["status"] = status
        state["status_label"] = label
        return {"ok": True, "data": state, "error": None}

    async def publish_state_update(research_id: str, state: dict[str, Any]) -> None:
        await events.publish(
            research_id,
            {
                "type": "research_update",
                "data": {
                    "status": state["status"],
                    "status_label": state["status_label"],
                    "actions": state["actions"],
                },
            },
        )

    def _require_started_scheduler(research_id: str, verb: str) -> None:
        """规划期没有 scheduler，`/pause`、`/stop` 以前一律 500（§W-1 登记）。

        500 是句假话——没有任何东西崩了，只是这一刻还没到能暂停的阶段。
        照实说：研究不存在给 404，规划期给 409 并写清现在能做什么。
        规划期真正可暂停（挂起计划生成任务）是另一件事，见 worklog 挂账。
        """
        if research_id not in researches:
            raise HTTPException(status_code=404, detail="调研任务不存在")
        if runtime.scheduler_for(research_id) is None:
            raise HTTPException(
                status_code=409,
                detail=f"计划尚未开跑，现在还不能{verb}；请等计划批准后再试，或直接放弃这次调研。",
            )

    @application.post("/api/researches/{research_id}/pause")
    async def pause_research(
        research_id: str,
        x_request_id: str = Header(..., alias="X-Request-ID"),
    ) -> JSONResponse:
        scope = f"pause:{research_id}"
        hit = cached(scope, x_request_id)
        if hit is not None:
            return hit
        _require_started_scheduler(research_id, "暂停")
        await runtime.pause(research_id)
        response = await change_state(research_id, "paused", "已暂停")
        response["data"]["actions"] = [
            {"id": "resume", "label": "继续", "method": "POST", "href": f"/api/researches/{research_id}/resume"},
            *[action for action in response["data"]["actions"] if action["id"] == "stop"],
        ]
        await publish_state_update(research_id, response["data"])
        return remember(scope, x_request_id, response)

    @application.post("/api/researches/{research_id}/resume")
    async def resume_research(
        research_id: str,
        x_request_id: str = Header(..., alias="X-Request-ID"),
    ) -> JSONResponse:
        scope = f"resume:{research_id}"
        hit = cached(scope, x_request_id)
        if hit is not None:
            return hit
        if research_id not in researches:
            raise HTTPException(status_code=404, detail="调研任务不存在")
        # resume 只翻状态并把驱动放后台，不阻塞到整轮跑完；回报的状态取自调度器本身
        await runtime.resume(research_id)
        response = envelope(runtime.sync_state_with_scheduler(research_id))
        await publish_state_update(research_id, response["data"])
        return remember(scope, x_request_id, response)

    @application.post("/api/researches/{research_id}/stop")
    async def stop_research(
        research_id: str,
        x_request_id: str = Header(..., alias="X-Request-ID"),
    ) -> JSONResponse:
        scope = f"stop:{research_id}"
        hit = cached(scope, x_request_id)
        if hit is not None:
            return hit
        _require_started_scheduler(research_id, "终止")
        await runtime.stop(research_id)
        response = await change_state(research_id, "stopped", "已终止")
        # 终止后仍留一个恢复出口：停下的章已复位，/resume 会按未完成部分继续
        response["data"]["actions"] = [
            {"id": "resume", "label": "继续", "method": "POST",
             "href": f"/api/researches/{research_id}/resume"},
        ]
        await publish_state_update(research_id, response["data"])
        return remember(scope, x_request_id, response)

    @application.post("/api/cards/{card_id}/respond")
    async def respond_to_card(
        card_id: str,
        request: CardResponseRequest,
        x_request_id: str = Header(..., alias="X-Request-ID"),
    ) -> JSONResponse:
        scope = f"respond:{card_id}"
        hit = cached(scope, x_request_id)
        if hit is not None:
            return hit
        card = cards.get(card_id)
        if card is None:
            return remember(
                scope,
                x_request_id,
                error_envelope("card_not_found", "待办卡片不存在或已被清理，请重新加载工作板"),
                status_code=404,
            )
        if card.status is not CardStatus.PENDING:
            return remember(
                scope,
                x_request_id,
                error_envelope("card_already_resolved", "这张卡片已经处理过，无需重复提交"),
                status_code=409,
            )
        allowed = {
            str(action.get("id") or action.get("type"))
            for action in card.actions
        }
        if request.action not in allowed:
            return remember(
                scope,
                x_request_id,
                error_envelope(
                    "card_action_invalid",
                    "提交的动作不在卡片允许范围内，请重新加载后选择页面上的按钮",
                    {"allowed": sorted(allowed)},
                ),
                status_code=422,
            )
        if card.card_type is CardType.HISTORY_REUSE:
            lock = history_choice_locks.setdefault(card.research_id, asyncio.Lock())
            async with lock:
                if card.status is not CardStatus.PENDING:
                    return remember(
                        scope,
                        x_request_id,
                        error_envelope(
                            "card_already_resolved",
                            "这组历史候选已经处理过，无需重复提交",
                        ),
                        status_code=409,
                    )
                source_research_id = str(card.target.get("source_research_id", ""))
                report = store.get_report(card.research_id)
                if report is None:
                    return remember(
                        scope,
                        x_request_id,
                        error_envelope(
                            "research_not_found",
                            "待处理研究不存在，请返回入口重新发起",
                        ),
                        status_code=404,
                    )
                reuse_source_to_apply: str | None = None
                if request.action == "reuse":
                    source_plan = load_plan(store, source_research_id)
                    if source_plan is None:
                        return remember(
                            scope,
                            x_request_id,
                            error_envelope(
                                "history_plan_unavailable",
                                "这条历史记录没有可复用计划，请选择全新开始",
                            ),
                            status_code=409,
                        )
                    task = plan_tasks.get(card.research_id)
                    if task is not None and not task.done():
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass
                    # 全新预生成可能已发布 QUESTION 卡；改选历史模板时统一作废，
                    # 防旧问题继续写入新计划。
                    stale_cards = [
                        item for item in cards.values()
                        if item.research_id == card.research_id
                        and item.card_type is not CardType.HISTORY_REUSE
                        and item.status is CardStatus.PENDING
                    ]
                    for item in stale_cards:
                        item.status = CardStatus.CANCELLED
                        item.resolved_at = runtime.now_iso()
                        await events.publish(card.research_id, item.to_event())
                    reuse_source_to_apply = source_research_id

                pending_cards = [
                    item for item in cards.values()
                    if item.research_id == card.research_id
                    and item.card_type is CardType.HISTORY_REUSE
                    and item.status is CardStatus.PENDING
                ]
                for item in pending_cards:
                    item.status = CardStatus.ANSWERED
                    item.result = {
                        "action": request.action,
                        "choice": request.action,
                        "source_research_id": source_research_id,
                        "selected": item.card_id == card.card_id,
                    }
                    item.resolved_at = runtime.now_iso()
                    await events.publish(card.research_id, item.to_event())
                state = researches[card.research_id]
                if state.get("cards"):
                    resolved_by_id = {
                        item.card_id: item.to_dict() for item in pending_cards
                    }
                    state["cards"] = [
                        resolved_by_id.get(str(item.get("card_id")), item)
                        for item in state["cards"]
                    ]
                if reuse_source_to_apply is not None:
                    extra = (
                        report.get("extra")
                        if isinstance(report.get("extra"), dict)
                        else {}
                    )
                    await runtime.reuse_plan(
                        card.research_id,
                        reuse_source_to_apply,
                        str(report["research_question"]),
                        scale=str(extra.get("scale", "standard")),
                    )
                return remember(scope, x_request_id, envelope(card.to_dict()))
        payload = (
            copy.deepcopy(request.payload)
            if isinstance(request.payload, dict)
            else {"value": copy.deepcopy(request.payload)}
        )
        try:
            resolved = await runtime.respond_card(
                card_id, action=request.action, payload=payload
            )
        except (RuntimeError, ValueError) as error:
            return remember(
                scope,
                x_request_id,
                error_envelope("card_response_rejected", str(error)),
                status_code=409,
            )
        return remember(scope, x_request_id, envelope(resolved.to_dict()))

    if test_routes_enabled:
        @application.post("/api/test/fixtures/m2-d")
        async def load_m2d_fixture(
            request: TestFixtureRequest,
            x_request_id: str = Header(..., alias="X-Request-ID"),
        ) -> JSONResponse:
            """仅测试：装载确定性计划；生产默认不注册此路由。"""
            scope = "test-fixture:m2-d"
            hit = cached(scope, x_request_id)
            if hit is not None:
                return hit
            from tests.plan_factory import make_plan_dict

            source = make_plan_dict()
            runner = source["goals"][0]["agents"][0]
            runner["engine"] = "codex"
            runner["capability"] = {
                "profile": "sandboxed-runner",
                "tools": ["shell.exec", "fs.read", "fs.write"],
                "sources": [],
                "fs": {
                    "read": ["goals/goal-1/**"],
                    "write": ["goals/goal-1/**"],
                },
                "network": "none",
                "shell": "workspace",
            }
            source["baseline"]["goals"][0]["agents"][0] = copy.deepcopy(runner)
            if request.unanswered:
                source["decision_balance"][0]["answer"] = None
                source["decision_balance"][0]["answered_at"] = None
            plan = Plan.from_dict(source)
            runtime._adapters[plan.research_id] = runtime.adapter_factory()
            store.create_report(
                id=plan.research_id,
                title=plan.title,
                research_question=plan.research_question,
                use_case=plan.use_case,
                status="running",
                created_at=plan.created_at,
            )
            save_plan(store, plan, expected_rev=0)
            researches[plan.research_id] = {
                "research_id": plan.research_id,
                "title": plan.title,
                "status": "awaiting_review",
                "status_label": "等待核对计划",
                "progress": {
                    "done": 0,
                    "total": len(plan.goals),
                    "summary": "计划已生成，等待回答追问并批准",
                },
                "actions": [],
                "goals": [
                    {
                        "id": goal.goal_id,
                        "title": goal.title,
                        "status": goal.status,
                        "summary": goal.objective,
                        "agents": [
                            {
                                "id": agent.agent_id,
                                "name": agent.display_name,
                                "engine": agent.engine,
                                "status": agent.status,
                                "activity": agent.task,
                            }
                            for agent in goal.agents
                        ],
                    }
                    for goal in plan.goals
                ],
                "cards": [],
                "events": [],
            }
            return remember(
                scope,
                x_request_id,
                envelope({"research_id": plan.research_id}),
            )

        @application.post("/api/test/researches/{research_id}/cards")
        async def inject_test_card(
            research_id: str,
            payload: dict[str, Any] = Body(...),
            x_request_id: str = Header(..., alias="X-Request-ID"),
        ) -> JSONResponse:
            """仅测试：注入运行期卡片并沿真实 SSE 发布。"""
            scope = f"test-card:{research_id}"
            hit = cached(scope, x_request_id)
            if hit is not None:
                return hit
            if research_id not in researches:
                return remember(
                    scope,
                    x_request_id,
                    error_envelope("research_not_found", "调研任务不存在，无法注入测试卡片"),
                    status_code=404,
                )
            try:
                card = Card(**payload)
            except (TypeError, ValueError) as error:
                return remember(
                    scope,
                    x_request_id,
                    error_envelope("invalid_test_card", f"测试卡片字段不合法：{error}"),
                    status_code=422,
                )
            if card.research_id != research_id:
                return remember(
                    scope,
                    x_request_id,
                    error_envelope("invalid_test_card", "卡片 research_id 与路径不一致"),
                    status_code=422,
                )
            cards[card.card_id] = card
            state = researches[research_id]
            state["cards"] = [
                card.to_dict(),
                *[item for item in state.get("cards", []) if item.get("card_id") != card.card_id],
            ]
            await events.publish(research_id, card.to_event())
            return remember(scope, x_request_id, envelope(card.to_dict()))

    @application.get("/api/researches/{research_id}/events")
    async def research_events(
        research_id: str,
        request: Request,
        last_event_id_header: Optional[str] = Header(None, alias="Last-Event-ID"),
        last_event_id_query: Optional[int] = Query(None, alias="last_event_id"),
    ) -> StreamingResponse:
        raw_last_id = last_event_id_header
        if raw_last_id is not None:
            try:
                last_event_id = int(raw_last_id)
            except ValueError as error:
                raise HTTPException(status_code=400, detail="Last-Event-ID 必须是整数") from error
        else:
            last_event_id = last_event_id_query

        store_ready = database.is_file()
        stored_snapshot = (
            historical_snapshot(research_id)
            if research_id not in researches and store_ready
            else None
        )
        if research_id not in researches and store_ready and stored_snapshot is None:
            raise HTTPException(status_code=404, detail="调研任务不存在")

        async def stream() -> AsyncIterator[str]:
            connected = {
                "type": "stream_connected",
                "research_id": research_id,
                "sequence": last_event_id or 0,
                "occurred_at": runtime.now_iso(),
                "data": {"message": "SSE 已连接"},
            }
            body = json.dumps(
                connected, ensure_ascii=False, separators=(",", ":")
            )
            # 连接确认不进入 research 共享缓冲，也不写 id，避免污染重放游标。
            yield f"event: stream_connected\ndata: {body}\n\n"
            if stored_snapshot is not None:
                snapshot_event = {
                    "type": "research_snapshot",
                    "research_id": research_id,
                    "sequence": last_event_id or 0,
                    "occurred_at": runtime.now_iso(),
                    "data": stored_snapshot,
                }
                snapshot_body = json.dumps(
                    snapshot_event, ensure_ascii=False, separators=(",", ":")
                )
                yield f"event: research_snapshot\ndata: {snapshot_body}\n\n"
                return
            replay = await events.replay_after(research_id, last_event_id)
            cursor = last_event_id or 0
            for event in replay.events:
                cursor = event.sequence
                yield event.to_sse()
            while not await request.is_disconnected():
                available = await events.wait_after(research_id, cursor)
                if not available:
                    yield ": keep-alive\n\n"
                    continue
                for event in available:
                    if event.sequence <= cursor:
                        continue
                    cursor = event.sequence
                    yield event.to_sse()

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # §DLV-1 交付面：报告结构化只读 / 证据清单 / 导出（app/api/delivery.py）
    register_delivery_routes(
        application,
        store=store,
        read_report=read_historical_report,
        envelope=envelope,
        runs_root=runtime.runs_root,
    )

    application.mount(
        "/assets",
        StaticFiles(directory=str(frontend / "assets"), check_dir=False),
        name="frontend-assets",
    )

    @application.get("/", response_class=HTMLResponse)
    async def frontend_index() -> FileResponse:
        index = frontend / "index.html"
        if not index.is_file():
            raise HTTPException(status_code=503, detail="前端尚未构建，请先运行 npm run build")
        return FileResponse(index)

    @application.get("/{route_path:path}", response_class=HTMLResponse)
    async def frontend_route(route_path: str) -> FileResponse:
        del route_path
        index = frontend / "index.html"
        if not index.is_file():
            raise HTTPException(status_code=404, detail="页面不存在")
        return FileResponse(index)

    return application


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8721, workers=1)
