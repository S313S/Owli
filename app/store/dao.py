"""Owli 的固定存储接口。"""

from __future__ import annotations

import json
import copy
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


__all__ = ["PlanSnapshotConflict", "Store"]

_EXTRA_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
_NORM_METHODS = {
    "percentile_in_batch", "percentile_in_window", "log_zscore_in_window", "none",
}
_NORM_CONTEXT_KEYS = {
    "scope", "platform", "metric", "n", "formula", "stats", "computed_at",
}
_NORM_NONE_REASONS = {
    "insufficient_sample", "no_metric_available", "metric_not_collected",
}
_PRIMARY_METRICS = {
    "hacker_news": "points", "product_hunt": "votes_count", "x": "like_count",
    "bilibili": "view", "xhs": "liked_count", "douyin": "digg_count",
    "reddit": None, "web_search": None,
}
_SCORE_FIELDS = (
    "score_authority", "score_freshness", "score_crossref",
    "score_completeness", "score_independence",
)
_AUTHORITY_KINDS = {
    "first_party_official", "verified_principal", "institutional_primary",
    "named_secondary", "community_high_signal", "anonymous_or_unverifiable",
    "content_farm",
}
_CONTENT_KINDS = {
    "product_launch", "market_data", "user_opinion", "industry_view", "reference",
}
_INTEREST_RELATIONS = {
    "arms_length", "disclosed_interest", "undisclosed_interest",
}
_CROSSREF_VERDICTS = {"PASS", "WEAK", "SINGLE", "CONFLICT"}
_EVIDENCE_DEFAULTS: dict[str, Any] = {
    "goal_id": None,
    "agent_name": None,
    "engine": None,
    "source_type": "post",
    "platform_item_id": None,
    "title": None,
    "content_excerpt": None,
    "author_name": None,
    "author_meta": None,
    "source_keyword": None,
    "fetch_method": "official_api",
    "published_at": None,
    "raw_metrics": None,
    "normalized_score": None,
    "norm_method": None,
    "norm_context": None,
    "score_authority": None,
    "score_freshness": None,
    "score_crossref": None,
    "score_completeness": None,
    "score_independence": None,
    "rating_notes": None,
    "rated_by": None,
    "citation_no": None,
    "extra": None,
}
_EVIDENCE_REQUIRED = {"id", "report_id", "platform", "permalink", "fetched_at"}
_EVIDENCE_FIELDS = _EVIDENCE_REQUIRED | set(_EVIDENCE_DEFAULTS)
_CHAPTER_TERMINAL = {"done", "missing", "deferred"}
_CHAPTER_REASONS = {
    "empty_result", "tool_unavailable", "quota_exhausted", "retry_exhausted",
    "conclusion_invalid", "timeout",
}


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


def _string_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _validate_evidence_extra(extra: dict[str, Any]) -> None:
    list_keys = ("claim_ids", "crossref_peers", "crossref_conflicts")
    for key in list_keys:
        if key in extra and not _string_list(extra[key]):
            raise ValueError(f"evidence.extra.{key} 必须是 string[]")
    for key in ("origin_key", "crossref_cluster", "rating_override_reason"):
        if key in extra and (not isinstance(extra[key], str) or not extra[key]):
            raise ValueError(f"evidence.extra.{key} 必须是非空字符串")
    if "crossref_n_clusters" in extra and (
        not isinstance(extra["crossref_n_clusters"], int)
        or isinstance(extra["crossref_n_clusters"], bool)
        or extra["crossref_n_clusters"] < 1
    ):
        raise ValueError("evidence.extra.crossref_n_clusters 必须是正整数")
    closed_sets = {
        "crossref_verdict": _CROSSREF_VERDICTS,
        "authority_kind": _AUTHORITY_KINDS,
        "content_kind": _CONTENT_KINDS,
        "interest_relation": _INTEREST_RELATIONS,
    }
    for key, allowed in closed_sets.items():
        if key in extra and extra[key] not in allowed:
            raise ValueError(f"evidence.extra.{key} 不在闭集：{extra[key]!r}")
    if "crossref_secondary" in extra and not isinstance(extra["crossref_secondary"], dict):
        raise ValueError("evidence.extra.crossref_secondary 必须是 object")
    for claim_id, result in (extra.get("crossref_secondary") or {}).items():
        if not isinstance(claim_id, str) or not claim_id or not isinstance(result, dict):
            raise ValueError("evidence.extra.crossref_secondary 必须按 claim_id 映射 object")
        if result.get("verdict") not in _CROSSREF_VERDICTS:
            raise ValueError("evidence.extra.crossref_secondary.verdict 不在闭集")


def _validate_normalization(payload: dict[str, Any]) -> None:
    method = payload["norm_method"]
    score = payload["normalized_score"]
    context = payload["norm_context"]
    if method is None:
        if score is not None or context is not None:
            raise ValueError("normalized_score/norm_context 存在时 norm_method 不能为空")
        return
    if method not in _NORM_METHODS:
        raise ValueError(f"norm_method 不在闭集：{method!r}")
    if not isinstance(context, dict):
        raise ValueError("norm_context 必须是 object")
    missing = sorted(_NORM_CONTEXT_KEYS - set(context))
    if missing:
        raise ValueError(f"norm_context 缺必填键：{missing}")
    if context.get("platform") != payload["platform"]:
        raise ValueError("norm_context.platform 与 evidence.platform 不一致")
    if context.get("scope") not in {"batch", "window"}:
        raise ValueError("norm_context.scope 只能是 batch 或 window")
    if payload["platform"] == "x" and context.get("sampling") != "post_filtered_local":
        raise ValueError("X 归一化必须标记 sampling=post_filtered_local")
    expected_metric = _PRIMARY_METRICS.get(payload["platform"])
    if context.get("metric") != expected_metric:
        raise ValueError("norm_context.metric 与平台主指标不一致")
    n = context.get("n")
    if not isinstance(n, int) or isinstance(n, bool) or n < 0:
        raise ValueError("norm_context.n 必须是非负整数")
    if method == "none":
        if score is not None:
            raise ValueError("norm_method=none 时 normalized_score 必须为 NULL")
        if context.get("reason") not in _NORM_NONE_REASONS:
            raise ValueError("norm_method=none 时 norm_context.reason 不在闭集")
        return
    if method == "percentile_in_batch" and (
        context.get("scope") != "batch" or n < 20
    ):
        raise ValueError("percentile_in_batch 要求 scope=batch 且 n>=20")
    if method in {"percentile_in_window", "log_zscore_in_window"} and (
        context.get("scope") != "window" or n < 50
    ):
        raise ValueError(f"{method} 要求 scope=window 且 n>=50")
    if not isinstance(score, (int, float)) or isinstance(score, bool) or not 0 <= score <= 1:
        raise ValueError("启用归一化时 normalized_score 必须是 0–1 数值")
    if context.get("scope") == "window" and not {
        "window_days", "fallback_from",
    } <= set(context):
        raise ValueError("窗口归一化缺 window_days/fallback_from")


def _prepare_evidence(values: dict[str, Any]) -> dict[str, Any]:
    unknown = set(values) - _EVIDENCE_FIELDS - {"score_total", "grade"}
    if unknown:
        raise TypeError(f"evidence 含未知字段：{sorted(unknown)}")
    missing = sorted(key for key in _EVIDENCE_REQUIRED if not values.get(key))
    if missing:
        raise ValueError(f"evidence 缺必填字段：{missing}")
    payload = {**_EVIDENCE_DEFAULTS, **{key: value for key, value in values.items() if key in _EVIDENCE_FIELDS}}
    payload["extra"] = {} if payload["extra"] is None else payload["extra"]
    payload["raw_metrics"] = {} if payload["raw_metrics"] is None else payload["raw_metrics"]
    if not isinstance(payload["raw_metrics"], dict):
        raise TypeError("raw_metrics 必须是 dict")
    _extra_text(payload["extra"])
    _validate_evidence_extra(payload["extra"])
    _validate_normalization(payload)
    score_values = [payload[field] for field in _SCORE_FIELDS]
    if any(value is not None for value in score_values):
        if not all(
            isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 2
            for value in score_values
        ):
            raise ValueError("五维分必须同时提供五个 0–2 整数")
        if not payload["rating_notes"]:
            raise ValueError("有五维分时 rating_notes 必填")
    if payload["rating_notes"] is not None:
        from app.reliability.scoring import rating_notes_problem

        problem = rating_notes_problem(payload["rating_notes"], payload)
        if problem is not None:
            raise ValueError(f"rating_notes 非法：{problem}")
    return payload


class Store:
    """只暴露 reports 与 evidence 的具名读写方法。"""

    def __init__(self, database_path: str | Path) -> None:
        self._database_path = Path(database_path)
        self._plan_events: list[Any] = []

    def on_plan_event(self, event: Any) -> None:
        """暂存计划生成事件，供同一请求的上层事件缓冲读取。"""
        self._plan_events.append(event)

    @property
    def plan_events(self) -> tuple[Any, ...]:
        """只读返回本 Store 实例收到的计划生成事件。"""
        return tuple(self._plan_events)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def append_event(
        self,
        research_id: str,
        *,
        event_type: str,
        payload: Mapping[str, Any],
        created_at: str,
    ) -> dict[str, Any]:
        """原子分配 research 内序号并写事件；序号不依赖进程内状态。"""

        normalized_research_id = str(research_id).strip()
        normalized_type = str(event_type).strip()
        if not normalized_research_id:
            raise ValueError("事件 research_id 不得为空")
        if not normalized_type:
            raise ValueError("事件 type 不得为空")
        frozen_payload = copy.deepcopy(dict(payload))
        payload_json = json.dumps(
            frozen_payload, ensure_ascii=False, separators=(",", ":")
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            sequence = int(connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM events WHERE research_id = ?",
                (normalized_research_id,),
            ).fetchone()[0])
            connection.execute(
                """
                INSERT INTO events(research_id, sequence, type, payload, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    normalized_research_id,
                    sequence,
                    normalized_type,
                    payload_json,
                    str(created_at),
                ),
            )
        return {
            "research_id": normalized_research_id,
            "sequence": sequence,
            "type": normalized_type,
            "payload": frozen_payload,
            "created_at": str(created_at),
        }

    def list_events_window(
        self,
        research_id: str,
        *,
        created_since: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        """读取时间与条数窗口的交集；倒序限量后恢复为 sequence 升序。"""

        if limit <= 0:
            raise ValueError("事件窗口 limit 必须大于 0")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT research_id, sequence, type, payload, created_at
                FROM events
                WHERE research_id = ? AND created_at >= ?
                ORDER BY sequence DESC
                LIMIT ?
                """,
                (research_id, created_since, limit),
            ).fetchall()
        result = []
        for row in reversed(rows):
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            result.append(item)
        return result

    def list_running_reports(self) -> list[dict[str, Any]]:
        """返回需要在进程启动时恢复运行态的报告。"""

        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM reports WHERE status = 'running' ORDER BY created_at, id"
            ).fetchall()
        reports = []
        for row in rows:
            report = dict(row)
            for field in (
                "plan_snapshot", "decision_balance", "engines_used", "attachments", "extra"
            ):
                if report[field] is not None:
                    report[field] = json.loads(report[field])
            reports.append(report)
        return reports

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
        payload = _prepare_evidence(
            {key: value for key, value in locals().items() if key != "self"}
        )
        with self._connect() as connection:
            self._insert_evidence(connection, payload)

    def add_evidence_batch(self, evidence_items: Iterable[Mapping[str, Any]]) -> None:
        """同事务批量写 evidence；任一条不合格或冲突则整批回滚。"""
        payloads = [_prepare_evidence(dict(item)) for item in evidence_items]
        with self._connect() as connection:
            for payload in payloads:
                self._insert_evidence(connection, payload)

    def _insert_evidence(
        self, connection: sqlite3.Connection, payload: dict[str, Any]
    ) -> None:
        normalized_extra = payload["extra"]
        existing_keys = self._existing_evidence_extra_keys(
            connection, payload["report_id"], normalized_extra
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
                payload["id"], payload["report_id"], payload["goal_id"],
                payload["agent_name"], payload["engine"], payload["platform"],
                payload["source_type"], payload["platform_item_id"],
                payload["permalink"], payload["title"], payload["content_excerpt"],
                payload["author_name"], _json_text(payload["author_meta"]),
                payload["source_keyword"], payload["fetch_method"],
                payload["published_at"], payload["fetched_at"],
                _json_text(payload["raw_metrics"]), payload["normalized_score"],
                payload["norm_method"], _json_text(payload["norm_context"]),
                payload["score_authority"], payload["score_freshness"],
                payload["score_crossref"], payload["score_completeness"],
                payload["score_independence"], payload["rating_notes"],
                payload["rated_by"], payload["citation_no"],
                _extra_text(normalized_extra),
            ),
        )
        self._register_extra(
            connection, "evidence", payload["report_id"], normalized_extra, existing_keys
        )

    def ensure_chapters(
        self,
        research_id: str,
        chapters: Iterable[Mapping[str, Any]],
        *,
        updated_at: str,
        reset_running: bool = True,
    ) -> None:
        """登记计划章；保留终态，并把上次中断的 running 恢复为 pending。"""

        rows = []
        for item in chapters:
            goal_id = str(item.get("goal_id", "")).strip()
            chapter_id = str(item.get("chapter_id", "")).strip()
            if not goal_id or not chapter_id:
                raise ValueError("章节登记必须含 goal_id/chapter_id")
            rows.append((research_id, goal_id, chapter_id, updated_at))
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT OR IGNORE INTO chapter_progress (
                  research_id, goal_id, chapter_id, updated_at
                ) VALUES (?, ?, ?, ?)
                """,
                rows,
            )
            if reset_running:
                connection.execute(
                    """
                    UPDATE chapter_progress
                    SET status = 'pending', updated_at = ?
                    WHERE research_id = ? AND status = 'running'
                    """,
                    (updated_at, research_id),
                )

    def start_chapter(
        self,
        research_id: str,
        goal_id: str,
        chapter_id: str,
        *,
        engine: str,
        updated_at: str,
    ) -> bool:
        if engine not in {"claude", "codex"}:
            raise ValueError("chapter engine 只能是 claude 或 codex")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE chapter_progress
                SET status = 'running', attempts = attempts + 1,
                    engine = ?, reason = NULL, engine_error = NULL,
                    conclusion_error = NULL, updated_at = ?
                WHERE research_id = ? AND goal_id = ? AND chapter_id = ?
                  AND status IN ('pending','deferred','running')
                """,
                (engine, updated_at, research_id, goal_id, chapter_id),
            )
        return cursor.rowcount == 1

    def finish_chapter(
        self,
        research_id: str,
        goal_id: str,
        chapter_id: str,
        *,
        status: str,
        reason: str | None,
        actual_output_path: str | None,
        actual_count: int | None,
        engine_error: str | None = None,
        conclusion_error: str | None = None,
        updated_at: str,
    ) -> None:
        if status not in _CHAPTER_TERMINAL:
            raise ValueError("章终态只能是 done、missing 或 deferred")
        if status == "done" and reason is not None:
            raise ValueError("done 章不得带 reason")
        if status == "missing" and reason not in _CHAPTER_REASONS:
            raise ValueError("missing 章必须带闭集 reason")
        if status == "deferred" and reason not in _CHAPTER_REASONS:
            raise ValueError("deferred 章必须带闭集 reason")
        if actual_count is not None and actual_count < 0:
            raise ValueError("actual_count 不得为负数")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE chapter_progress
                SET status = ?, reason = ?, actual_output_path = ?,
                    actual_count = ?, engine_error = ?, conclusion_error = ?,
                    updated_at = ?
                WHERE research_id = ? AND goal_id = ? AND chapter_id = ?
                """,
                (
                    status, reason, actual_output_path, actual_count,
                    engine_error, conclusion_error, updated_at,
                    research_id, goal_id, chapter_id,
                ),
            )
        if cursor.rowcount != 1:
            raise KeyError(f"章节账本不存在：{research_id}/{goal_id}/{chapter_id}")

    def reset_running_chapter(
        self,
        research_id: str,
        goal_id: str,
        chapter_id: str,
        *,
        updated_at: str,
    ) -> bool:
        """在跑章被 /stop 打断时把 running 复位成 pending，供恢复后重跑。

        与 ensure_chapters(reset_running=True) 的恢复初始化同口径，只是收敛到单章：
        章没跑完就没有终态，账本不能留 running 幽灵。attempts 累计值保留不动。
        """

        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE chapter_progress
                SET status = 'pending', reason = NULL,
                    engine_error = NULL, conclusion_error = NULL,
                    updated_at = ?
                WHERE research_id = ? AND goal_id = ? AND chapter_id = ?
                  AND status = 'running'
                """,
                (updated_at, research_id, goal_id, chapter_id),
            )
        return cursor.rowcount == 1

    def reset_retry_exhausted_chapters(
        self,
        research_id: str,
        goal_id: str,
        chapter_ids: Iterable[str],
        *,
        updated_at: str,
    ) -> list[str]:
        """章级重试前，把传输耗尽（missing/retry_exhausted）的子节复位成 pending 重新派活。

        只放行 `retry_exhausted` 这一个 reason：`quota_exhausted` / `tool_unavailable`
        等闭集原因是「这一轮问不出来」，重新派活只会白烧额度，仍旧跳过。
        """

        wanted = [
            str(chapter_id) for chapter_id in chapter_ids if str(chapter_id).strip()
        ]
        if not wanted:
            return []
        reset: list[str] = []
        with self._connect() as connection:
            for chapter_id in wanted:
                cursor = connection.execute(
                    """
                    UPDATE chapter_progress
                    SET status = 'pending', reason = NULL,
                        actual_output_path = NULL, actual_count = NULL,
                        engine_error = NULL, conclusion_error = NULL,
                        updated_at = ?
                    WHERE research_id = ? AND goal_id = ? AND chapter_id = ?
                      AND status = 'missing' AND reason = 'retry_exhausted'
                    """,
                    (updated_at, research_id, goal_id, chapter_id),
                )
                if cursor.rowcount == 1:
                    reset.append(chapter_id)
        return reset

    def reset_done_chapters(
        self,
        research_id: str,
        goal_id: str,
        chapter_ids: Iterable[str],
        *,
        updated_at: str,
    ) -> None:
        """父章拼装校验失败时，只恢复明确列出的 done 子节供下一轮重写。"""

        rows = [
            (updated_at, research_id, goal_id, str(chapter_id))
            for chapter_id in chapter_ids
            if str(chapter_id).strip()
        ]
        if not rows:
            return
        with self._connect() as connection:
            connection.executemany(
                """
                UPDATE chapter_progress
                SET status = 'pending', reason = NULL,
                    actual_output_path = NULL, actual_count = NULL,
                    engine_error = NULL, conclusion_error = NULL,
                    updated_at = ?
                WHERE research_id = ? AND goal_id = ? AND chapter_id = ?
                  AND status = 'done'
                """,
                rows,
            )

    def list_chapters(self, research_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT research_id, goal_id, chapter_id, status, attempts,
                       engine, reason, engine_error, conclusion_error,
                       actual_output_path, actual_count, updated_at
                FROM chapter_progress
                WHERE research_id = ?
                ORDER BY goal_id, chapter_id
                """,
                (research_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def runnable_chapter_keys(self, research_id: str) -> set[tuple[str, str]]:
        return {
            (row["goal_id"], row["chapter_id"])
            for row in self.list_chapters(research_id)
            if row["status"] in {"pending", "deferred"}
        }

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

    def get_drafting_report(self, research_question: str) -> dict[str, Any] | None:
        """读取调用方已建、尚未写入计划快照的最新报告。"""
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM reports
                WHERE research_question = ? AND plan_snapshot IS NULL
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (research_question,),
            ).fetchone()
        if row is None:
            return None
        report = dict(row)
        for field in ("decision_balance", "engines_used", "attachments", "extra"):
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
        """兼容单条计划变更；批量实现由 save_plan_changes 统一承担。"""
        feedbacks = [] if feedback is None else [feedback]
        saved = self.save_plan_changes(
            report_id,
            snapshot=snapshot,
            expected_rev=expected_rev,
            feedbacks=feedbacks,
        )
        return saved[0] if saved else None

    def save_plan_changes(
        self,
        report_id: str,
        *,
        snapshot: dict[str, Any],
        expected_rev: int,
        feedbacks: list[dict[str, Any]],
    ) -> list[str]:
        """一次 PUT 的多条变更与 feedback 同事务，单条 feedback 可降级。"""
        snapshot_to_save = copy.deepcopy(snapshot)
        feedback_ids: list[str] = []
        with self._connect() as connection:
            for index, feedback in enumerate(feedbacks):
                savepoint = f"feedback_write_{index}"
                connection.execute(f"SAVEPOINT {savepoint}")
                try:
                    self._insert_feedback(connection, feedback)
                except Exception:
                    connection.execute(f"ROLLBACK TO {savepoint}")
                    connection.execute(f"RELEASE {savepoint}")
                    change_id = feedback["extra"]["change_id"]
                    for change in snapshot_to_save["change_log"]:
                        if change.get("change_id") == change_id:
                            change["feedback_id"] = None
                            break
                else:
                    connection.execute(f"RELEASE {savepoint}")
                    feedback_ids.append(str(feedback["id"]))
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
        return feedback_ids

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
