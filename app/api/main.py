"""Owli M0 的单进程 FastAPI 入口。"""

from __future__ import annotations

import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Any, Optional

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.adapters.selfcheck import SchemaCheckError, initialize_and_check
from app.api.events import ResearchEventBuffer


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE_PATH = ROOT / "var" / "owli.db"
DEFAULT_SCHEMA_PATH = ROOT / "app" / "store" / "schema.sql"
DEFAULT_FRONTEND_DIST = ROOT / "web" / "dist"


class ResearchRequest(BaseModel):
    query: str


def _initial_state(research_id: str, query: str) -> dict[str, Any]:
    return {
        "research_id": research_id,
        "title": query,
        "status": "running",
        "status_label": "运行中",
        "progress": {"done": 1, "total": 6, "summary": "当前：子目标 ② 与 ③ 并行采集中"},
        "actions": [
            {"id": "pause", "label": "暂停", "method": "POST", "href": f"/api/researches/{research_id}/pause"},
            {"id": "stop", "label": "终止", "danger": True, "confirm": "终止本次调研？已入库证据与产物会保留。", "method": "POST", "href": f"/api/researches/{research_id}/stop"},
        ],
        "goals": [
            {"id": "goal-1", "title": "界定竞品集与评估框架", "status": "done", "summary": "2 个 agent 全部完成 · 产物 competitor-set.md", "agents": []},
            {"id": "goal-2", "title": "海外社区真实使用反馈采集", "status": "running", "summary": "1 完成 / 2 运行 / 1 排队 · 已入库证据 31 条", "agents": [
                {"id": "hn", "name": "Hacker News 采集", "engine": "Codex", "status": "running", "activity": "正在取 story 32932137 的评论区（已 412 / 793 条）"},
                {"id": "ph", "name": "Product Hunt 采集", "engine": "Codex", "status": "running", "activity": "正在读取产品页与评论"},
            ]},
            {"id": "goal-3", "title": "官方文档与第三方评测采集", "status": "running", "summary": "2 个 agent 运行中 · 已入库证据 14 条", "agents": [
                {"id": "docs", "name": "官方文档采集", "engine": "Claude", "status": "running", "activity": "正在整理官方更新日志"},
            ]},
            {"id": "goal-4", "title": "可靠度评级", "status": "queued", "summary": "等待子目标 ②③ 汇合后开始", "agents": []},
            {"id": "goal-5", "title": "对比矩阵", "status": "queued", "summary": "等待可靠度评级完成", "agents": []},
            {"id": "goal-6", "title": "报告与附件", "status": "queued", "summary": "等待前置子目标完成", "agents": []},
        ],
        "cards": [],
        "events": [],
    }


def create_app(
    database_path: str | Path = DEFAULT_DATABASE_PATH,
    schema_path: str | Path = DEFAULT_SCHEMA_PATH,
    frontend_dist: str | Path = DEFAULT_FRONTEND_DIST,
    event_buffer: ResearchEventBuffer | None = None,
) -> FastAPI:
    database = Path(database_path)
    schema = Path(schema_path)
    frontend = Path(frontend_dist)
    events = event_buffer or ResearchEventBuffer(max_events=2000, max_age_seconds=3600)
    researches: dict[str, dict[str, Any]] = {}

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
        researches[research_id] = _initial_state(research_id, query)
        await events.publish(
            research_id,
            {"type": "research_snapshot", "data": researches[research_id]},
        )
        return {"ok": True, "data": {"research_id": research_id, "similar": []}, "error": None}

    @application.get("/api/researches/{research_id}")
    async def get_research(research_id: str) -> dict:
        state = researches.setdefault(
            research_id,
            _initial_state(research_id, "飞书竞品优缺点挖掘"),
        )
        return {"ok": True, "data": state, "error": None}

    async def change_state(research_id: str, status: str, label: str) -> dict:
        state = researches.setdefault(research_id, _initial_state(research_id, research_id))
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
        response["data"]["actions"] = _initial_state(research_id, "")["actions"]
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
