"""M2-c 计划树执行器：只推进运行时状态，不改写计划书。"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Awaitable, Callable, Mapping

from app.adapters.ratelimit import RouteState
from app.plan.cards import (
    Card,
    CardActionType,
    CardBlocking,
    CardStatus,
    CardType,
)
from app.plan.model import Agent, Goal, Plan


R8_CONFIRM_SECONDS = 15 * 60


@dataclass(frozen=True)
class TaskRunResult:
    """单 agent 的结构化判定；成败与进程退出码无关。"""

    succeeded: bool
    engine: str | None = None
    route_decisions: tuple[Any, ...] = field(default_factory=tuple)
    failure_feedback: str | None = None


@dataclass(frozen=True)
class TaskContext:
    research_id: str
    goal_id: str
    attempt: int
    round_number: int
    engine: str
    on_event: Callable[[Any], Awaitable[None]]
    failure_feedback: str | None = None


RunTask = Callable[[Agent, TaskContext], Awaitable[TaskRunResult | Any]]
Emit = Callable[[Any], Any]
Clock = Callable[[], datetime]
Timer = Callable[[float, Callable[[], Any]], Any]


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
    validation = _field(result, "validation", "report")
    for item in getattr(validation, "results", ()) or ():
        verdict = str(getattr(item, "verdict", "")).lower()
        if verdict and verdict != "pass":
            parts.append(
                f"产物校验未过（{getattr(item, 'name', '')}）："
                f"{getattr(item, 'message', '')}"
            )
    return "\n".join(parts) or None


class Scheduler:
    """按 goal/agent 两级 DAG 推进，并执行重试、限流和发卡策略。"""

    def __init__(
        self,
        plan: Plan,
        run_task: RunTask,
        emit: Emit,
        clock: Clock,
        timer: Timer,
    ) -> None:
        self.plan = plan
        self._run_task = run_task
        self._emit_callback = emit
        self._clock = clock
        self._timer = timer
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
        self.emitted_events: list[Any] = []
        self._paused = False
        self._started = False
        self._tasks: dict[asyncio.Task[Any], tuple[str, str]] = {}
        self._state_lock = asyncio.Lock()
        self._goal_started_at: dict[str, datetime] = {}
        self._attempts: dict[str, int] = {}
        self._cards: dict[str, dict[str, Any]] = {}
        self._card_sequence = 0
        self._agent_feedback: dict[str, str | None] = {}

    def _validate_graphs(self) -> None:
        goal_graph = {
            goal.goal_id: list(goal.depends_on) for goal in self.plan.goals
        }
        _assert_acyclic(goal_graph, "goal")
        for goal in self.plan.goals:
            agent_graph = {
                agent.agent_id: list(agent.depends_on) for agent in goal.agents
            }
            _assert_acyclic(agent_graph, f"{goal.goal_id} agent")

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

    async def resume(self) -> None:
        if self.status == "stopped":
            return
        self._paused = False
        self.status = "running"
        await self._emit({"type": "scheduler_update", "data": {"status": "running"}})
        await self._drive()

    async def stop(self) -> None:
        if self.status == "stopped":
            return
        self.status = "stopped"
        self._paused = True
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
        if not all(self.agent_statuses[item] == "done" for item in agent.depends_on):
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
        asyncio.create_task(self._drive())

    def _set_completed_if_terminal(self) -> None:
        if self.status in {"stopped", "paused"}:
            return
        terminal = {"done", "failed", "skipped"}
        if all(status in terminal for status in self.goal_statuses.values()):
            self.status = "completed"

    async def _settle_goals(self) -> None:
        for goal in self.plan.goals:
            if self.goal_statuses[goal.goal_id] != "running":
                continue
            statuses = [self.agent_statuses[agent.agent_id] for agent in goal.agents]
            if any(status == "failed" for status in statuses):
                await self._fail_goal(goal.goal_id, "agent_exhausted")
                continue
            if statuses and all(status == "done" for status in statuses):
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
        )

    async def _execute_agent(self, goal: Goal, agent: Agent) -> None:
        policy = goal.retry_policy
        per_round = int(policy["max_attempts_per_round"])
        total = per_round * int(policy["max_rounds"])
        ask_at = int(policy["ask_engine_switch_at"])
        while self._attempts.get(agent.agent_id, 0) < total:
            if self.status == "stopped" or self.goal_statuses[goal.goal_id] != "running":
                return
            attempt = self._attempts.get(agent.agent_id, 0) + 1
            if self._paused:
                await self._set_agent_status(agent.agent_id, "retrying")
                return
            self._attempts[agent.agent_id] = attempt
            await self._set_agent_status(
                agent.agent_id, "running" if attempt == 1 else "retrying"
            )
            context = TaskContext(
                research_id=self.plan.research_id,
                goal_id=goal.goal_id,
                attempt=attempt,
                round_number=((attempt - 1) // per_round) + 1,
                engine=agent.engine,
                on_event=self._consume_signal,
                failure_feedback=self._agent_feedback.get(agent.agent_id),
            )
            try:
                result = self._normalize_result(await self._run_task(agent, context))
            except asyncio.CancelledError:
                return
            except Exception as exc:
                result = TaskRunResult(False, agent.engine)
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
            if self.status == "stopped" or self.goal_statuses[goal.goal_id] != "running":
                return
            if result.succeeded:
                await self._set_agent_status(agent.agent_id, "done")
                return
            self._agent_feedback[agent.agent_id] = result.failure_feedback
            if attempt == ask_at:
                await self._create_engine_switch_card(goal, agent, per_round)
            if attempt < total:
                continue
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
            raise ValueError(f"未知卡片：{card_id}") from exc
        card: Card = entry["card"]
        if card.status is not CardStatus.PENDING:
            raise RuntimeError(f"卡片已处理：{card_id}")
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
        card: Card = entry["card"]
        if card.status is not CardStatus.PENDING:
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
