"""M2-c 计划树执行器：只推进运行时状态，不改写计划书。"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Awaitable, Callable, Mapping

from app.adapters.ratelimit import RouteState
from app.orchestrator.background import guard_task
from app.orchestrator.chapter_failure import (
    CHAPTER_FAILURE_REASONS,
    chapter_failure_reason,
)
from app.plan.cards import (
    Card,
    CardActionType,
    CardBlocking,
    CardStatus,
    CardType,
)
from app.plan.model import Agent, Goal, Plan, agent_kind_of

logger = logging.getLogger(__name__)

R8_CONFIRM_SECONDS = 15 * 60
repeat_cause_limit = 3
CHAPTER_RETRY_INTERVAL_SECONDS = {"fast": 5.0, "standard": 15.0}
_MISSING_REASONS = CHAPTER_FAILURE_REASONS
_ISO_TIMESTAMP_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})"
)

_LONG_TOKEN_PATTERN = re.compile(r"(?<![0-9A-Za-z])(?:0x)?[0-9A-Fa-f]{8,}(?![0-9A-Za-z])")


@dataclass(frozen=True)
class TaskRunResult:
    """单 agent 的结构化判定；成败与进程退出码无关。"""

    succeeded: bool
    engine: str | None = None
    route_decisions: tuple[Any, ...] = field(default_factory=tuple)
    failure_feedback: str | None = None
    chapter_status: str | None = None
    reason: str | None = None
    actual_output_path: str | None = None
    actual_count: int | None = None
    engine_error: str | None = None
    conclusion_error: str | None = None


@dataclass(frozen=True)
class TaskContext:
    research_id: str
    goal_id: str
    attempt: int
    round_number: int
    engine: str
    on_event: Callable[[Any], Awaitable[None]]
    failure_feedback: str | None = None
    #: 本章墙钟的绝对到点时刻（None = 不设墙钟）。
    deadline_at: datetime | None = None
    #: 节化章的单节墙钟；非节化章为 None。
    section_deadline_seconds: float | None = None
    #: 在取消清理阶段读取 scheduler 已登记的原因；节化执行据此区分 timeout 与 /stop。
    cancellation_reason: Callable[[], str | None] | None = None


RunTask = Callable[[Agent, TaskContext], Awaitable[TaskRunResult | Any]]
Emit = Callable[[Any], Any]
Clock = Callable[[], datetime]
Timer = Callable[[float, Callable[[], Any]], Any]
BeforeGoalComplete = Callable[[Goal], Any]


def _assert_acyclic(nodes: Mapping[str, list[str]], label: str) -> None:
    remaining = {node: set(dependencies) for node, dependencies in nodes.items()}
    while remaining:
        ready = {node for node, dependencies in remaining.items() if not dependencies}
        if not ready:
            raise ValueError(f"{label} 依赖图存在环：{sorted(remaining)}")
        for node in ready:
            remaining.pop(node)
        for dependencies in remaining.values():
            dependencies.difference_update(ready)


def _field(value: Any, *names: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
        return default
    for name in names:
        if hasattr(value, name):
            return getattr(value, name)
    return default


def _failure_feedback(result: Any) -> str | None:
    """提取契约层失败原因供下一轮重试回灌；引擎/传输层失败不回灌。

    真实样本 r-6b4baebabade goal-3/tagging：owli-result.summary 三轮均超
    200 字被拒，重试提示词却不带原因，agent 每轮盲改、重试耗尽。规划期
    已有 errors 回灌机制，执行期此前没有。
    """
    if _field(result, "engine_error"):
        return None
    parts: list[str] = []
    conclusion_error = _field(result, "conclusion_error")
    if conclusion_error:
        parts.append(f"结构化结论被拒：{conclusion_error}")
    conclusion = _field(result, "conclusion")
    status = getattr(conclusion, "status", None)
    if conclusion is not None and status and status != "done":
        parts.append(f"上一轮结构化结论自报 status={status}")
    for field_name in ("unmet", "capability_denials"):
        values = getattr(conclusion, field_name, None) if conclusion is not None else None
        if values:
            parts.append(
                f"上一轮 {field_name}："
                + json.dumps(list(values), ensure_ascii=False)
            )
    validation = _field(result, "validation", "report")
    for item in getattr(validation, "results", ()) or ():
        verdict = str(getattr(item, "verdict", "")).lower()
        if verdict and verdict != "pass":
            parts.append(
                f"产物校验未过（{getattr(item, 'name', '')}）："
                f"{getattr(item, 'message', '')}"
            )
    return "\n".join(parts) or None


def _truncate_error(value: str | None, limit: int = 2000) -> str | None:
    """错误原文按 Unicode 字符安全截断，避免超长载荷进入章账本。"""

    if value is None:
        return None
    return str(value)[:limit]


def _normalize_failure_text(value: str | None) -> str:
    text = str(value or "")[:200]
    text = _ISO_TIMESTAMP_PATTERN.sub("<timestamp>", text)
    return _LONG_TOKEN_PATTERN.sub("<id>", text)


def _failure_signature(result: TaskRunResult) -> tuple[str, str, str, str]:
    return (
        str(result.chapter_status or ""),
        str(result.reason or ""),
        _normalize_failure_text(result.conclusion_error),
        _normalize_failure_text(result.engine_error),
    )


class Scheduler:
    """按 goal/agent 两级 DAG 推进，并执行重试、限流和发卡策略。"""

    def __init__(
        self,
        plan: Plan,
        run_task: RunTask,
        emit: Emit,
        clock: Clock,
        timer: Timer,
        chapter_ledger: Any | None = None,
        before_goal_complete: BeforeGoalComplete | None = None,
        batch_count: Callable[[Agent], Any] | None = None,
    ) -> None:
        self.plan = plan
        self._run_task = run_task
        #: §RATE-3：评级章分几片跑（0 = 不分）；片数由运行期库行数决定，
        #: 由 runtime 回答。只用来按片给墙钟，口径同节化章的 section_deadline_seconds。
        self._batch_count = batch_count
        self._emit_callback = emit
        self._clock = clock
        self._timer = timer
        self._chapter_ledger = chapter_ledger
        self._before_goal_complete = before_goal_complete
        self._goals = {goal.goal_id: goal for goal in plan.goals}
        self._agents = {
            agent.agent_id: (goal, agent)
            for goal in plan.goals
            for agent in goal.agents
        }
        self._validate_graphs()
        self.goal_statuses = {goal.goal_id: "pending" for goal in plan.goals}
        self.agent_statuses = {
            agent.agent_id: "queued" for goal in plan.goals for agent in goal.agents
        }
        self.status = "ready"
        # §AUTO-EXP 货 5：无原因取消（D-023 只留了事件）现在还要把研究判 failed；
        # 收尾在 _finalize_if_terminal 读这个标记改 report_status。
        self.cancelled_without_reason: bool = False
        self.emitted_events: list[Any] = []
        self._paused = False
        self._started = False
        self._tasks: dict[asyncio.Task[Any], tuple[str, str]] = {}
        self._drive_tasks: set[asyncio.Task[Any]] = set()
        self._state_lock = asyncio.Lock()
        self._goal_started_at: dict[str, datetime] = {}
        self._agent_started_at: dict[str, datetime] = {}
        self._attempts: dict[str, int] = {}
        self._cards: dict[str, dict[str, Any]] = {}
        self._card_sequence = 0
        self._agent_feedback: dict[str, str | None] = {}
        self._supplemented: set[str] = set()
        self._last_attempt_started_at: dict[tuple[str, str], datetime] = {}
        self._last_failure_signature: dict[tuple[str, str], tuple[str, str, str, str]] = {}
        self._repeat_cause_counts: dict[tuple[str, str], int] = {}
        #: 在跑的 adapter 任务（章 agent_id → task）；墙钟取消与 /stop 共用它。
        self._running_runs: dict[str, asyncio.Task[Any]] = {}
        self._cancel_reasons: dict[str, str] = {}
        self._deadline_expired: set[str] = set()
        self._deadline_armed: set[str] = set()
        self._deadline_rearming: set[str] = set()
        if self._chapter_ledger is not None:
            self._chapter_ledger.ensure_chapters(
                plan.research_id,
                [
                    {"goal_id": goal.goal_id, "chapter_id": self._chapter_id(agent)}
                    for goal in plan.goals
                    for agent in goal.agents
                ],
                updated_at=self._clock().isoformat(),
            )
            rows = self._chapter_ledger.list_chapters(plan.research_id)
            by_key = {
                (row["goal_id"], row["chapter_id"]): row for row in rows
            }
            for goal in plan.goals:
                for agent in goal.agents:
                    row = by_key[(goal.goal_id, self._chapter_id(agent))]
                    if row["status"] in {"done", "missing"}:
                        self.agent_statuses[agent.agent_id] = row["status"]
                    elif row["status"] == "deferred":
                        self._supplemented.add(agent.agent_id)

    @staticmethod
    def _chapter_id(agent: Agent) -> str:
        chapter = agent.chapter if isinstance(agent.chapter, Mapping) else {}
        return str(chapter.get("chapter_id") or agent.agent_id)

    def _finish_ledger(
        self,
        goal: Goal,
        agent: Agent,
        *,
        status: str,
        reason: str | None,
        output_path: str | None,
        actual_count: int | None,
        engine_error: str | None,
        conclusion_error: str | None,
    ) -> None:
        if self._chapter_ledger is None:
            return
        self._chapter_ledger.finish_chapter(
            self.plan.research_id,
            goal.goal_id,
            self._chapter_id(agent),
            status=status,
            reason=reason,
            actual_output_path=output_path,
            actual_count=actual_count,
            engine_error=_truncate_error(engine_error),
            conclusion_error=_truncate_error(conclusion_error),
            updated_at=self._clock().isoformat(),
        )

    def _reset_ledger(self, goal: Goal, agent: Agent) -> bool:
        """把被打断的在跑章复位成 pending；没有账本或本来就不是 running 时返回 False。"""
        if self._chapter_ledger is None:
            return False
        reset = getattr(self._chapter_ledger, "reset_running_chapter", None)
        if reset is None:
            return False
        return bool(reset(
            self.plan.research_id,
            goal.goal_id,
            self._chapter_id(agent),
            updated_at=self._clock().isoformat(),
        ))

    async def _emit_chapter_update(self, goal: Goal, agent: Agent) -> None:
        if self._chapter_ledger is None:
            return
        row = next(
            item for item in self._chapter_ledger.list_chapters(self.plan.research_id)
            if item["goal_id"] == goal.goal_id
            and item["chapter_id"] == self._chapter_id(agent)
        )
        await self._emit({"type": "chapter_update", "data": row})

    def _validate_graphs(self) -> None:
        seen_agent_ids: set[str] = set()
        duplicate_agent_ids: set[str] = set()
        for goal in self.plan.goals:
            for agent in goal.agents:
                if agent.agent_id in seen_agent_ids:
                    duplicate_agent_ids.add(agent.agent_id)
                seen_agent_ids.add(agent.agent_id)
        if duplicate_agent_ids:
            raise ValueError(
                f"agent_id 跨 goal 重复：{sorted(duplicate_agent_ids)}"
            )
        goal_graph = {
            goal.goal_id: list(goal.depends_on) for goal in self.plan.goals
        }
        _assert_acyclic(goal_graph, "goal")
        for goal in self.plan.goals:
            agent_graph = {
                agent.agent_id: list(agent.depends_on) for agent in goal.agents
            }
            _assert_acyclic(agent_graph, f"{goal.goal_id} agent")

    async def _wait_for_chapter_retry(self, goal: Goal, agent: Agent) -> None:
        if self._chapter_ledger is None:
            return
        key = (goal.goal_id, self._chapter_id(agent))
        started_at = self._last_attempt_started_at.get(key)
        if started_at is None:
            return
        interval = CHAPTER_RETRY_INTERVAL_SECONDS[self.plan.scale]
        elapsed = (self._clock() - started_at).total_seconds()
        remaining = interval - elapsed
        if remaining <= 0:
            return
        ready = asyncio.get_running_loop().create_future()

        def release() -> None:
            if not ready.done():
                ready.set_result(None)

        self._timer(remaining, release)
        await ready

    def update_plan(self, plan: Plan) -> None:
        """在干预点替换后续计划；保留已运行节点的运行时状态。"""
        if plan.research_id != self.plan.research_id:
            raise ValueError("不能用其他 research 的计划更新 Scheduler")
        active = {
            goal_id for goal_id, status in self.goal_statuses.items()
            if status == "running"
        }
        changed_active = {
            goal.goal_id
            for goal in plan.goals
            if goal.goal_id in active and goal.to_dict() != self._goals[goal.goal_id].to_dict()
        }
        if changed_active:
            raise ValueError(f"运行中的 goal 不允许修改：{sorted(changed_active)}")
        old_agent_statuses = dict(self.agent_statuses)
        old_goal_statuses = dict(self.goal_statuses)
        self.plan = plan
        self._goals = {goal.goal_id: goal for goal in plan.goals}
        self._agents = {
            agent.agent_id: (goal, agent)
            for goal in plan.goals
            for agent in goal.agents
        }
        self._validate_graphs()
        self.goal_statuses = {
            goal.goal_id: old_goal_statuses.get(goal.goal_id, "pending")
            for goal in plan.goals
        }
        self.agent_statuses = {
            agent.agent_id: old_agent_statuses.get(agent.agent_id, "queued")
            for goal in plan.goals
            for agent in goal.agents
        }

    async def _emit(self, event: Any) -> None:
        self.emitted_events.append(event)
        result = self._emit_callback(event)
        if inspect.isawaitable(result):
            await result

    async def _set_goal_status(self, goal_id: str, status: str) -> None:
        if self.goal_statuses[goal_id] == status:
            return
        self.goal_statuses[goal_id] = status
        await self._emit({
            "type": "goal_update",
            "data": {"goal_id": goal_id, "status": status},
        })
        await self._emit({"type": "progress", "data": self.progress()})

    async def _set_agent_status(self, agent_id: str, status: str) -> None:
        if self.agent_statuses[agent_id] == status:
            return
        self.agent_statuses[agent_id] = status
        goal = self._agents[agent_id][0]
        await self._emit({
            "type": "agent_update",
            "data": {
                "goal_id": goal.goal_id,
                "agent_id": agent_id,
                "status": status,
            },
        })
        await self._emit({"type": "progress", "data": self.progress()})

    def progress(self) -> dict[str, Any]:
        total = sum(status != "skipped" for status in self.goal_statuses.values())
        done = sum(status == "done" for status in self.goal_statuses.values())
        return {
            "done": done,
            "total": total,
            "agents": dict(self.agent_statuses),
        }

    async def start(self) -> None:
        if self.status == "stopped":
            return
        self._started = True
        if not self._paused:
            self.status = "running"
        await self._drive()

    async def pause(self) -> None:
        if self.status == "stopped":
            return
        self._paused = True
        self.status = "paused"
        await self._emit({"type": "scheduler_update", "data": {"status": "paused"}})

    async def resume(self, *, wait: bool = True) -> None:
        """恢复执行；`stopped` 也能恢复（停下的章已复位，按未完成部分继续跑）。

        wait=False 时只翻状态并把驱动循环放到后台任务里，调用方立刻拿到真实状态，
        不必阻塞到整轮跑完（HTTP `/resume` 走这条）。驱动循环仍是同一个 `_drive`。
        """
        self._paused = False
        self._started = True
        if self.status != "completed":
            self.status = "running"
        await self._emit(
            {"type": "scheduler_update", "data": {"status": self.status}}
        )
        task = self._spawn_drive()
        if wait:
            await task

    def _spawn_drive(self) -> asyncio.Task[Any]:
        task = asyncio.create_task(self._drive(), name="owli:drive")
        self._drive_tasks.add(task)
        task.add_done_callback(self._drive_tasks.discard)
        # D-013 货 2：`_drive` 死在后台就是整卷停摆，异常必须留痕
        return guard_task(task, logger=logger, context="调度驱动")

    @property
    def drive_pending(self) -> bool:
        return any(not task.done() for task in self._drive_tasks)

    async def wait_idle(self) -> None:
        """等后台驱动循环全部落定，供上层做终态收尾。"""
        while True:
            pending = [task for task in self._drive_tasks if not task.done()]
            if not pending:
                return
            await asyncio.gather(*pending, return_exceptions=True)

    async def stop(self) -> None:
        if self.status == "stopped":
            return
        self.status = "stopped"
        self._paused = True
        # /stop 与墙钟超时共用一条取消路径：不留在跑的 adapter 任务（D-008）。
        for agent_id in list(self._running_runs):
            self._cancel_running_run(agent_id, "stopped")
        await self._emit({"type": "scheduler_update", "data": {"status": "stopped"}})

    async def _drive(self) -> None:
        while self._started and self.status != "stopped":
            async with self._state_lock:
                await self._propagate_upstream_failures()
                await self._settle_goals()
                if not self._paused:
                    await self._launch_ready_agents()
                running = set(self._tasks)
            if not running:
                if self._chapter_ledger is not None:
                    deferred = [
                        agent_id
                        for agent_id, status in self.agent_statuses.items()
                        if status == "deferred" and agent_id not in self._supplemented
                    ]
                    if deferred:
                        for agent_id in deferred:
                            self._supplemented.add(agent_id)
                            await self._set_agent_status(agent_id, "queued")
                        continue
                self._set_completed_if_terminal()
                return
            done, _ = await asyncio.wait(
                running, return_when=asyncio.FIRST_COMPLETED
            )
            async with self._state_lock:
                for task in done:
                    self._tasks.pop(task, None)
                    try:
                        task.result()
                    except asyncio.CancelledError:
                        pass

    async def _propagate_upstream_failures(self) -> None:
        changed = True
        while changed:
            changed = False
            for goal in self.plan.goals:
                if self.goal_statuses[goal.goal_id] != "pending":
                    continue
                upstream = [self.goal_statuses[item] for item in goal.depends_on]
                has_failure = any(item in {"failed", "skipped"} for item in upstream)
                if has_failure and goal.on_upstream_failure != "run_anyway":
                    await self._set_goal_status(goal.goal_id, "skipped")
                    for agent in goal.agents:
                        await self._set_agent_status(agent.agent_id, "skipped")
                    changed = True

    def _goal_ready(self, goal: Goal) -> bool:
        if self.goal_statuses[goal.goal_id] != "pending":
            return False
        upstream = [self.goal_statuses[item] for item in goal.depends_on]
        if goal.on_upstream_failure == "run_anyway":
            return all(item in {"done", "failed", "skipped"} for item in upstream)
        return all(item == "done" for item in upstream)

    def _agent_ready(self, goal: Goal, agent: Agent) -> bool:
        if self.agent_statuses[agent.agent_id] not in {"queued", "retrying"}:
            return False
        if any(
            active_agent == agent.agent_id
            for _, active_agent in self._tasks.values()
        ):
            return False
        if not all(
            self.agent_statuses[item] in {"done", "missing"}
            for item in agent.depends_on
        ):
            return False
        return True

    async def _launch_ready_agents(self) -> None:
        for goal in self.plan.goals:
            status = self.goal_statuses[goal.goal_id]
            if status == "pending" and not self._goal_ready(goal):
                continue
            if status not in {"pending", "running"}:
                continue
            ready = [agent for agent in goal.agents if self._agent_ready(goal, agent)]
            if not ready:
                continue
            if status == "pending":
                await self._set_goal_status(goal.goal_id, "running")
                self._schedule_goal_deadline(goal)
            for agent in ready:
                await self._set_agent_status(agent.agent_id, "running")
                task = asyncio.create_task(self._execute_agent(goal, agent))
                self._tasks[task] = (goal.goal_id, agent.agent_id)

    def _schedule_goal_deadline(self, goal: Goal) -> None:
        started_at = self._clock()
        self._goal_started_at[goal.goal_id] = started_at
        delay = float(goal.retry_policy["goal_deadline_hours"]) * 3600
        self._timer(delay, lambda: self._expire_goal(goal.goal_id, started_at))

    async def _expire_goal(self, goal_id: str, started_at: datetime) -> None:
        if self._goal_started_at.get(goal_id) != started_at:
            return
        if self.goal_statuses[goal_id] != "running":
            return
        await self._fail_goal(goal_id, "goal_deadline")
        self._spawn_drive()

    def _set_completed_if_terminal(self) -> None:
        if self.status in {"stopped", "paused"}:
            return
        terminal = {"done", "failed", "skipped"}
        if all(status in terminal for status in self.goal_statuses.values()):
            self.status = "completed"

    async def _settle_goals(self) -> None:
        for goal in self.plan.goals:
            goal_status = self.goal_statuses[goal.goal_id]
            if goal_status not in {"pending", "running"}:
                continue
            statuses = [self.agent_statuses[agent.agent_id] for agent in goal.agents]
            if self._chapter_ledger is None and any(
                status == "failed" for status in statuses
            ):
                await self._fail_goal(goal.goal_id, "agent_exhausted")
                continue
            terminal = (
                {"done"}
                if self._chapter_ledger is None
                else {"done", "missing"}
            )
            if statuses and all(status in terminal for status in statuses):
                if goal_status == "pending" and not self._goal_ready(goal):
                    continue
                if goal_status == "pending":
                    await self._set_goal_status(goal.goal_id, "running")
                if self._before_goal_complete is not None:
                    try:
                        result = self._before_goal_complete(goal)
                        if inspect.isawaitable(result):
                            await result
                    except Exception as exc:
                        await self._fail_goal(
                            goal.goal_id,
                            f"goal_persistence_failed:{type(exc).__name__}",
                        )
                        continue
                await self._set_goal_status(goal.goal_id, "awaiting_intervention")
                await self._create_intervention_card(goal)

    async def _fail_goal(self, goal_id: str, reason: str) -> None:
        if self.goal_statuses[goal_id] in {"done", "failed", "skipped"}:
            return
        await self._set_goal_status(goal_id, "failed")
        goal = self._goals[goal_id]
        for agent in goal.agents:
            if self.agent_statuses[agent.agent_id] in {
                "queued", "retrying", "running"
            }:
                await self._set_agent_status(agent.agent_id, "skipped")
        await self._emit({
            "type": "goal_gate",
            "data": {"goal_id": goal_id, "reason": reason},
        })
        await self._propagate_upstream_failures()

    def _normalize_result(self, result: Any) -> TaskRunResult:
        if isinstance(result, TaskRunResult):
            return result
        succeeded = _field(result, "succeeded")
        if not isinstance(succeeded, bool):
            raise TypeError("run_task 必须返回带 succeeded 布尔值的结构化结果")
        decisions = _field(result, "route_decisions", default=()) or ()
        return TaskRunResult(
            succeeded=succeeded,
            engine=_field(result, "engine"),
            route_decisions=tuple(decisions),
            failure_feedback=None if succeeded else _failure_feedback(result),
            chapter_status=_field(result, "chapter_status"),
            reason=_field(result, "reason"),
            actual_output_path=_field(result, "actual_output_path"),
            actual_count=_field(result, "actual_count"),
            engine_error=_field(result, "engine_error"),
            conclusion_error=_field(result, "conclusion_error"),
        )

    async def _invoke_run_task(self, agent: Agent, context: TaskContext) -> Any:
        """把一次派活包成独立 task，才能被墙钟 / stop 主动取消。"""

        result = self._run_task(agent, context)
        if inspect.isawaitable(result):
            result = await result
        return result

    def _cancel_running_run(self, agent_id: str, reason: str) -> None:
        """取消在跑的 adapter 任务。墙钟超时与 /stop 都只走这一条路径（D-008）。"""

        task = self._running_runs.get(agent_id)
        if task is None or task.done():
            return
        self._cancel_reasons[agent_id] = reason
        task.cancel()

    def _arm_chapter_deadline(self, agent: Agent, deadline_seconds: float) -> None:
        """章第一次派活时挂上墙钟定时器：到点主动取消在跑任务，不等它自己返回。"""

        if agent.agent_id in self._deadline_armed:
            return
        self._deadline_armed.add(agent.agent_id)

        def expire() -> None:
            # 一次都还没派活就到点的，只可能是假时钟把定时器提前触发了：
            # 此时没有可取消的对象；按绝对 deadline 的剩余时间重挂，不能永久放弃。
            if self._attempts.get(agent.agent_id, 0) < 1:
                # 同步假 timer 会在 `_timer()` 栈内立刻再调 expire；只抑制这种
                # 重入，真实异步 timer 返回后标记即清，下一次到点仍正常生效。
                if agent.agent_id in self._deadline_rearming:
                    return
                started_at = self._agent_started_at.get(agent.agent_id)
                elapsed = (
                    (self._clock() - started_at).total_seconds()
                    if started_at is not None
                    else 0.0
                )
                remaining = max(0.0, float(deadline_seconds) - elapsed)
                self._deadline_rearming.add(agent.agent_id)
                try:
                    self._timer(remaining, expire)
                finally:
                    self._deadline_rearming.discard(agent.agent_id)
                return
            self._deadline_expired.add(agent.agent_id)
            self._cancel_running_run(agent.agent_id, "timeout")

        self._timer(float(deadline_seconds), expire)

    async def _finish_on_deadline(
        self,
        goal: Goal,
        agent: Agent,
        result: TaskRunResult | None,
        *,
        exhausted: bool = False,
    ) -> None:
        """墙钟到点定终态：reason 恒为 timeout（部分节已成功的章同样介入）。

        还有轮次预算时先 deferred 留一次补轮；轮次已用尽就直接 missing，
        不留一个永远排不上队的 deferred 幽灵。
        """

        status = (
            "missing"
            if exhausted or agent.agent_id in self._supplemented
            else "deferred"
        )
        self._finish_ledger(
            goal,
            agent,
            status=status,
            reason="timeout",
            output_path=None if result is None else result.actual_output_path,
            actual_count=None if result is None else result.actual_count,
            engine_error=None if result is None else result.engine_error,
            conclusion_error=None if result is None else result.conclusion_error,
        )
        await self._emit_chapter_update(goal, agent)
        await self._set_agent_status(agent.agent_id, status)

    async def _abort_agent_on_stop(
        self, goal: Goal, agent: Agent, result: TaskRunResult | None
    ) -> None:
        """/stop 打断在跑章：已拿到成功结果就落终态，否则复位账本并重新排队。

        两条路都不留 running 幽灵；复位成 pending 后 `resume` 由 `_drive` 正常派活。
        """
        if result is not None and result.succeeded:
            self._finish_ledger(
                goal,
                agent,
                status="done",
                reason=None,
                output_path=result.actual_output_path or str(agent.output["path"]),
                actual_count=result.actual_count,
                engine_error=result.engine_error,
                conclusion_error=result.conclusion_error,
            )
            await self._emit_chapter_update(goal, agent)
            await self._set_agent_status(agent.agent_id, "done")
            return
        self._reset_ledger(goal, agent)
        await self._emit_chapter_update(goal, agent)
        await self._set_agent_status(agent.agent_id, "queued")

    async def _execute_agent(self, goal: Goal, agent: Agent) -> None:
        policy = goal.retry_policy
        per_round = int(policy["max_attempts_per_round"])
        total = per_round * int(policy["max_rounds"])
        ask_at = int(policy["ask_engine_switch_at"])
        deadline_seconds = policy.get("chapter_deadline_seconds")
        section_deadline_seconds = None
        if deadline_seconds is not None:
            deadline_seconds = int(deadline_seconds)
            # sectioning 模块级依赖 scheduler；此处只能函数内导入，避免互相引用成环。
            from app.orchestrator.sectioning import _section_specs, should_section

            kind = agent_kind_of(
                agent.agent_id, agent.capability.get("profile"),
            )
            output_format = str(agent.output.get("format", ""))
            if should_section(kind, output_format):
                section_deadline_seconds = deadline_seconds
                deadline_seconds *= len(_section_specs(self.plan, agent))
            elif kind == "reliability_audit" and self._batch_count is not None:
                # §RATE-3：评级章一章内分片，每片一份自己的墙钟、章预算 = 片数 ×
                # 章墙钟——与节化章「墙钟按节计」同一口径，不新造概念。
                batches = self._batch_count(agent)
                if inspect.isawaitable(batches):
                    batches = await batches
                if int(batches or 0) > 0:
                    section_deadline_seconds = deadline_seconds
                    deadline_seconds *= int(batches)
        self._agent_started_at.setdefault(agent.agent_id, self._clock())
        if self._chapter_ledger is not None and deadline_seconds is not None:
            self._arm_chapter_deadline(agent, deadline_seconds)
        while self._attempts.get(agent.agent_id, 0) < total:
            if self.status == "stopped":
                await self._abort_agent_on_stop(goal, agent, None)
                return
            if agent.agent_id in self._deadline_expired:
                await self._finish_on_deadline(goal, agent, None)
                return
            if self.goal_statuses[goal.goal_id] != "running":
                return
            attempt = self._attempts.get(agent.agent_id, 0) + 1
            if self._paused:
                await self._set_agent_status(agent.agent_id, "retrying")
                return
            if attempt > 1:
                await self._wait_for_chapter_retry(goal, agent)
                if self.status == "stopped":
                    await self._abort_agent_on_stop(goal, agent, None)
                    return
                if self.goal_statuses[goal.goal_id] != "running":
                    return
                if self._paused:
                    await self._set_agent_status(agent.agent_id, "retrying")
                    return
            self._attempts[agent.agent_id] = attempt
            if self._chapter_ledger is not None:
                attempt_started_at = self._clock()
                chapter_key = (goal.goal_id, self._chapter_id(agent))
                self._last_attempt_started_at[chapter_key] = attempt_started_at
                started = self._chapter_ledger.start_chapter(
                    self.plan.research_id,
                    goal.goal_id,
                    self._chapter_id(agent),
                    engine=agent.engine,
                    updated_at=attempt_started_at.isoformat(),
                )
                if not started:
                    return
            await self._set_agent_status(
                agent.agent_id, "running" if attempt == 1 else "retrying"
            )
            deadline_at = (
                self._agent_started_at[agent.agent_id]
                + timedelta(seconds=deadline_seconds)
                if deadline_seconds is not None
                else None
            )
            context = TaskContext(
                research_id=self.plan.research_id,
                goal_id=goal.goal_id,
                attempt=attempt,
                round_number=((attempt - 1) // per_round) + 1,
                engine=agent.engine,
                on_event=self._consume_signal,
                failure_feedback=self._agent_feedback.get(agent.agent_id),
                deadline_at=deadline_at,
                section_deadline_seconds=section_deadline_seconds,
                cancellation_reason=lambda agent_id=agent.agent_id: (
                    self._cancel_reasons.get(agent_id)
                ),
            )
            run_future = asyncio.ensure_future(
                self._invoke_run_task(agent, context)
            )
            self._running_runs[agent.agent_id] = run_future
            try:
                await asyncio.wait({run_future})
            except asyncio.CancelledError:
                run_future.cancel()
                return
            finally:
                self._running_runs.pop(agent.agent_id, None)
            if run_future.cancelled():
                cancel_reason = self._cancel_reasons.pop(agent.agent_id, None)
                if cancel_reason == "timeout":
                    await self._finish_on_deadline(
                        goal, agent, None,
                        exhausted=self._attempts.get(agent.agent_id, 0) >= total,
                    )
                elif cancel_reason == "stopped" or self.status == "stopped":
                    # /stop 掐掉的这次派活没跑完，不该让 resume 再干等一次章级退避。
                    self._last_attempt_started_at.pop(
                        (goal.goal_id, self._chapter_id(agent)), None,
                    )
                    await self._abort_agent_on_stop(goal, agent, None)
                else:
                    # D-023 加了事件；§AUTO-EXP 货 5（08-30 拍板）：无原因取消不再
                    # 只留痕——goal 判 failed、级联传播后走正常收尾，研究置 failed。
                    await self._emit({
                        "type": "agent_run_cancelled",
                        "data": {
                            "goal_id": goal.goal_id,
                            "agent_id": agent.agent_id,
                            "chapter_id": self._chapter_id(agent),
                            "cancel_reason": cancel_reason,
                            "scheduler_status": self.status,
                            "note": "run 被取消但没有取消原因；goal 判 failed，不自动重试",
                        },
                        "is_error": True,
                    })
                    self.cancelled_without_reason = True
                    await self._fail_goal(goal.goal_id, "agent_run_cancelled")
                    self._set_completed_if_terminal()
                    self._spawn_drive()
                return
            self._cancel_reasons.pop(agent.agent_id, None)
            try:
                result = self._normalize_result(run_future.result())
            except asyncio.CancelledError:
                return
            except Exception as exc:
                result = TaskRunResult(
                    False,
                    agent.engine,
                    engine_error=f"{type(exc).__name__}: {exc}",
                )
                await self._emit({
                    "type": "agent_error",
                    "data": {
                        "goal_id": goal.goal_id,
                        "agent_id": agent.agent_id,
                        "message": str(exc),
                    },
                })
            for decision in result.route_decisions:
                await self._consume_signal(decision)
            if self.status == "stopped":
                await self._abort_agent_on_stop(goal, agent, result)
                return
            if self.goal_statuses[goal.goal_id] != "running":
                return
            if self._chapter_ledger is not None and not result.succeeded:
                chapter_key = (goal.goal_id, self._chapter_id(agent))
                signature = _failure_signature(result)
                if self._last_failure_signature.get(chapter_key) == signature:
                    repeat_count = self._repeat_cause_counts.get(chapter_key, 1) + 1
                else:
                    repeat_count = 1
                self._last_failure_signature[chapter_key] = signature
                self._repeat_cause_counts[chapter_key] = repeat_count
                if (
                    repeat_count >= repeat_cause_limit
                    and result.chapter_status not in {"missing", "deferred"}
                ):
                    reason = chapter_failure_reason(
                        result,
                        fallback="retry_exhausted",
                    )
                    self._finish_ledger(
                        goal,
                        agent,
                        status="missing",
                        reason=reason,
                        output_path=result.actual_output_path,
                        actual_count=result.actual_count,
                        engine_error=result.engine_error,
                        conclusion_error=result.conclusion_error,
                    )
                    await self._emit_chapter_update(goal, agent)
                    await self._set_agent_status(agent.agent_id, "missing")
                    return
            if (
                self._chapter_ledger is not None
                and deadline_seconds is not None
                and (self._clock() - self._agent_started_at[agent.agent_id]).total_seconds()
                >= deadline_seconds
                and not result.succeeded
                and result.chapter_status not in {"missing", "deferred"}
            ):
                # 墙钟到点的章一律 timeout：errors 为空时不再退回 retry_exhausted（D-008 根因 1）。
                await self._finish_on_deadline(
                    goal, agent, result,
                    exhausted=self._attempts.get(agent.agent_id, 0) >= total,
                )
                return
            if result.succeeded:
                self._finish_ledger(
                    goal,
                    agent,
                    status="done",
                    reason=None,
                    output_path=result.actual_output_path or str(agent.output["path"]),
                    actual_count=result.actual_count,
                    engine_error=result.engine_error,
                    conclusion_error=result.conclusion_error,
                )
                await self._emit_chapter_update(goal, agent)
                await self._set_agent_status(agent.agent_id, "done")
                return
            if self._chapter_ledger is not None and result.chapter_status in {
                "missing", "deferred"
            }:
                status = result.chapter_status
                if status == "deferred" and agent.agent_id in self._supplemented:
                    status = "missing"
                self._finish_ledger(
                    goal,
                    agent,
                    status=status,
                    reason=result.reason,
                    output_path=result.actual_output_path,
                    actual_count=result.actual_count,
                    engine_error=result.engine_error,
                    conclusion_error=result.conclusion_error,
                )
                await self._emit_chapter_update(goal, agent)
                await self._set_agent_status(agent.agent_id, status)
                return
            self._agent_feedback[agent.agent_id] = result.failure_feedback
            if self._chapter_ledger is None and attempt == ask_at:
                await self._create_engine_switch_card(goal, agent, per_round)
            if attempt < total:
                continue
            if self._chapter_ledger is not None:
                self._finish_ledger(
                    goal,
                    agent,
                    status="missing",
                    reason="retry_exhausted",
                    output_path=result.actual_output_path,
                    actual_count=result.actual_count,
                    engine_error=result.engine_error,
                    conclusion_error=result.conclusion_error,
                )
                await self._emit_chapter_update(goal, agent)
                await self._set_agent_status(agent.agent_id, "missing")
            else:
                await self._set_agent_status(agent.agent_id, "failed")
                await self._fail_goal(goal.goal_id, "retry_exhausted")
            return

    async def _consume_signal(self, signal: Any) -> None:
        raw_state = _field(signal, "route_state", "state")
        if isinstance(raw_state, Enum):
            raw_state = raw_state.value
        if raw_state is None:
            return
        try:
            state = RouteState(str(raw_state))
        except ValueError:
            return
        source = str(_field(signal, "engine", default="Owli") or "Owli")
        reason = str(_field(signal, "reason", "text", default=""))
        target = _field(signal, "failover_target")
        await self._emit({
            "type": "route_update",
            "data": {
                "source": source,
                "engine": source,
                "state": state.value,
                "reason": reason,
                "failover_target": target,
                "outcome": _field(signal, "outcome"),
            },
        })
        if state is RouteState.WARN and "继续跑会计费" in reason:
            await self._begin_r8_confirmation()

    def _new_card_id(self) -> str:
        self._card_sequence += 1
        return f"card-{self._card_sequence}"

    def _timestamp(self, value: datetime | None = None) -> str:
        return (value or self._clock()).isoformat()

    async def _publish_card(
        self, card: Card, *, kind: str, route_after_attempt: int | None = None
    ) -> None:
        self._cards[card.card_id] = {
            "card": card,
            "kind": kind,
            "route_after_attempt": route_after_attempt,
        }
        await self._emit(card.to_event())

    async def _create_engine_switch_card(
        self, goal: Goal, agent: Agent, route_after_attempt: int
    ) -> None:
        card = Card(
            card_id=self._new_card_id(),
            card_type=CardType.ENGINE_SWITCH_CONFIRM,
            research_id=self.plan.research_id,
            goal_id=goal.goal_id,
            agent_id=agent.agent_id,
            title="是否在下一轮切换引擎？",
            body="本轮重跑不会暂停；选择切换后，从下一轮首次重试生效。",
            target={},
            actions=[{
                "type": CardActionType.CHOICE_2.value,
                "options": ["切换引擎", "保持不变"],
            }],
            blocking=CardBlocking.NONE,
            deadline=None,
            status=CardStatus.PENDING,
            result=None,
            created_at=self._timestamp(),
            resolved_at=None,
        )
        await self._publish_card(
            card,
            kind="c3",
            route_after_attempt=route_after_attempt,
        )

    async def _create_intervention_card(self, goal: Goal) -> None:
        card = Card(
            card_id=self._new_card_id(),
            card_type=CardType.INTERVENE,
            research_id=self.plan.research_id,
            goal_id=goal.goal_id,
            agent_id=None,
            title="请确认阶段产物后继续",
            body=str(goal.intervention.get("prompt", "是否继续下一阶段？")),
            target={},
            actions=[{
                "type": CardActionType.CHOICE_2.value,
                "options": ["继续", "调整后继续"],
            }],
            blocking=CardBlocking.GOAL,
            deadline=None,
            status=CardStatus.PENDING,
            result=None,
            created_at=self._timestamp(),
            resolved_at=None,
        )
        await self._publish_card(card, kind="intervene")

    async def _begin_r8_confirmation(self) -> None:
        if any(
            item["kind"] == "r8"
            and item["card"].status is CardStatus.PENDING
            for item in self._cards.values()
        ):
            return
        deadline = self._clock() + timedelta(seconds=R8_CONFIRM_SECONDS)
        card = Card(
            card_id=self._new_card_id(),
            card_type=CardType.EXTRA_QUOTA_CONFIRM,
            research_id=self.plan.research_id,
            goal_id=None,
            agent_id=None,
            title="是否接受额外额度计费？",
            body="该引擎套餐额度已用完；15 分钟未答将默认切换引擎。",
            target={},
            actions=[{
                "type": CardActionType.CHOICE_2.value,
                "options": ["接受计费继续", "不接受，切换引擎"],
            }],
            blocking=CardBlocking.AGENT,
            deadline=self._timestamp(deadline),
            status=CardStatus.PENDING,
            result=None,
            created_at=self._timestamp(),
            resolved_at=None,
        )
        await self._publish_card(card, kind="r8")
        self._timer(
            R8_CONFIRM_SECONDS,
            lambda: self._expire_r8(card.card_id),
        )

    def _claim_card(self, entry: dict[str, Any], status: CardStatus) -> bool:
        """原子占卡（D-013 货 1）。

        「先检查 PENDING、再置状态」中间**一个 await 都不能有**——只要让出一次控制权，
        第二路回复就能挤进来通过同一个检查，然后在 `_resolve_card` 之后撞上
        `RuntimeError("卡片已处理")`。这个异常多半落在 `asyncio.create_task` 里没人接
        （§W-1 第 4/6 轮实测），goal 就此死等。asyncio 单线程，
        只要这两行之间不 await，check-and-set 就是原子的。

        返回 True 表示占卡成功、由调用方继续跑副作用；False 表示已经有人答过，
        调用方直接幂等返回。
        """
        card: Card = entry["card"]
        if card.status is not CardStatus.PENDING:
            return False
        card.status = status
        return True

    async def _resolve_card(
        self, entry: dict[str, Any], result: dict[str, Any], status: CardStatus
    ) -> None:
        card: Card = entry["card"]
        card.status = status
        card.result = dict(result)
        card.resolved_at = self._timestamp()
        await self._emit(card.to_event())

    async def answer_card(self, card_id: str, result: dict[str, Any]) -> None:
        try:
            entry = self._cards[card_id]
        except KeyError as exc:
            # 幂等只覆盖「这张卡已经答过」；不认识的 card_id 是另一回事，继续报错。
            raise ValueError(f"未知卡片：{card_id}") from exc
        card: Card = entry["card"]
        if not self._claim_card(entry, CardStatus.ANSWERED):
            return
        await self._resolve_card(entry, result, CardStatus.ANSWERED)
        kind = entry["kind"]
        choice = str(
            result.get("choice", result.get("action", result.get("value", "")))
        ).casefold()
        if kind == "c3":
            switch = bool(result.get("switch_engine")) or any(
                token in choice for token in ("switch", "切换")
            )
            if switch and card.agent_id is not None:
                await self._emit({
                    "type": "route_override_requested",
                    "data": {
                        "scope": "agent",
                        "agent_id": card.agent_id,
                        "after_attempt": entry["route_after_attempt"],
                    },
                })
            return
        if kind == "intervene":
            adjusts = any(token in choice for token in ("adjust", "调整"))
            if adjusts:
                await self._emit({
                    "type": "intervention_adjustment_requested",
                    "data": {"goal_id": card.goal_id},
                })
                if card.goal_id is not None:
                    await self._create_intervention_card(self._goals[card.goal_id])
                return
            if card.goal_id is not None:
                await self._set_goal_status(card.goal_id, "done")
            await self._drive()
            return
        if kind == "r8":
            rejects = any(
                token in choice for token in ("reject", "不接受", "切换")
            )
            accepts = not rejects and (
                bool(result.get("continue_with_overage")) or any(
                    token in choice for token in ("accept", "接受")
                )
            )
            if not accepts:
                await self._emit({
                    "type": "route_override_requested",
                    "data": {"scope": "research", "after_attempt": 0},
                })
            await self._emit({
                "type": "route_gate_release_requested",
                "data": {"scope": "research"},
            })
            await self._drive()

    async def _expire_r8(self, card_id: str) -> None:
        entry = self._cards.get(card_id)
        if entry is None:
            return
        if not self._claim_card(entry, CardStatus.EXPIRED_DEFAULTED):
            return
        await self._resolve_card(
            entry,
            {"choice": "switch", "defaulted": True},
            CardStatus.EXPIRED_DEFAULTED,
        )
        await self._emit({
            "type": "route_override_requested",
            "data": {"scope": "research", "after_attempt": 0},
        })
        await self._emit({
            "type": "route_gate_release_requested",
            "data": {"scope": "research"},
        })
        await self._drive()
