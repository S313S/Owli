"""历史报告粗筛与主引擎判重的稳定后端契约。"""

from __future__ import annotations

import asyncio
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Sequence


_QUERY_PART = re.compile(r"[A-Za-z0-9]+|[\u3400-\u9fff]+")
_CONFIDENCE = frozenset({"高", "中", "低"})
_REUSABLE_ELEMENTS = frozenset({"信息源组合", "采集方式", "报告骨架"})


@dataclass(frozen=True)
class RecallCandidate:
    report_id: str
    title: str
    research_question: str
    summary_line: str
    tags: tuple[str, ...]
    sources: tuple[str, ...]
    completed_at: str | None
    bm25_score: float | None
    keyword_score: float


@dataclass(frozen=True)
class CoarseRecallResult:
    query_mode: str
    candidates: tuple[RecallCandidate, ...]


@dataclass(frozen=True)
class DuplicateDecision:
    report_id: str
    same_item: bool
    confidence: str
    reason: str
    reusable_elements: tuple[str, ...]


@dataclass(frozen=True)
class RecallMatch:
    candidate: RecallCandidate
    same_item: bool | None
    confidence: str | None
    reason: str
    reusable_elements: tuple[str, ...]
    match_label: str


@dataclass(frozen=True)
class RecallResult:
    query_mode: str
    candidates: tuple[RecallCandidate, ...]
    matches: tuple[RecallMatch, ...]
    degraded: bool
    degrade_reason: str | None


DuplicateJudge = Callable[
    [str, Sequence[RecallCandidate]],
    Awaitable[Sequence[DuplicateDecision]],
]


def _normalized_query(query: str) -> str:
    normalized = str(query).strip()
    if not normalized:
        raise ValueError("召回查询词不得为空")
    return normalized


def _fts_expression(query: str) -> str | None:
    """把自然语言拆成去重 trigram 的 OR 查询，避免整句 AND 过严。"""

    trigrams: list[str] = []
    seen: set[str] = set()
    for part in _QUERY_PART.findall(query):
        for index in range(max(0, len(part) - 2)):
            value = part[index:index + 3]
            folded = value.casefold()
            if folded in seen:
                continue
            seen.add(folded)
            trigrams.append(value.replace('"', '""'))
            if len(trigrams) >= 64:
                break
        if len(trigrams) >= 64:
            break
    if not trigrams:
        return None
    return " OR ".join(f'"{value}"' for value in trigrams)


def _like_pattern(query: str) -> str:
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _candidate(row: sqlite3.Row) -> RecallCandidate:
    tags = tuple(item for item in str(row["tags"] or "").split() if item)
    sources = tuple(
        item for item in str(row["sources"] or "").split("\x1f") if item
    )
    return RecallCandidate(
        report_id=str(row["report_id"]),
        title=str(row["title"]),
        research_question=str(row["research_question"]),
        summary_line=str(row["summary_line"] or ""),
        tags=tags,
        sources=sources,
        completed_at=row["completed_at"],
        bm25_score=(
            float(row["bm25_score"])
            if row["bm25_score"] is not None
            else None
        ),
        keyword_score=float(row["keyword_score"]),
    )


class RecallRepository:
    """只暴露受控的历史候选读取，不向调用方开放裸 SQL。"""

    def __init__(self, database_path: str | Path) -> None:
        self._database_path = Path(database_path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def search(self, query: str, *, top_n: int = 50) -> CoarseRecallResult:
        normalized = _normalized_query(query)
        if not 1 <= top_n <= 50:
            raise ValueError("召回 top_n 必须在 1–50 之间")
        compact_length = len("".join(normalized.split()))
        expression = _fts_expression(normalized)
        if compact_length <= 2 or expression is None:
            return self._search_like(normalized, top_n=top_n)
        result = self._search_fts(expression, top_n=top_n)
        if len(result.candidates) >= top_n:
            return result
        supplemented = self._completed_candidates(
            exclude={item.report_id for item in result.candidates},
            limit=top_n - len(result.candidates),
        )
        return CoarseRecallResult(
            result.query_mode,
            result.candidates + supplemented,
        )

    def _search_fts(self, expression: str, *, top_n: int) -> CoarseRecallResult:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT recall_fts.report_id, recall_fts.title, recall_fts.tags,
                       recall_fts.summary_line, reports.research_question,
                       reports.completed_at, bm25(recall_fts) AS bm25_score,
                       -bm25(recall_fts) AS keyword_score,
                       COALESCE((
                         SELECT group_concat(platform, char(31))
                         FROM (
                           SELECT DISTINCT platform
                           FROM evidence
                           WHERE report_id = reports.id
                           ORDER BY platform
                         )
                       ), '') AS sources
                FROM recall_fts
                JOIN reports ON reports.id = recall_fts.report_id
                WHERE recall_fts MATCH ? AND reports.status = 'completed'
                ORDER BY bm25_score ASC, reports.completed_at DESC,
                         recall_fts.report_id ASC
                LIMIT ?
                """,
                (expression, top_n),
            ).fetchall()
        return CoarseRecallResult("fts5_bm25", tuple(map(_candidate, rows)))

    def _completed_candidates(
        self,
        *,
        exclude: set[str],
        limit: int,
    ) -> tuple[RecallCandidate, ...]:
        if limit <= 0:
            return ()
        excluded_sql = ""
        parameters: list[object] = []
        if exclude:
            placeholders = ",".join("?" for _ in exclude)
            excluded_sql = f" AND reports.id NOT IN ({placeholders})"
            parameters.extend(sorted(exclude))
        parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT reports.id AS report_id, reports.title,
                       COALESCE(recall_fts.tags, '') AS tags,
                       reports.summary_line, reports.research_question,
                       reports.completed_at, NULL AS bm25_score,
                       0 AS keyword_score,
                       COALESCE((
                         SELECT group_concat(platform, char(31))
                         FROM (
                           SELECT DISTINCT platform
                           FROM evidence
                           WHERE report_id = reports.id
                           ORDER BY platform
                         )
                       ), '') AS sources
                FROM reports
                LEFT JOIN recall_fts ON recall_fts.report_id = reports.id
                WHERE reports.status = 'completed'{excluded_sql}
                ORDER BY reports.completed_at DESC, reports.id ASC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        return tuple(map(_candidate, rows))

    def _search_like(self, query: str, *, top_n: int) -> CoarseRecallResult:
        pattern = _like_pattern(query)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT recall_fts.report_id, recall_fts.title, recall_fts.tags,
                       recall_fts.summary_line, reports.research_question,
                       reports.completed_at, NULL AS bm25_score,
                       (CASE WHEN recall_fts.title LIKE ? ESCAPE '\\' THEN 100 ELSE 0 END
                        + CASE WHEN reports.research_question LIKE ? ESCAPE '\\' THEN 60 ELSE 0 END
                        + CASE WHEN recall_fts.summary_line LIKE ? ESCAPE '\\' THEN 30 ELSE 0 END
                        + CASE WHEN recall_fts.tags LIKE ? ESCAPE '\\' THEN 20 ELSE 0 END
                       ) AS keyword_score,
                       COALESCE((
                         SELECT group_concat(platform, char(31))
                         FROM (
                           SELECT DISTINCT platform
                           FROM evidence
                           WHERE report_id = reports.id
                           ORDER BY platform
                         )
                       ), '') AS sources
                FROM recall_fts
                JOIN reports ON reports.id = recall_fts.report_id
                WHERE reports.status = 'completed' AND (
                  recall_fts.title LIKE ? ESCAPE '\\'
                  OR reports.research_question LIKE ? ESCAPE '\\'
                  OR recall_fts.summary_line LIKE ? ESCAPE '\\'
                  OR recall_fts.tags LIKE ? ESCAPE '\\'
                )
                ORDER BY keyword_score DESC, reports.completed_at DESC,
                         recall_fts.report_id ASC
                LIMIT ?
                """,
                (pattern,) * 8 + (top_n,),
            ).fetchall()
        return CoarseRecallResult("like", tuple(map(_candidate, rows)))


class RecallService:
    """执行粗筛和主引擎判重；主引擎失败只降低质量，不阻塞主流程。"""

    def __init__(
        self,
        repository: RecallRepository,
        *,
        judge: DuplicateJudge | None,
        judge_limit: int = 50,
        result_limit: int = 3,
        judge_timeout_seconds: float = 30.0,
    ) -> None:
        if not 1 <= judge_limit <= 50:
            raise ValueError("judge_limit 必须在 1–50 之间")
        if not 1 <= result_limit <= 3:
            raise ValueError("result_limit 必须在 1–3 之间")
        if not 0 < judge_timeout_seconds <= 60:
            raise ValueError("judge_timeout_seconds 必须在 0–60 秒之间")
        self._repository = repository
        self._judge = judge
        self._judge_limit = judge_limit
        self._result_limit = result_limit
        self._judge_timeout_seconds = float(judge_timeout_seconds)

    async def recall(self, query: str) -> RecallResult:
        coarse = self._repository.search(query, top_n=self._judge_limit)
        if not coarse.candidates:
            return RecallResult(coarse.query_mode, (), (), False, None)
        if (
            coarse.query_mode == "fts5_bm25"
            and not any(
                item.bm25_score is not None for item in coarse.candidates
            )
        ):
            return RecallResult(
                coarse.query_mode,
                coarse.candidates,
                (),
                False,
                None,
            )
        try:
            if self._judge is None:
                raise RuntimeError("主引擎判重器未配置")
            try:
                raw_decisions = await asyncio.wait_for(
                    self._judge(query, coarse.candidates),
                    timeout=self._judge_timeout_seconds,
                )
            except TimeoutError as exc:
                seconds = f"{self._judge_timeout_seconds:g}"
                raise TimeoutError(f"主引擎判重超过 {seconds} 秒") from exc
            decisions = tuple(raw_decisions)
            if not decisions:
                raise ValueError("主引擎判重至少返回一条结论")
            matches = self._validated_matches(coarse.candidates, decisions)
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            fallback_candidates = (
                coarse.candidates
                if coarse.query_mode == "like"
                else tuple(
                    item
                    for item in coarse.candidates
                    if item.bm25_score is not None
                )
            )
            return RecallResult(
                coarse.query_mode,
                coarse.candidates,
                tuple(
                    RecallMatch(
                        candidate=item,
                        same_item=None,
                        confidence=None,
                        reason="主引擎不可用，结果未经语义判断。",
                        reusable_elements=(),
                        match_label="关键词粗匹配",
                    )
                    for item in fallback_candidates[:self._result_limit]
                ),
                True,
                reason,
            )
        return RecallResult(
            coarse.query_mode,
            coarse.candidates,
            matches,
            False,
            None,
        )

    def _validated_matches(
        self,
        candidates: Sequence[RecallCandidate],
        decisions: Sequence[DuplicateDecision],
    ) -> tuple[RecallMatch, ...]:
        by_id = {item.report_id: item for item in candidates}
        seen: set[str] = set()
        matches: list[RecallMatch] = []
        for decision in decisions:
            if decision.report_id not in by_id:
                raise ValueError(f"主引擎返回了粗筛候选之外的 report_id：{decision.report_id}")
            if decision.report_id in seen:
                raise ValueError(f"主引擎重复返回 report_id：{decision.report_id}")
            if not isinstance(decision.same_item, bool):
                raise ValueError("same_item 必须是 boolean")
            if decision.confidence not in _CONFIDENCE:
                raise ValueError(f"confidence 不在闭集：{decision.confidence}")
            reason = decision.reason.strip()
            if not reason:
                raise ValueError("主引擎判重理由不得为空")
            unknown = set(decision.reusable_elements) - _REUSABLE_ELEMENTS
            if unknown:
                raise ValueError(f"可复用要素不在闭集：{sorted(unknown)}")
            seen.add(decision.report_id)
            matches.append(RecallMatch(
                candidate=by_id[decision.report_id],
                same_item=decision.same_item,
                confidence=decision.confidence,
                reason=reason,
                reusable_elements=tuple(decision.reusable_elements),
                match_label="主引擎语义判断",
            ))
            if len(matches) >= self._result_limit:
                break
        return tuple(matches)
