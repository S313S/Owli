"""§DLV-1 货 4/5：导出 API、下载白名单、登记进 reports.extra。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from tests.test_dlv1_delivery import _app, _seed_evidence, _seed_history, _write_json_report

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _run(application, method: str, path: str, **kwargs) -> httpx.Response:
    async def exercise() -> httpx.Response:
        async with application.router.lifespan_context(application):
            transport = httpx.ASGITransport(app=application)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.request(method, path, **kwargs)
    return asyncio.run(exercise())


def test_导出_excel_可下载且登记进_extra(tmp_path: Path) -> None:
    from app.store.dao import Store

    database, research_id, report_path = _seed_history(tmp_path)
    _write_json_report(report_path)
    _seed_evidence(database, research_id)
    application = _app(tmp_path, database)
    base = f"/api/researches/{research_id}"

    response = _run(application, "POST", f"{base}/export", json={"kind": "excel"})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["url"] == f"{base}/exports/{research_id}.xlsx"
    assert Path(data["path"]).is_file()

    download = _run(application, "GET", data["url"])
    assert download.status_code == 200 and download.headers["content-type"].startswith(XLSX)
    assert download.content[:2] == b"PK"
    assert _run(application, "GET", f"{base}/exports/../../owli.db").status_code == 404
    assert _run(application, "GET", f"{base}/exports/nope.xlsx").status_code == 404

    exports = Store(database).get_report(research_id)["extra"]["exports"]
    assert [(x["kind"], x["url"]) for x in exports] == [("excel", data["url"])]
    assert _run(application, "GET", base).json()["data"]["exports"] == exports
    assert _run(application, "GET", f"{base}/report").json()["data"]["exports"] == exports
    _run(application, "POST", f"{base}/export", json={"kind": "excel"})
    assert len(Store(database).get_report(research_id)["extra"]["exports"]) == 1  # 同产物只留最新
    assert _run(application, "POST", f"{base}/export", json={"kind": "pdf"}).status_code == 400
