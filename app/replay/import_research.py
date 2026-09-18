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
from typing import Any, Mapping

from app.adapters.transcript import TRANSCRIPT_SUFFIX
# §SEL-3：路径改写沿用沙盒那一份（记忆 `reuse-before-rewrite`：重写出来的差异是静默的），
# 不另写一份——两边要改的是同一列、同一个含义。
from app.replay.sandbox import _rebase_ledger_paths

#: 复制时重发、不从源行抄的列：新研究要新主键、挂到新 research 上。
_EVIDENCE_IDENTITY_COLUMNS = frozenset({"id", "report_id"})

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


def _source_lineage(database: Path, research_id: str) -> tuple[str, ...]:
    """源研究自己的 id + 它一路 replay 上来的祖先 id（自近及远）。

    账本里的 `actual_output_path` 带的是**当初真跑出那份产物的那个研究**的 id。
    源自己若是导入来的，它的账本就还带着上一代的 id——这一列从来没被改写过，
    所以一份账本里能同时出现好几代（真机 `r-3b3482ca7f8b` 的账本里 104 行是
    `r-20271e8a5028`、4 行 `r-f9bb30969cf2`、2 行 `r-96e0257a86b4`，**一行都不是**
    它自己）。只认「直接源」那一个 id，这次导入一行都改不动。

    id 不靠猜：走 `reports.extra.replay_of` 这条自己写下的溯源链（§RP-1 落的那格）。
    链断在库外（祖先那一行不在本库里，比如它跑在另一台 worktree 的库里）时，
    **它的 id 仍从子代的 `replay_of` 里拿到**，只是走不动下一跳，就停在那儿。
    """

    lineage: list[str] = []
    seen: set[str] = set()
    current: str | None = str(research_id)
    connection = sqlite3.connect(f"file:{Path(database).resolve()}?mode=ro", uri=True)
    try:
        while current and current not in seen:
            seen.add(current)
            lineage.append(current)
            row = connection.execute(
                "SELECT extra FROM reports WHERE id = ?", (current,)
            ).fetchone()
            if row is None:
                break
            try:
                extra = json.loads(row[0] or "{}")
            except (TypeError, ValueError):
                break
            parent = extra.get("replay_of") if isinstance(extra, Mapping) else None
            current = str(parent).strip() if isinstance(parent, str) and parent.strip() else None
    finally:
        connection.close()
    return tuple(lineage)


def _replay_chapters(
    only_chapters: list[str] | None,
) -> tuple[set[str], set[str]] | None:
    """§OBS-2 货 6「重跑这节」：把选中的章展开成 (要复位的行, 要连子孙一起复位的章)。

    - 点名 `ch-4/sec-2`：复位它**和它的父章** `ch-4`——只复位 sec 行的话父章还是
      done，节化撰写压根不会被走到；同章其它 done 的节届时照旧跳过。
    - 点名 `ch-4`（整章）：连它下面的节一起复位。
    """

    if not only_chapters:
        return None
    exact: set[str] = set()
    with_children: set[str] = set()
    for chapter_id in only_chapters:
        chapter = str(chapter_id).strip()
        if not chapter:
            continue
        exact.add(chapter)
        if "/" in chapter:
            exact.add(chapter.split("/", 1)[0])
        else:
            with_children.add(chapter)
    return (exact, with_children) if exact else None


def _is_replayed(
    row: Mapping[str, Any], replay_goals: set[str],
    wanted: tuple[set[str], set[str]] | None, *, reset_done: bool,
) -> bool:
    if row["goal_id"] not in replay_goals:
        return False
    if wanted is not None:
        exact, with_children = wanted
        chapter_id = str(row["chapter_id"])
        parent = chapter_id.split("/", 1)[0]
        return chapter_id in exact or (parent in with_children and parent != chapter_id)
    return reset_done or row["status"] != "done"


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
    only_chapters: list[str] | None = None,
) -> ImportedResearch:
    source = _read_source(Path(source_database), source_research_id)
    report = source["report"]
    research_id = research_id or _new_research_id()
    snapshot = _rewrite_snapshot(
        str(report["plan_snapshot"]), source_research_id, research_id
    )
    replay_goals = _goals_from(snapshot, from_goal)
    if only_chapters and from_goal:
        # 章号只在 goal 内唯一（每个 goal 都有 ch-1）；点名重跑某章时
        # 就把范围收到那一个 goal，免得同名章在后面的 goal 里跟着被复位。
        replay_goals = {from_goal}

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
            only_chapters=only_chapters,
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


def _evidence_columns(
    store: Any, evidence: list[dict[str, Any]]
) -> tuple[str, ...]:
    """要从源行抄过去的 evidence 列：**目标库表结构**的全部列 ∩ 源行带的列 − 身份列。

    §D-082：这里原是一份手写清单，§CMT-1 给 evidence 加了 `kind` / `parent_permalink`
    没跟上，复制出来的评论全成了 dao 默认的 `kind='post'`、父链为空。改成读目标库
    `PRAGMA table_info(evidence)`，加列就自动跟上。
    - 交源行的列：源库 schema 比目标旧时缺的列不抄，落目标库默认值；
    - `table_info` 不列生成列（`score_total` / `grade`），它们本来也不能写；
    - 表里有但 dao 不认的列，`add_evidence_batch` 会当场抛 TypeError——要响，不要静默丢。
    """

    connection = sqlite3.connect(_database_of(store))
    try:
        table = [str(row[1]) for row in connection.execute("PRAGMA table_info(evidence)")]
    finally:
        connection.close()
    if not table:
        raise ReplayImportError("目标库没有 evidence 表，无法复制证据")
    present = set().union(*(row.keys() for row in evidence))
    return tuple(
        column for column in table
        if column not in _EVIDENCE_IDENTITY_COLUMNS and column in present
    )


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
    only_chapters: list[str] | None = None,
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
        columns = _evidence_columns(store, evidence)
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
                    for column in columns
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
        only_chapters=only_chapters,
    )
    # §SEL-3：账本里的产物路径是**绝对路径**，抄过来还指着源树、源 id；而计划里
    # `opening.inputs` 是相对路径，跑时按**新树 / 新 id** 拼绝对路径。不改写这一列，
    # 两边永远对不上：`_declared_done_goal_closure` 追不到任何上游产物，每一节的
    # 可见 goal 只剩它自己，跨章互证节因此一条跨 goal 证据都引不到。
    # 真机实证（`r-3b3482ca7f8b` → 导入）：goal-6/ch-4 的 18 条 inputs 命中 0/18。
    _rebase_ledger_paths(
        Path(_database_of(store)),
        research_id,
        target_dir.parent,
        source_research_ids=_source_lineage(source_database, source_research_id),
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
    # §D-042：半截分片 `sec-N.part.K.md` 也是上一轮的产物，留着比留 `sec-N.md`
    # 更坏——`_run_section_shards` 见到可解析的片信封就 `write_shard_skipped`
    # 直接复用，而片里的角标是**上一轮证据池**的编号（S31…S85）。本轮池只有
    # 30 条时，合并出的节正文必撞证据池唯一引用源契约 → conclusion_invalid
    # （闭集 reason，章内不再重派）。凡「原跑留有半截分片」的 missing 节，
    # 重放和 §OBS-2「重跑这节」都因此补不回来。
    parent = (target_dir / relative).parent
    for stale in parent.glob(f"{relative.stem}.part.*{relative.suffix}"):
        stale.unlink(missing_ok=True)
    # §OBS-2 货 1：章级复位连它的 transcript 一起丢，重放新写一份，
    # 免得沙盒里带着底料那一轮的原始流冒充本轮读数。节级复位不动
    # （同章其它节的原始流还在这一份里）。
    parts = relative.parts
    if len(parts) == 3 and parts[0] == "goals":
        transcript = relative.parent / f"{relative.stem}{TRANSCRIPT_SUFFIX}"
        (target_dir / transcript).unlink(missing_ok=True)


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
    only_chapters: list[str] | None = None,
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
    wanted = _replay_chapters(only_chapters)
    reset: list[str] = []
    connection = sqlite3.connect(_database_of(store))
    try:
        for row in rows:
            key = (row["goal_id"], row["chapter_id"])
            replayed = _is_replayed(row, replay_goals, wanted, reset_done=reset_done)
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
