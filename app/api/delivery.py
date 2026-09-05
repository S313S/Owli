"""§DLV-1 交付面路由：报告结构化只读 / 证据清单 / 导出。

独立成模块只为少碰 `app/api/main.py`（RP-1 同期在改同一文件）；
状态、白名单读盘、信封格式全部由 main.py 注入，这里不持有任何运行态。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse

from app.reliability.scoring import SCORE_FIELDS
from app.report.render import parse_report

logger = logging.getLogger(__name__)

EVIDENCE_FIELDS: tuple[str, ...] = (
    "id", "citation_no", "permalink", "title", "content_excerpt", "platform",
    "source_type", "fetch_method", "author_name", "published_at", "fetched_at",
    "goal_id", *SCORE_FIELDS, "score_total", "grade", "rating_notes", "rated_by",
    "raw_metrics",
    # §CMT-1 货 5：报告页要能按帖/评论筛选，父帖链接给评论行做溯源。
    "kind", "parent_permalink",
)


def evidence_view(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """证据清单：角标行在前按角标号，未引用行按 id；只读评分不重算。"""
    items = [{field: row.get(field) for field in EVIDENCE_FIELDS} for row in rows]
    items.sort(key=lambda item: (item["citation_no"] is None, item["citation_no"] or 0, item["id"]))
    return {
        "items": items,
        "counts": {
            "total": len(items),
            "cited": sum(item["citation_no"] is not None for item in items),
            "by_platform": dict(Counter(str(item["platform"]) for item in items)),
            "by_grade": dict(Counter(str(item["grade"] or "?") for item in items)),
            "by_kind": dict(Counter(str(item["kind"] or "post") for item in items)),
        },
        "score_fields": list(SCORE_FIELDS),
    }


def register_delivery_routes(
    application: FastAPI,
    *,
    store: Any,
    read_report: Callable[[str, str | None], str | None],
    envelope: Callable[[Any], dict[str, Any]],
    runs_root: Path,
    #: §RPT-1：正式稿整理是后台活，进度靠它往 SSE 上推；不给就静默不推（测试用）。
    publish: Callable[[str, Mapping[str, Any]], Awaitable[Any]] | None = None,
) -> None:
    def require_report(research_id: str) -> dict[str, Any]:
        report = store.get_report(research_id)
        if report is None:
            raise HTTPException(status_code=404, detail="调研任务不存在")
        return report

    @application.get("/api/researches/{research_id}/report")
    async def get_research_report(research_id: str) -> dict[str, Any]:
        report = require_report(research_id)
        text = read_report(research_id, report.get("report_path"))
        if text is None:
            raise HTTPException(status_code=404, detail="报告正文不可用")
        view = parse_report(text)
        view["research_id"] = research_id
        view["status"] = report.get("status")
        view["report_path"] = report.get("report_path")
        view["title"] = view.get("title") or report.get("title")
        view["summary"] = report.get("summary")
        view["summary_line"] = report.get("summary_line")
        view["exports"] = (report.get("extra") or {}).get("exports") or []
        # §FU-1 起四列优先、extra.feishu 兜底：四列是 set_feishu_sync 写的正式字段，
        # extra 只补四列放不下的细节（transport/doc_url/message/error）与老库回填前的旧账。
        columns = {
            "status": report.get("feishu_sync_status"),
            "doc_token": report.get("feishu_doc_token"),
            "record_id": report.get("feishu_record_id"),
            "synced_at": report.get("feishu_synced_at"),
        }
        view["feishu"] = {
            **((report.get("extra") or {}).get("feishu") or {}),
            **{key: value for key, value in columns.items() if value is not None},
        }
        return envelope(view)

    @application.get("/api/researches/{research_id}/evidence")
    async def get_research_evidence(research_id: str) -> dict[str, Any]:
        require_report(research_id)
        return envelope(evidence_view(store.list_evidence(research_id)))

    def export_dir(research_id: str) -> Path:
        return (runs_root / research_id / "exports").resolve()

    async def _emit(research_id: str, payload: dict[str, Any]) -> None:
        """推事件；推不出去不能连累整理本身（失败只落日志）。"""
        if publish is None:
            return
        try:
            await publish(research_id, payload)
        except Exception:  # noqa: BLE001 — SSE 推送失败不该让后台任务炸掉
            logger.warning("正式稿进度事件推送失败：%s", research_id, exc_info=True)

    async def _polish_in_background(research_id: str, template: str, text: str) -> None:
        """后台整理一次正式稿。失败只发 export_failed + 落一条无 url 的登记，研究状态一个字不改。"""
        from app.export.registry import record_export
        from app.report.polish.run import artifact_paths, polish

        def record_failure(reason: str) -> None:
            # 同 kind 同 path 只留最新一条，所以下一次整理成功会把这条失败记录顶掉。
            # url=None 就是「这条没有可下载的产物」，前端据此不给链接。
            record_export(store, research_id, kind="polished",
                          path=str(artifact_paths(runs_root, research_id, template)[0]),
                          url=None, desc=f"正式稿整理失败（模板 {template}）：{reason}")

        await _emit(research_id, {"type": "progress", "data": {
            "stage": "polish", "template": template, "status": "running",
            "summary": f"正在整理正式稿（{template}）"}})
        try:
            outcome = await polish(store, research_id, runs_root, text, template=template)
        except Exception as exc:  # noqa: BLE001 — 整理失败不改研究状态
            logger.exception("正式稿整理失败：%s/%s", research_id, template)
            reason = f"{type(exc).__name__}: {exc}"
            record_failure(reason)
            await _emit(research_id, {"type": "export_failed", "data": {
                "kind": "polished", "template": template, "error": reason}})
            return
        if outcome["status"] != "ok":
            offpool = outcome.get("offpool") or []
            reason = ("角标越出信息源池：" + "、".join(offpool)) if offpool else \
                "；".join(outcome.get("errors") or ["未知原因"])
            record_failure(reason)
            await _emit(research_id, {"type": "export_failed", "data": {
                "kind": "polished", "template": template, "error": reason, "offpool": offpool}})
            return
        path = Path(outcome["path"])
        record_export(store, research_id, kind="polished", path=str(path),
                      url=f"/api/researches/{research_id}/exports/{path.name}",
                      desc=f"正式稿（模板 {template}）")
        await _emit(research_id, {"type": "progress", "data": {
            "stage": "polish", "template": template, "status": "done",
            "summary": f"正式稿已整理完成（{template}）"}})

    @application.get("/api/report-templates")
    async def list_report_templates() -> dict[str, Any]:
        """前端模板下拉的数据源；加模板只加目录，这里不用改。"""
        from app.report.polish.skills import load_templates

        return envelope({"templates": [t.as_listing() for t in load_templates()]})

    @application.post("/api/researches/{research_id}/export")
    async def export_research(research_id: str, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        from app.export.excel import export_excel
        from app.export.registry import record_export

        report = require_report(research_id)
        kind = str(payload.get("kind") or "excel")
        text = read_report(research_id, report.get("report_path"))
        if text is None:
            raise HTTPException(status_code=404, detail="报告正文不可用，无法导出")
        if kind == "excel":
            path = export_excel(store, research_id, runs_root, text)
            url = f"/api/researches/{research_id}/exports/{path.name}"
            record = record_export(store, research_id, kind="excel", path=str(path), url=url,
                                   desc="Excel 附件（6 sheet，spec §2）")
            return envelope({"kind": "excel", "path": str(path), "url": url, "record": record})
        if kind == "feishu":
            from app.export.feishu import push_to_feishu

            return envelope(push_to_feishu(store, research_id, text))
        if kind == "polished":
            from app.orchestrator.background import guard_task
            from app.report.polish.skills import get_template

            try:
                skill = get_template(payload.get("template"))
            except KeyError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            task = asyncio.create_task(
                _polish_in_background(research_id, skill.name, text))
            guard_task(task, logger=logger, context="正式稿整理")
            return envelope({"kind": "polished", "template": skill.name, "status": "started",
                             "title": skill.title})
        raise HTTPException(status_code=400, detail="kind 只能是 excel、feishu 或 polished")

    @application.get("/api/researches/{research_id}/polished")
    async def get_polished_report(research_id: str,
                                  template: str | None = Query(default=None)) -> dict[str, Any]:
        """正式稿正文 + 确定性表 + 与工作稿同形的 sources（前端角标卡零改动）。"""
        from app.report.polish.run import artifact_paths
        from app.report.polish.skills import get_template

        report = require_report(research_id)
        try:
            skill = get_template(template)
        except KeyError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        md_path, tables_path = artifact_paths(runs_root, research_id, skill.name)
        if not md_path.is_file():
            raise HTTPException(status_code=404, detail="这份研究还没整理过该模板的正式稿")
        work_text = read_report(research_id, report.get("report_path"))
        tables = json.loads(tables_path.read_text(encoding="utf-8")) if tables_path.is_file() else {}
        # 角标卡的料还是工作稿那一份：正式稿不新增信息源，只从池里挑。
        # 工作稿读不到时（库里 report_path 是指向别的 worktree 的绝对路径就会这样）
        # 退回 tables.json 里存下的同一批源，别让整页角标变成悬空。
        sources = parse_report(work_text)["sources"] if work_text else [
            {"citation_no": int(item["mark"][1:]), "mark": item["mark"],
             "title": item.get("title") or "", "permalink": item.get("url") or ""}
            for item in (tables.get("sources") or [])
        ]
        return envelope({
            "research_id": research_id, "template": skill.name, "title": skill.title,
            "markdown": md_path.read_text(encoding="utf-8"),
            "tables": tables.get("tables") or {},
            "sources": sources,
            "generated_at": md_path.stat().st_mtime,
            "url": f"/api/researches/{research_id}/exports/{md_path.name}",
        })

    @application.get("/api/researches/{research_id}/exports/{file_name}")
    async def download_export(research_id: str, file_name: str) -> FileResponse:
        require_report(research_id)
        target = (export_dir(research_id) / file_name).resolve()
        if not target.is_relative_to(export_dir(research_id)) or not target.is_file():
            raise HTTPException(status_code=404, detail="导出产物不存在")
        media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" \
            if target.suffix == ".xlsx" else "application/octet-stream"
        return FileResponse(target, media_type=media, filename=target.name)
