"""Owli 的固定存储接口。"""

from __future__ import annotations

import json
import copy
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


__all__ = ["PlanSnapshotConflict", "Store"]

_EXTRA_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


class PlanSnapshotConflict(RuntimeError):
    """reports.plan_snapshot 的乐观锁版本不匹配。"""


def _json_text(value: Any) -> str | None:
    if value is None or isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _extra_text(extra: dict[str, Any]) -> str:
    if not isinstance(extra, dict):
        raise TypeError("extra 必须是 dict")
    for key in extra:
        if not _EXTRA_KEY_PATTERN.fullmatch(key):
            raise ValueError(f"extra 键必须是 snake_case 且不能以下划线开头：{key}")
    return json.dumps(extra, ensure_ascii=False, separators=(",", ":"))


def _value_type(value: Any) -> str:
    if isinstance(value, bool) or isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "real"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "text"


class Store:
    """只暴露 reports 与 evidence 的具名读写方法。"""

    def __init__(self, database_path: str | Path) -> None:
        self._database_path = Path(database_path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def create_report(
        self,
        *,
        id: str,
        title: str,
        research_question: str,
        created_at: str,
        use_case: str = "other",
        status: str = "running",
        completed_at: str | None = None,
        summary: str | None = None,
        summary_line: str | None = None,
        plan_snapshot: Any = None,
        decision_balance: Any = None,
        engines_used: Any = None,
        report_path: str | None = None,
        attachments: Any = None,
        feishu_doc_token: str | None = None,
        feishu_record_id: str | None = None,
        feishu_synced_at: str | None = None,
        feishu_sync_status: str = "pending",
        extra: dict[str, Any] | None = None,
    ) -> None:
        normalized_extra = {} if extra is None else extra
        extra_json = _extra_text(normalized_extra)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO reports (
                  id, title, research_question, use_case, status, created_at,
                  completed_at, summary, summary_line, plan_snapshot,
                  decision_balance, engines_used, report_path, attachments,
                  feishu_doc_token, feishu_record_id, feishu_synced_at,
                  feishu_sync_status, extra
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    id, title, research_question, use_case, status, created_at,
                    completed_at, summary, summary_line, _json_text(plan_snapshot),
                    _json_text(decision_balance), _json_text(engines_used), report_path,
                    _json_text(attachments), feishu_doc_token, feishu_record_id,
                    feishu_synced_at, feishu_sync_status, extra_json,
                ),
            )
            self._register_extra(connection, "reports", id, normalized_extra, set())

    def add_evidence(
        self,
        *,
        id: str,
        report_id: str,
        platform: str,
        permalink: str,
        fetched_at: str,
        goal_id: str | None = None,
        agent_name: str | None = None,
        engine: str | None = None,
        source_type: str = "post",
        platform_item_id: str | None = None,
        title: str | None = None,
        content_excerpt: str | None = None,
        author_name: str | None = None,
        author_meta: Any = None,
        source_keyword: str | None = None,
        fetch_method: str = "official_api",
        published_at: str | None = None,
        raw_metrics: Any = None,
        normalized_score: float | None = None,
        norm_method: str | None = None,
        norm_context: Any = None,
        score_authority: int | None = None,
        score_freshness: int | None = None,
        score_crossref: int | None = None,
        score_completeness: int | None = None,
        score_independence: int | None = None,
        rating_notes: str | None = None,
        rated_by: str | None = None,
        citation_no: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        normalized_extra = {} if extra is None else extra
        extra_json = _extra_text(normalized_extra)
        metrics_json = _json_text({} if raw_metrics is None else raw_metrics)
        with self._connect() as connection:
            existing_keys = self._existing_evidence_extra_keys(
                connection, report_id, normalized_extra
            )
            connection.execute(
                """
                INSERT INTO evidence (
                  id, report_id, goal_id, agent_name, engine, platform, source_type,
                  platform_item_id, permalink, title, content_excerpt, author_name,
                  author_meta, source_keyword, fetch_method, published_at, fetched_at,
                  raw_metrics, normalized_score, norm_method, norm_context,
                  score_authority, score_freshness, score_crossref,
                  score_completeness, score_independence, rating_notes, rated_by,
                  citation_no, extra
                ) VALUES (
                  ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                  ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    id, report_id, goal_id, agent_name, engine, platform, source_type,
                    platform_item_id, permalink, title, content_excerpt, author_name,
                    _json_text(author_meta), source_keyword, fetch_method, published_at,
                    fetched_at, metrics_json, normalized_score, norm_method,
                    _json_text(norm_context), score_authority, score_freshness,
                    score_crossref, score_completeness, score_independence,
                    rating_notes, rated_by, citation_no, extra_json,
                ),
            )
            self._register_extra(
                connection, "evidence", report_id, normalized_extra, existing_keys
            )

    def get_report(self, report_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM reports WHERE id = ?", (report_id,)
            ).fetchone()
        if row is None:
            return None
        report = dict(row)
        for field in (
            "plan_snapshot", "decision_balance", "engines_used", "attachments", "extra"
        ):
            if report[field] is not None:
                report[field] = json.loads(report[field])
        return report

    def save_plan_snapshot(
        self,
        report_id: str,
        *,
        snapshot: dict[str, Any],
        expected_rev: int,
    ) -> None:
        """整棵计划树写入具名列；expected_rev=0 只允许首次保存。"""
        with self._connect() as connection:
            cursor = self._update_plan_snapshot(
                connection, report_id, snapshot=snapshot, expected_rev=expected_rev
            )
            if cursor.rowcount != 1:
                raise PlanSnapshotConflict(
                    f"计划版本冲突：{report_id} 期望 rev={expected_rev}"
                )

    def save_plan_change(
        self,
        report_id: str,
        *,
        snapshot: dict[str, Any],
        expected_rev: int,
        feedback: dict[str, Any] | None,
    ) -> str | None:
        """计划变更与可选 feedback 同事务；feedback 故障降级为空标记。"""
        snapshot_to_save = copy.deepcopy(snapshot)
        feedback_id: str | None = None
        with self._connect() as connection:
            if feedback is not None:
                connection.execute("SAVEPOINT feedback_write")
                try:
                    self._insert_feedback(connection, feedback)
                except Exception:
                    connection.execute("ROLLBACK TO feedback_write")
                    connection.execute("RELEASE feedback_write")
                    snapshot_to_save["change_log"][-1]["feedback_id"] = None
                else:
                    connection.execute("RELEASE feedback_write")
                    feedback_id = str(feedback["id"])
            cursor = self._update_plan_snapshot(
                connection,
                report_id,
                snapshot=snapshot_to_save,
                expected_rev=expected_rev,
            )
            if cursor.rowcount != 1:
                raise PlanSnapshotConflict(
                    f"计划版本冲突：{report_id} 期望 rev={expected_rev}"
                )
        return feedback_id

    def finish_report(
        self,
        report_id: str,
        *,
        status: str,
        completed_at: str,
        summary: str | None = None,
        summary_line: str | None = None,
        report_path: str | None = None,
    ) -> None:
        """用具名字段完成或终止报告，禁止调用方拼接 SQL。"""
        if status not in {"completed", "failed"}:
            raise ValueError("finish_report.status 只能是 completed 或 failed")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE reports
                SET status = ?, completed_at = ?, summary = COALESCE(?, summary),
                    summary_line = COALESCE(?, summary_line),
                    report_path = COALESCE(?, report_path)
                WHERE id = ?
                """,
                (
                    status,
                    completed_at,
                    summary,
                    summary_line,
                    report_path,
                    report_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"报告不存在：{report_id}")

    def read_validation_path(self, path: str, report_id: str) -> Any:
        """按封闭路径读取校验数据；调用方不能传 SQL 或表名片段。"""
        if path == "evidence":
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT id FROM evidence WHERE report_id = ? ORDER BY citation_no, id",
                    (report_id,),
                ).fetchall()
            return [row["id"] for row in rows]
        if path == "report_tags":
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT tag FROM report_tags WHERE report_id = ? ORDER BY tag",
                    (report_id,),
                ).fetchall()
            return [row["tag"] for row in rows]

        parts = path.split(".")
        if len(parts) < 2 or parts[0] != "reports":
            raise ValueError(f"不支持的校验读取路径：{path}")
        report = self.get_report(report_id)
        if report is None:
            return None

        if parts[1] == "extra":
            current: Any = report["extra"]
            remaining = parts[2:]
        elif len(parts) == 2 and parts[1] in report:
            return report[parts[1]]
        else:
            raise ValueError(f"不支持的校验读取路径：{path}")

        for key in remaining:
            if not isinstance(current, dict) or key not in current:
                return None
            current = current[key]
        return current

    def _update_plan_snapshot(
        self,
        connection: sqlite3.Connection,
        report_id: str,
        *,
        snapshot: dict[str, Any],
        expected_rev: int,
    ) -> sqlite3.Cursor:
        return connection.execute(
            """
            UPDATE reports
            SET plan_snapshot = ?, decision_balance = ?, title = ?,
                research_question = ?, use_case = ?
            WHERE id = ? AND (
              (? = 0 AND plan_snapshot IS NULL)
              OR json_extract(plan_snapshot, '$.plan_rev') = ?
            )
            """,
            (
                _json_text(snapshot),
                _json_text(snapshot["decision_balance"]),
                snapshot["title"],
                snapshot["research_question"],
                snapshot["use_case"],
                report_id,
                expected_rev,
                expected_rev,
            ),
        )

    def _insert_feedback(
        self,
        connection: sqlite3.Connection,
        feedback: dict[str, Any],
    ) -> None:
        connection.execute(
            """
            INSERT INTO feedback (
              id, report_id, evidence_id, kind, target, before_value,
              after_value, reason, actor, created_at, applied, extra
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                feedback["id"],
                feedback["report_id"],
                feedback["evidence_id"],
                feedback["kind"],
                feedback["target"],
                _json_text(feedback["before_value"]),
                _json_text(feedback["after_value"]),
                feedback["reason"],
                feedback["actor"],
                feedback["created_at"],
                feedback["applied"],
                _json_text(feedback["extra"]),
            ),
        )

    def _existing_evidence_extra_keys(
        self,
        connection: sqlite3.Connection,
        report_id: str,
        extra: dict[str, Any],
    ) -> set[str]:
        existing: set[str] = set()
        for key in extra:
            json_path = f"$.{key}"
            row = connection.execute(
                """
                SELECT 1 FROM evidence
                WHERE report_id = ? AND json_type(extra, ?) IS NOT NULL
                LIMIT 1
                """,
                (report_id, json_path),
            ).fetchone()
            if row is not None:
                existing.add(key)
        return existing

    def _register_extra(
        self,
        connection: sqlite3.Connection,
        table_name: str,
        report_id: str,
        extra: dict[str, Any],
        existing_report_keys: set[str],
    ) -> None:
        del report_id  # 调用方显式传入报告锚点，本版仅用于语义自检。
        now = datetime.now(timezone.utc).isoformat()
        for key, value in extra.items():
            sample = json.dumps(value, ensure_ascii=False, separators=(",", ":"))[:200]
            report_increment = 0 if key in existing_report_keys else 1
            connection.execute(
                """
                INSERT INTO ext_key_registry (
                  table_name, key, value_type, first_seen_at, last_seen_at,
                  seen_count, report_count, sample_value
                ) VALUES (?, ?, ?, ?, ?, 1, 1, ?)
                ON CONFLICT(table_name, key) DO UPDATE SET
                  last_seen_at = excluded.last_seen_at,
                  seen_count = ext_key_registry.seen_count + 1,
                  report_count = ext_key_registry.report_count + ?,
                  sample_value = excluded.sample_value,
                  type_conflicts = ext_key_registry.type_conflicts +
                    CASE WHEN ext_key_registry.value_type = excluded.value_type
                         THEN 0 ELSE 1 END
                """,
                (
                    table_name,
                    key,
                    _value_type(value),
                    now,
                    now,
                    sample,
                    report_increment,
                ),
            )
