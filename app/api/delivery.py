"""§DLV-1 交付面路由：报告结构化只读 / 证据清单 / 导出。

独立成模块只为少碰 `app/api/main.py`（RP-1 同期在改同一文件）；
状态、白名单读盘、信封格式全部由 main.py 注入，这里不持有任何运行态。
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, Callable

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse

from app.reliability.scoring import SCORE_FIELDS
from app.report.render import parse_report

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
        raise HTTPException(status_code=400, detail="kind 只能是 excel 或 feishu")

    @application.get("/api/researches/{research_id}/exports/{file_name}")
    async def download_export(research_id: str, file_name: str) -> FileResponse:
        require_report(research_id)
        target = (export_dir(research_id) / file_name).resolve()
        if not target.is_relative_to(export_dir(research_id)) or not target.is_file():
            raise HTTPException(status_code=404, detail="导出产物不存在")
        media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" \
            if target.suffix == ".xlsx" else "application/octet-stream"
        return FileResponse(target, media_type=media, filename=target.name)
