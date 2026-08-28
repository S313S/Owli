"""脚本级单节重放：把某研究的某一节拿旧数据重跑一遍。

**走的是整跑那一条路径**：复位目标节 → `RuntimeCoordinator._run_task`，
也就是 Scheduler 在整跑里调的那个方法。这里不另写一份撰写逻辑——
§W-1 的教训是尺子重实现生产解析，量出来的数是假的。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.api.events import ResearchEventBuffer
from app.orchestrator.runtime import RuntimeCoordinator
from app.orchestrator.scheduler import TaskContext
from app.orchestrator.sectioning import _section_specs
from app.plan.model import Plan
from app.replay.sandbox import Fingerprint, ReplaySandbox
from app.store.dao import Store


class ReplayTargetError(LookupError):
    """底料里找不到要重放的 goal / 章 / 节。"""


@dataclass(frozen=True)
class SectionReplayResult:
    research_id: str
    goal_id: str
    chapter_id: str
    requested_sections: tuple[str, ...]
    ledger_before: tuple[dict[str, Any], ...]
    ledger_after: tuple[dict[str, Any], ...]
    artifacts: tuple[str, ...]
    task_result: Any
    fingerprint_before: Fingerprint
    fingerprint_after: Fingerprint

    @property
    def source_untouched(self) -> bool:
        return self.fingerprint_before == self.fingerprint_after


def _resolve_agent(plan: Plan, goal_id: str, chapter_id: str) -> tuple[Any, Any]:
    for goal in plan.goals:
        if goal.goal_id != goal_id:
            continue
        for agent in goal.agents:
            chapter = agent.chapter if isinstance(agent.chapter, dict) else {}
            if str(chapter.get("chapter_id") or agent.agent_id) == chapter_id:
                return goal, agent
        raise ReplayTargetError(f"{goal_id} 下没有章 {chapter_id}")
    raise ReplayTargetError(f"计划里没有 {goal_id}")


def _reset_to_pending(
    database: Path, research_id: str, goal_id: str, section_ids: list[str]
) -> None:
    """把目标节复位成 pending，好让节循环重新派它。

    直接写 sqlite 而不是走 `Store`：`dao.py` 是禁区，且账本上现成的两个复位
    方法各自只认一个 reason（`retry_exhausted` / `done`），重放要复位的是
    任意终态的节。这是**重放准备**，不是生产账本语义，故不进 dao。
    """

    connection = sqlite3.connect(database)
    try:
        connection.executemany(
            """
            UPDATE chapter_progress
            SET status = 'pending', reason = NULL,
                actual_output_path = NULL, actual_count = NULL,
                engine_error = NULL, conclusion_error = NULL,
                updated_at = ?
            WHERE research_id = ? AND goal_id = ? AND chapter_id = ?
            """,
            [
                (datetime.now(timezone.utc).isoformat(), research_id, goal_id, sid)
                for sid in section_ids
            ],
        )
        connection.commit()
    finally:
        connection.close()


def _ledger(store: Store, research_id: str, goal_id: str) -> tuple[dict[str, Any], ...]:
    return tuple(
        dict(row)
        for row in store.list_chapters(research_id)
        if row["goal_id"] == goal_id
    )


async def replay_sections(
    *,
    sandbox: ReplaySandbox,
    research_id: str,
    goal_id: str,
    chapter_id: str,
    sections: list[str] | None = None,
    scale_config: Any = None,
) -> SectionReplayResult:
    """在沙盒里重跑指定章的指定节；未指定则重跑该章全部节。"""

    store = Store(sandbox.database)
    report = store.get_report(research_id)
    if report is None or not report.get("plan_snapshot"):
        raise ReplayTargetError(f"沙盒库里没有 {research_id} 或它没有 plan_snapshot")
    plan = Plan.from_dict(report["plan_snapshot"])
    goal, agent = _resolve_agent(plan, goal_id, chapter_id)

    specs = _section_specs(plan, agent)
    by_suffix = {spec["section_id"].rsplit("/", 1)[-1]: spec for spec in specs}
    if sections:
        missing = [name for name in sections if name not in by_suffix]
        if missing:
            raise ReplayTargetError(
                f"{chapter_id} 没有这些节：{missing}；可选 {sorted(by_suffix)}"
            )
        targets = [by_suffix[name] for name in sections]
    else:
        targets = list(specs)

    ledger_before = _ledger(store, research_id, goal_id)
    section_root = (
        sandbox.runs_root / research_id / Path(str(agent.output["path"])).parent
        / Path(str(agent.output["path"])).stem
    )
    for spec in targets:
        # 上一轮写下的占位正文不能留着冒充产物（整跑复位节时也是这么做的）。
        (section_root / spec["filename"]).unlink(missing_ok=True)
    _reset_to_pending(
        sandbox.database, research_id, goal_id,
        [spec["section_id"] for spec in targets],
    )

    events = ResearchEventBuffer(max_events=4000, max_age_seconds=24 * 3600)
    events.bind_to_running_loop()
    events.bind_store(store)
    runtime = RuntimeCoordinator(
        store=store,
        event_buffer=events,
        researches={},
        cards={},
        runs_root=sandbox.runs_root,
        auto_confirm=False,
        routing_utc_clock=lambda: datetime.now(timezone.utc),
        scale_config=scale_config,
    )
    runtime.researches[research_id] = runtime._state_from_plan(plan)
    runtime._adapters[research_id] = runtime.adapter_factory()

    section_seconds = goal.retry_policy.get("chapter_deadline_seconds")
    section_seconds = None if section_seconds is None else float(section_seconds)
    context = TaskContext(
        research_id=research_id,
        goal_id=goal_id,
        attempt=1,
        round_number=1,
        engine=agent.engine,
        on_event=_ignore_signal,
        deadline_at=(
            datetime.now(timezone.utc)
            + timedelta(seconds=section_seconds * len(specs))
            if section_seconds is not None
            else None
        ),
        section_deadline_seconds=section_seconds,
    )
    task_result = await runtime._run_task(plan, agent, context)
    return SectionReplayResult(
        research_id=research_id,
        goal_id=goal_id,
        chapter_id=chapter_id,
        requested_sections=tuple(spec["section_id"] for spec in targets),
        ledger_before=ledger_before,
        ledger_after=_ledger(store, research_id, goal_id),
        artifacts=tuple(
            str(section_root / spec["filename"])
            for spec in targets
            if (section_root / spec["filename"]).is_file()
        ),
        task_result=task_result,
        fingerprint_before=sandbox.source_fingerprint,
        fingerprint_after=sandbox.verify_source_untouched(),
    )


async def _ignore_signal(_: Any) -> None:
    """整跑里这个回调是 Scheduler 的信号入口；重放没有调度器，丢弃即可。"""
