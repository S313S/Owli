"""Owli M0 的单进程 FastAPI 入口。"""

from __future__ import annotations

import asyncio
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Any, Callable, Optional

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.adapters.selfcheck import SchemaCheckError, initialize_and_check
from app.api.events import ResearchEventBuffer
from app.orchestrator.mini import MiniOrchestrator, build_actions, build_initial_state
from app.store.dao import Store


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE_PATH = ROOT / "var" / "owli.db"
DEFAULT_SCHEMA_PATH = ROOT / "app" / "store" / "schema.sql"
DEFAULT_FRONTEND_DIST = ROOT / "web" / "dist"


class ResearchRequest(BaseModel):
    query: str


def create_app(
    database_path: str | Path = DEFAULT_DATABASE_PATH,
    schema_path: str | Path = DEFAULT_SCHEMA_PATH,
    frontend_dist: str | Path = DEFAULT_FRONTEND_DIST,
    event_buffer: ResearchEventBuffer | None = None,
    orchestrator_factory: Callable[..., Any] | None = None,
) -> FastAPI:
    database = Path(database_path)
    schema = Path(schema_path)
    frontend = Path(frontend_dist)
    events = event_buffer or ResearchEventBuffer(max_events=2000, max_age_seconds=3600)
    researches: dict[str, dict[str, Any]] = {}
    background_tasks: set[asyncio.Task[Any]] = set()
    store = Store(database)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        try:
            events.bind_to_running_loop()
            application.state.schema_check = initialize_and_check(database, schema)
        except SchemaCheckError as error:
            print(str(error), file=sys.stderr)
            raise
        yield

    application = FastAPI(title="Owli", lifespan=lifespan)
    application.state.event_buffer = events
    application.state.researches = researches
    application.state.background_tasks = background_tasks
    application.state.store = store

    async def run_in_background(
        research_id: str,
        runner: Any,
    ) -> None:
        """消化编排器边界外的意外异常，避免任务永远卡在 running。"""
        try:
            await runner.run()
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
                        completed_at=datetime.now(timezone.utc).isoformat(),
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
            "data": {"schema": application.state.schema_check},
            "error": None,
        }

    @application.post("/api/researches")
    async def create_research(request: ResearchRequest) -> dict:
        query = request.query.strip()
        if not query:
            raise HTTPException(status_code=422, detail="需求文本不能为空")
        research_id = f"r-{uuid.uuid4().hex[:12]}"
        researches[research_id] = build_initial_state(research_id, query)
        await events.publish(
            research_id,
            {"type": "research_snapshot", "data": researches[research_id]},
        )
        factory = orchestrator_factory or MiniOrchestrator
        runner = factory(
            research_id=research_id,
            query=query,
            store=store,
            event_buffer=events,
            state=researches[research_id],
        )
        task = asyncio.create_task(
            run_in_background(research_id, runner),
            name=f"owli:{research_id}",
        )
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)
        return {"ok": True, "data": {"research_id": research_id, "similar": []}, "error": None}

    @application.get("/api/researches/{research_id}")
    async def get_research(research_id: str) -> dict:
        state = researches.get(research_id)
        if state is None:
            raise HTTPException(status_code=404, detail="调研任务不存在")
        return {"ok": True, "data": state, "error": None}

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
    async def pause_research(research_id: str) -> dict:
        response = await change_state(research_id, "paused", "已暂停")
        response["data"]["actions"] = [
            {"id": "resume", "label": "继续", "method": "POST", "href": f"/api/researches/{research_id}/resume"},
            *[action for action in response["data"]["actions"] if action["id"] == "stop"],
        ]
        await publish_state_update(research_id, response["data"])
        return response

    @application.post("/api/researches/{research_id}/resume")
    async def resume_research(research_id: str) -> dict:
        response = await change_state(research_id, "running", "运行中")
        response["data"]["actions"] = build_actions(research_id)
        await publish_state_update(research_id, response["data"])
        return response

    @application.post("/api/researches/{research_id}/stop")
    async def stop_research(research_id: str) -> dict:
        response = await change_state(research_id, "stopped", "已终止")
        response["data"]["actions"] = []
        await publish_state_update(research_id, response["data"])
        return response

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

        await events.publish(
            research_id,
            {"type": "stream_connected", "data": {"message": "SSE 已连接"}},
        )

        async def stream() -> AsyncIterator[str]:
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
