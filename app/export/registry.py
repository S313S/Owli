"""导出登记（§DLV-1 货 5）：产物与飞书状态落 `reports.extra`，不改 dao。

`finish_report` 不收 `attachments`，dao 也没有写 `feishu_*` 四列的具名方法
（禁区，奏折已递）；本包一律走 `reports.extra.exports[] / extra.feishu`，
经 dao 既有 `_register_extra` 登记扩展键。四列写入待 dao 加 `set_feishu_sync`。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Mapping


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _update_extra(store: Any, report_id: str, mutate) -> dict[str, Any]:
    with store._connect() as connection:  # noqa: SLF001 — 只用 dao 自己的连接与登记机制
        row = connection.execute("SELECT extra FROM reports WHERE id = ?", (report_id,)).fetchone()
        if row is None:
            raise KeyError(f"报告不存在：{report_id}")
        extra = json.loads(row["extra"] or "{}")
        existing = set(extra)
        changed = mutate(extra)
        connection.execute(
            "UPDATE reports SET extra = ? WHERE id = ?",
            (json.dumps(extra, ensure_ascii=False, separators=(",", ":")), report_id),
        )
        store._register_extra(connection, "reports", report_id, changed, existing)  # noqa: SLF001
    return extra


def record_export(store: Any, report_id: str, *, kind: str, path: str, url: str | None,
                  desc: str | None = None) -> dict[str, Any]:
    """追加一条导出记录（同 kind 同 path 只保留最新一条）。"""
    record = {"kind": kind, "path": path, "url": url, "desc": desc, "created_at": _now()}

    def mutate(extra: dict[str, Any]) -> dict[str, Any]:
        kept = [x for x in (extra.get("exports") or []) if not (x.get("kind") == kind and x.get("path") == path)]
        extra["exports"] = [*kept, record]
        return {"exports": extra["exports"]}

    _update_extra(store, report_id, mutate)
    return record


def record_feishu(store: Any, report_id: str, status: str, **fields: Any) -> dict[str, Any]:
    """飞书同步状态落 `extra.feishu`（status ∈ pending|synced|failed|skipped）。"""
    if status not in {"pending", "synced", "failed", "skipped"}:
        raise ValueError("feishu status 不在闭集")
    payload = {"status": status, "synced_at": _now(), **{k: v for k, v in fields.items() if v is not None}}

    def mutate(extra: dict[str, Any]) -> dict[str, Any]:
        merged = {**(extra.get("feishu") or {}), **payload}
        extra["feishu"] = merged
        return {"feishu": merged}

    return _update_extra(store, report_id, mutate)["feishu"]
