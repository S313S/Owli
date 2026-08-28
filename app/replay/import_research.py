"""把跑过的研究导进一个库，成为一个**新 research**，供「从指定 goal 起跑」。

为什么是新 id 而不是就地改：
1. 源那一行是要反复对照的基线，就地跑等于把基线毁掉；
2. 源 research 可能已经在 `_schedulers` 里，就地起跑会被 `_claim_execution`
   挡下——「旧那套谁来停」的答案是**不停也不换**，新 id 起新的一套；
3. 新 id 从没起过跑，走 `_claim_execution` 零冲突。

唯一键这一关（D-015「upsert 只覆盖一个唯一键」的教训）：`evidence` 有两个唯一键，
`UNIQUE(report_id, permalink)` 与 `UNIQUE(report_id, platform, platform_item_id)`，
**两个都带 report_id 作用域**。换了 report_id 就撞不上源那批行，同库导入也安全。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_EVIDENCE_COLUMNS = (
    "goal_id", "agent_name", "engine", "platform", "source_type",
    "platform_item_id", "permalink", "title", "content_excerpt", "author_name",
    "author_meta", "source_keyword", "fetch_method", "published_at", "fetched_at",
    "raw_metrics", "normalized_score", "norm_method", "norm_context",
    "score_authority", "score_freshness", "score_crossref", "score_completeness",
    "score_independence", "rating_notes", "rated_by", "citation_no", "extra",
)

_JSON_EVIDENCE_COLUMNS = frozenset(
    {"author_meta", "raw_metrics", "norm_context", "extra"}
)


class ReplayImportError(ValueError):
    """底料读不出来，或要起跑的 goal 不在计划里。"""


@dataclass(frozen=True)
class ImportedResearch:
    research_id: str
    source_research_id: str
    from_goal: str | None
    evidence_copied: int
    chapters_copied: int
    chapters_reset: tuple[str, ...]
    runs_dir: Path


def _read_source(database: Path, research_id: str) -> dict[str, Any]:
    connection = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        report = connection.execute(
            "SELECT * FROM reports WHERE id = ?", (research_id,)
        ).fetchone()
        if report is None:
            raise ReplayImportError(f"{database} 里没有 {research_id}")
        if not report["plan_snapshot"]:
            raise ReplayImportError(f"{research_id} 没有 plan_snapshot，重放没有底可依")
        return {
            "report": dict(report),
            "evidence": [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM evidence WHERE report_id = ?", (research_id,)
                )
            ],
            "chapters": [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM chapter_progress WHERE research_id = ?",
                    (research_id,),
                )
            ],
        }
    finally:
        connection.close()


def _goals_from(snapshot: dict[str, Any], from_goal: str | None) -> set[str]:
    """`from_goal` 及其之后的 goal（按计划顺序）——这些是要重跑的。"""

    goal_ids = [str(goal["goal_id"]) for goal in snapshot.get("goals", [])]
    if from_goal is None:
        return set(goal_ids)
    if from_goal not in goal_ids:
        raise ReplayImportError(f"计划里没有 {from_goal}；可选 {goal_ids}")
    return set(goal_ids[goal_ids.index(from_goal):])


def _new_research_id() -> str:
    return f"r-{uuid.uuid4().hex[:12]}"


def _rewrite_snapshot(raw: str, source_id: str, research_id: str) -> dict[str, Any]:
    snapshot = json.loads(raw)
    snapshot["research_id"] = research_id
    for entry in snapshot.get("change_log", []):
        if entry.get("target_id") == source_id:
            entry["target_id"] = research_id
    return snapshot


def import_research(
    *,
    store: Any,
    source_database: Path,
    source_runs: Path,
    source_research_id: str,
    runs_root: Path,
    now_iso: str,
    from_goal: str | None = None,
    research_id: str | None = None,
    reset_done: bool = False,
) -> ImportedResearch:
    source = _read_source(Path(source_database), source_research_id)
    report = source["report"]
    research_id = research_id or _new_research_id()
    snapshot = _rewrite_snapshot(
        str(report["plan_snapshot"]), source_research_id, research_id
    )
    replay_goals = _goals_from(snapshot, from_goal)

    source_dir = Path(source_runs) / source_research_id
    if not source_dir.is_dir():
        raise ReplayImportError(f"底料产物目录不在：{source_dir}")
    target_dir = Path(runs_root) / research_id
    if target_dir.exists():
        raise ReplayImportError(f"产物目录已存在，换个 research_id：{target_dir}")

    try:
        return _write(
            store=store,
            report=report,
            snapshot=snapshot,
            research_id=research_id,
            source_research_id=source_research_id,
            source_database=Path(source_database),
            source_dir=source_dir,
            target_dir=target_dir,
            evidence=source["evidence"],
            chapters=source["chapters"],
            replay_goals=replay_goals,
            from_goal=from_goal,
            now_iso=now_iso,
            reset_done=reset_done,
        )
    except Exception:
        # 半份研究比没有更糟：工作板上会多出一个跑不动也删不掉的条目。
        # reports 一删，evidence / chapter_progress 靠外键 CASCADE 跟着走。
        _rollback(store, research_id, target_dir)
        raise


def _rollback(store: Any, research_id: str, target_dir: Path) -> None:
    shutil.rmtree(target_dir, ignore_errors=True)
    connection = sqlite3.connect(_database_of(store))
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("DELETE FROM reports WHERE id = ?", (research_id,))
        connection.commit()
    finally:
        connection.close()


def _database_of(store: Any) -> str:
    path = getattr(store, "_database_path", None)
    if path is None:
        raise ReplayImportError("store 没有 _database_path，无法做重放期的账本复位")
    return str(path)


def _write(
    *,
    store: Any,
    report: dict[str, Any],
    snapshot: dict[str, Any],
    research_id: str,
    source_research_id: str,
    source_database: Path,
    source_dir: Path,
    target_dir: Path,
    evidence: list[dict[str, Any]],
    chapters: list[dict[str, Any]],
    replay_goals: set[str],
    from_goal: str | None,
    now_iso: str,
    reset_done: bool,
) -> ImportedResearch:
    store.create_report(
        id=research_id,
        title=str(report["title"]),
        research_question=str(report["research_question"]),
        use_case=str(report["use_case"]),
        status="running",
        created_at=now_iso,
        plan_snapshot=snapshot,
        decision_balance=json.loads(report["decision_balance"] or "null"),
        engines_used=json.loads(report["engines_used"] or "null"),
        extra={
            **json.loads(report["extra"] or "{}"),
            # 溯源写进 extra JSON 列，不给 schema 加列（禁区；D-020 的先例）。
            "replay_of": source_research_id,
            "replay_from_goal": from_goal,
            "replay_source_database": str(Path(source_database).resolve()),
            "replay_created_at": now_iso,
        },
    )
    shutil.copytree(source_dir, target_dir)

    if evidence:
        store.add_evidence_batch([
            {
                "id": f"ev-{uuid.uuid4().hex[:20]}",
                "report_id": research_id,
                **{
                    column: (
                        json.loads(row[column])
                        if column in _JSON_EVIDENCE_COLUMNS and row[column] is not None
                        else row[column]
                    )
                    for column in _EVIDENCE_COLUMNS
                },
            }
            for row in evidence
        ])

    reset = _copy_chapters(
        store,
        chapters,
        research_id,
        replay_goals,
        now_iso,
        reset_done=reset_done,
        snapshot=snapshot,
        target_dir=target_dir,
    )
    return ImportedResearch(
        research_id=research_id,
        source_research_id=source_research_id,
        from_goal=from_goal,
        evidence_copied=len(evidence),
        chapters_copied=len(chapters),
        chapters_reset=reset,
        runs_dir=target_dir,
    )


def _chapter_paths(snapshot: dict[str, Any]) -> dict[tuple[str, str], Path]:
    """章/节 → 产物相对路径。节的产物在 `<父章产物去扩展名>/sec-N.md` 下。"""

    paths: dict[tuple[str, str], Path] = {}
    for goal in snapshot.get("goals", []):
        goal_id = str(goal["goal_id"])
        for index, agent in enumerate(goal.get("agents", []), start=1):
            chapter = agent.get("chapter") or {}
            chapter_id = str(chapter.get("chapter_id") or agent["agent_id"])
            output = Path(str(agent["output"]["path"]))
            paths[(goal_id, chapter_id)] = output
            section_root = output.parent / output.stem
            for number in range(1, len(snapshot.get("goals", [])) + 1):
                paths[(goal_id, f"{chapter_id}/sec-{number}")] = (
                    section_root / f"sec-{number}.md"
                )
    return paths


def _drop_artifact(target_dir: Path, relative: Path) -> None:
    """复位一章就把它上一轮的产物删掉——留着会冒充产物让 `file_exists` 假绿。"""

    (target_dir / relative).unlink(missing_ok=True)
    rejected = relative.with_suffix(f".rejected{relative.suffix}")
    (target_dir / rejected).unlink(missing_ok=True)


def _copy_chapters(
    store: Any,
    rows: list[dict[str, Any]],
    research_id: str,
    replay_goals: set[str],
    now_iso: str,
    *,
    reset_done: bool,
    snapshot: dict[str, Any],
    target_dir: Path,
) -> tuple[str, ...]:
    """章账本原样搬过去；要重跑的 goal 里，选中的章复位成 pending。

    默认只复位**非 done** 的章（「出问题就从出问题的地方接着跑」的字面意思）；
    `reset_done=True` 才连 done 一起复位，那是「这段整个重做」。
    """

    store.ensure_chapters(
        research_id,
        [
            {"goal_id": row["goal_id"], "chapter_id": row["chapter_id"]}
            for row in rows
        ],
        updated_at=now_iso,
        reset_running=False,
    )
    paths = _chapter_paths(snapshot)
    reset: list[str] = []
    connection = sqlite3.connect(_database_of(store))
    try:
        for row in rows:
            key = (row["goal_id"], row["chapter_id"])
            replayed = row["goal_id"] in replay_goals and (
                reset_done or row["status"] != "done"
            )
            if replayed:
                reset.append(f"{row['goal_id']}/{row['chapter_id']}")
                relative = paths.get(key)
                if relative is not None:
                    _drop_artifact(target_dir, relative)
                continue
            connection.execute(
                """
                UPDATE chapter_progress
                SET status = ?, attempts = ?, engine = ?, reason = ?,
                    engine_error = ?, conclusion_error = ?,
                    actual_output_path = ?, actual_count = ?, extra = ?,
                    updated_at = ?
                WHERE research_id = ? AND goal_id = ? AND chapter_id = ?
                """,
                (
                    row["status"], row["attempts"], row["engine"], row["reason"],
                    row["engine_error"], row["conclusion_error"],
                    row["actual_output_path"], row["actual_count"], row["extra"],
                    now_iso, research_id, row["goal_id"], row["chapter_id"],
                ),
            )
        connection.commit()
    finally:
        connection.close()
    return tuple(reset)
