"""Owli M0 的单进程 FastAPI 入口。"""

from __future__ import annotations

import asyncio
import copy
import json
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

from app.adapters.selfcheck import SchemaCheckError, initialize_and_check, probe_engines
from app.api.events import ResearchEventBuffer
from app.config import ResearchScaleConfig, load_research_scale_config
from app.orchestrator.runtime import RuntimeCoordinator
from app.plan.cards import Card, CardStatus
from app.plan.editing import (
    PlanApprovalRejected,
    PlanEditRejected,
    PlanLintRejected,
    apply_edit,
    approve,
    reset,
)
from app.plan.model import Plan
from app.plan.store import PlanRevisionConflict, load_plan, save_plan
from app.store.dao import Store


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


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
) -> FastAPI:
    database = Path(database_path)
    schema = Path(schema_path)
    frontend = Path(frontend_dist)
    test_routes_enabled = (
        os.getenv("OWLI_ENABLE_TEST_ROUTES") == "1"
        if enable_test_routes is None
        else enable_test_routes
    )
    events = event_buffer or ResearchEventBuffer(max_events=2000, max_age_seconds=3600)
    researches: dict[str, dict[str, Any]] = {}
    cards: dict[str, Card] = {}
    request_cache: dict[tuple[str, str], tuple[int, dict[str, Any]]] = {}
    background_tasks: set[asyncio.Task[Any]] = set()
    store = Store(database)

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

    store.on_plan_event = publish_plan_event
    runtime = RuntimeCoordinator(
        store=store,
        event_buffer=events,
        researches=researches,
        cards=cards,
        adapter_factory=adapter_factory,
        runs_root=runs_root or ROOT / "runs",
        auto_confirm=auto_confirm,
        routing_utc_clock=routing_utc_clock,
        scale_config=scale_config or load_research_scale_config(),
    )

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        try:
            events.bind_to_running_loop()
            application.state.schema_check = initialize_and_check(database, schema)
            application.state.engine_checks = (engine_probe or probe_engines)()
        except SchemaCheckError as error:
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

    def envelope(data: Any = None) -> dict[str, Any]:
        return {"ok": True, "data": data, "error": None}

    def error_envelope(code: str, message: str, details: Any = None) -> dict[str, Any]:
        return {
            "ok": False,
            "data": None,
            "error": {"code": code, "message": message, "details": details},
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
            try:
                if store.get_report(research_id) is not None:
                    store.finish_report(
                        research_id,
                        status="failed",
                        completed_at=runtime.now_iso(),
                    )
            except Exception as storage_exc:
                raw = {
                    "original": raw,
                    "storage_finalize_error": {
                        "exception": type(storage_exc).__name__,
                        "message": str(storage_exc),
                    },
                }
            state["status"] = "unavailable"
            state["status_label"] = "引擎不可用"
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
                        "status": "unavailable",
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
        task = asyncio.create_task(
            run_in_background(
                research_id,
                runtime.prepare_research(research_id, query, scale=request.scale),
            ),
            name=f"owli:{research_id}",
        )
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)
        response = envelope({"research_id": research_id, "similar": []})
        request_cache[(scope, x_request_id)] = (200, copy.deepcopy(response))
        return response

    @application.get("/api/researches/{research_id}")
    async def get_research(research_id: str) -> dict:
        state = researches.get(research_id)
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
        task = asyncio.create_task(
            run_in_background(research_id, runtime.start_research(updated)),
            name=f"owli:scheduler:{research_id}",
        )
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)
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

    @application.post("/api/researches/{research_id}/pause")
    async def pause_research(
        research_id: str,
        x_request_id: str = Header(..., alias="X-Request-ID"),
    ) -> JSONResponse:
        scope = f"pause:{research_id}"
        hit = cached(scope, x_request_id)
        if hit is not None:
            return hit
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
        await runtime.resume(research_id)
        response = await change_state(research_id, "running", "运行中")
        response["data"]["actions"] = runtime.running_actions(research_id)
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
        await runtime.stop(research_id)
        response = await change_state(research_id, "stopped", "已终止")
        response["data"]["actions"] = []
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
