"""M2-e 运行期协调层：计划生成、真实时间、Scheduler 注册与 SSE 投影。"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from app.adapters import validation
from app.adapters.capability import Capability
from app.adapters.contracts import EngineTask
from app.adapters.routing import RoutedAdapter
from app.config import ResearchScaleConfig, load_research_scale_config
from app.orchestrator.scheduler import Scheduler, TaskRunResult
from app.orchestrator.sectioning import run_sectioned_task, should_section
from app.plan.cards import (
    Card,
    CardActionType,
    CardBlocking,
    CardStatus,
    CardType,
)
from app.plan.generate import generate_plan
from app.plan.editing import apply_edit, approve
from app.plan.model import Plan
from app.plan.store import load_plan
from app.report.markdown import enrich_source_section, load_evidence_artifacts
from app.reliability.audit import degrade_after_closed_set_retry


AdapterFactory = Callable[[], Any]


class RuntimeCoordinator:
    """每个 FastAPI 进程唯一的运行期协调器。"""

    def __init__(
        self,
        *,
        store: Any,
        event_buffer: Any,
        researches: dict[str, dict[str, Any]],
        cards: dict[str, Card],
        adapter_factory: AdapterFactory | None = None,
        runs_root: str | Path = validation.RUNS_ROOT,
        auto_confirm: bool | None = None,
        routing_utc_clock: Callable[[], datetime],
        scale_config: ResearchScaleConfig | None = None,
    ) -> None:
        self.store = store
        self.events = event_buffer
        self.researches = researches
        self.cards = cards
        self.adapter_factory = adapter_factory or (
            lambda: RoutedAdapter(
                utc_clock=routing_utc_clock,
            )
        )
        self.runs_root = Path(runs_root)
        self.auto_confirm = (
            os.getenv("OWLI_AUTO_CONFIRM") == "1"
            if auto_confirm is None
            else auto_confirm
        )
        self.unattended = os.getenv("OWLI_UNATTENDED") == "1"
        self.scale_config = scale_config or load_research_scale_config()
        self._adapters: dict[str, Any] = {}
        self._schedulers: dict[str, Any] = {}
        self._finalized: set[str] = set()
        self._auto_tasks: set[asyncio.Task[Any]] = set()
        setattr(self.store, "runs_root", self.runs_root)

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def now_iso(self) -> str:
        return self.now().isoformat()

    def scheduler_for(self, research_id: str) -> Any | None:
        return self._schedulers.get(research_id)

    def completed_goal_ids(self, research_id: str) -> set[str]:
        scheduler = self.scheduler_for(research_id)
        if scheduler is None:
            return set()
        return {
            goal_id
            for goal_id, status in scheduler.goal_statuses.items()
            if status in {"done", "awaiting_intervention"}
        }

    def update_plan(self, plan: Plan) -> None:
        scheduler = self.scheduler_for(plan.research_id)
        if scheduler is not None:
            scheduler.update_plan(plan)

    async def sync_question_cards(self, plan: Plan) -> None:
        answers = {
            str(item["q_id"]): item.get("answer")
            for item in plan.decision_balance
            if item.get("answer") not in (None, "", [], {})
        }
        state = self.researches.get(plan.research_id)
        if state is None:
            return
        for card in self.cards.values():
            if (
                card.research_id != plan.research_id
                or card.card_type is not CardType.QUESTION
                or card.status is not CardStatus.PENDING
            ):
                continue
            q_id = str(card.target.get("q_id", ""))
            if q_id not in answers:
                continue
            card.status = CardStatus.ANSWERED
            card.result = {
                "action": "plan_edit",
                "choice": answers[q_id],
                "auto": False,
            }
            card.resolved_at = self.now_iso()
            state["cards"] = [
                card.to_dict() if item.get("card_id") == card.card_id else item
                for item in state.get("cards", [])
            ]
            await self.events.publish(plan.research_id, card.to_event())

    def timer(self, delay_seconds: float, callback: Callable[[], Any]) -> Any:
        """Scheduler 唯一的真实 timer 实现。"""
        loop = asyncio.get_running_loop()

        def invoke() -> None:
            result = callback()
            if inspect.isawaitable(result):
                task = asyncio.create_task(result)
                self._track_auto_task(task)

        return loop.call_later(delay_seconds, invoke)

    def _track_auto_task(self, task: asyncio.Task[Any]) -> None:
        self._auto_tasks.add(task)
        task.add_done_callback(self._auto_tasks.discard)

    async def _drain_auto_tasks(
        self,
        *,
        max_rounds: int = 20,
        timeout_seconds: float = 10.0,
    ) -> None:
        """有限排干自动操作；每轮完成后重新扫描可能新增的后继任务。"""

        for _ in range(max_rounds):
            pending = [task for task in self._auto_tasks if not task.done()]
            if not pending:
                return
            await asyncio.wait(pending, timeout=timeout_seconds)

    def initial_state(self, research_id: str, query: str) -> dict[str, Any]:
        return {
            "research_id": research_id,
            "title": query,
            "status": "planning",
            "status_label": "正在生成计划",
            "progress": {"done": 0, "total": 0, "summary": "正在生成调研计划"},
            "actions": [],
            "goals": [],
            "cards": [],
            "events": [],
        }

    def running_actions(self, research_id: str) -> list[dict[str, str]]:
        return [
            {
                "id": "pause",
                "label": "暂停",
                "method": "POST",
                "href": f"/api/researches/{research_id}/pause",
            },
            {
                "id": "stop",
                "label": "停止",
                "method": "POST",
                "href": f"/api/researches/{research_id}/stop",
            },
        ]

    def _state_from_plan(self, plan: Plan) -> dict[str, Any]:
        return {
            "research_id": plan.research_id,
            "title": plan.title,
            "status": "awaiting_review",
            "status_label": "等待核对计划",
            "progress": {
                "done": 0,
                "total": len(plan.goals),
                "summary": "计划已生成，等待回答追问并批准",
            },
            "actions": [],
            "goals": [
                {
                    "id": goal.goal_id,
                    "title": goal.title,
                    "status": "pending",
                    "summary": goal.objective,
                    "agents": [
                        {
                            "id": agent.agent_id,
                            "name": agent.display_name,
                            "engine": agent.engine,
                            "status": "queued",
                            "activity": agent.task,
                        }
                        for agent in goal.agents
                    ],
                }
                for goal in plan.goals
            ],
            "cards": [],
            "events": [],
        }

    async def _publish_question(self, plan: Plan, question: dict[str, Any]) -> None:
        card = Card(
            card_id=f"{plan.research_id}-{question['q_id']}",
            card_type=CardType.QUESTION,
            research_id=plan.research_id,
            goal_id=None,
            agent_id=None,
            title=str(question["question"]),
            body="该答案会作为报告内注释，并影响计划中标记的 goal/agent。",
            target={"q_id": question["q_id"], "affects": list(question["affects"])},
            actions=[
                {
                    "type": CardActionType.CHOICE_2.value,
                    "id": f"option-{index}",
                    "label": str(option),
                    "value": option,
                }
                for index, option in enumerate(question["options"])
            ],
            blocking=CardBlocking.RESEARCH,
            deadline=None,
            status=CardStatus.PENDING,
            result=None,
            created_at=self.now_iso(),
            resolved_at=None,
        )
        self.cards[card.card_id] = card
        state = self.researches[plan.research_id]
        state["cards"].append(card.to_dict())
        await self.events.publish(plan.research_id, card.to_event())
        if self.auto_confirm:
            first = card.actions[0]
            await self.respond_card(
                card.card_id,
                action=str(first["id"]),
                payload={"choice": first["value"], "auto": True},
            )

    async def prepare_research(
        self, research_id: str, query: str, *, scale: str = "standard"
    ) -> Plan:
        adapter = self.adapter_factory()
        self._adapters[research_id] = adapter
        plan = await generate_plan(
            query,
            self.store,
            adapter,
            scale=scale,
            scale_config=self.scale_config,
        )
        if plan.research_id != research_id:
            raise RuntimeError(
                f"计划 research_id 与请求不一致：{plan.research_id} != {research_id}"
            )
        self.researches[research_id] = self._state_from_plan(plan)
        await self.events.publish(
            research_id,
            {"type": "research_update", "data": self.researches[research_id]},
        )
        for question in plan.decision_balance:
            await self._publish_question(plan, question)
        if self.auto_confirm:
            answered = load_plan(self.store, research_id)
            if answered is None:
                raise RuntimeError("自动批准前无法读取计划")
            approved = approve(self.store, answered, at=self.now_iso())
            state = self.researches[research_id]
            state["status"] = "approved"
            state["status_label"] = "计划已冻结"
            state["actions"] = self.running_actions(research_id)
            await self.events.publish(
                research_id,
                {
                    "type": "research_update",
                    "data": {
                        "status": "approved",
                        "status_label": "计划已冻结",
                        "actions": state["actions"],
                    },
                },
            )
            await self.start_research(approved)
            return approved
        return plan

    def _agent_kind(self, agent: Any) -> str:
        prefixes = {
            "goal-planning": "goal_planning",
            "plan-arbitration": "plan_arbitration",
            "reliability-audit": "reliability_audit",
            "cross-validation": "cross_validation",
            "consistency-check": "consistency_check",
            "report-writing": "report_writing",
            "summary": "summary",
            "tagging": "tagging",
            "data-collection": "data_collection",
            "browser-automation": "browser_automation",
            "code-execution": "code_execution",
            "excel-generation": "excel_generation",
            "data-cleaning": "data_cleaning",
        }
        for prefix, kind in prefixes.items():
            if agent.agent_id == prefix or agent.agent_id.startswith(f"{prefix}-"):
                return kind
        return {
            "web-collector": "data_collection",
            "report-writer": "report_writing",
            "sandboxed-runner": "code_execution",
            "readonly-analyst": "audit",
        }.get(str(agent.capability.get("profile")), "audit")

    def _decision_context(self, plan: Plan) -> str:
        lines = ["决策天平答案（报告必须用对应 q-<n> Markdown 注释角标引用）："]
        for item in plan.decision_balance:
            lines.append(
                f"- {item['q_id']}｜问题：{item['question']}｜答案："
                f"{json.dumps(item['answer'], ensure_ascii=False)}"
            )
        return "\n".join(lines)

    def _ctx(self, task: EngineTask) -> validation.Ctx:
        cache: dict[str, Any] = {}

        def read_text() -> str:
            if "text" not in cache:
                cache["text"] = task.output_path.read_text(encoding="utf-8")
            return cache["text"]

        def read_json() -> Any:
            if "json" not in cache:
                cache["json"] = json.loads(read_text())
            return cache["json"]

        return validation.Ctx(
            output_path=task.output_path,
            output_format=task.output_format,
            research_id=task.research_id,
            goal_id=task.goal_id,
            agent_id=task.agent_id,
            read_text=read_text,
            read_json=read_json,
            store=self.store,
            source_domains=frozenset({"news.ycombinator.com"}),
            runs_root=self.runs_root,
        )

    def _task(self, plan: Plan, agent: Any, context: Any) -> EngineTask:
        kind = self._agent_kind(agent)
        goal = next(item for item in plan.goals if item.goal_id == context.goal_id)
        output_path = self.runs_root / plan.research_id / str(agent.output["path"])
        sources = list(agent.capability.get("sources", []))
        source_item_limit = (
            self.scale_config.profile(plan.scale).source_item_limits.get(sources[0])
            if len(sources) == 1
            else None
        )
        body = (
            f"Goal 目标：{goal.objective}\n"
            f"Agent 任务：{agent.task}\n"
            f"产物落盘路径（写文件与 owli-result.output_path 都逐字用它）：{output_path}\n\n"
            f"{agent.prompt['body']}"
        )
        if str(agent.output.get("path")) == str(goal.deliverable.get("path")):
            acceptance = "；".join(str(item) for item in goal.acceptance)
            body = f"{body}\nGoal 验收条件：{acceptance}"
        chapter = agent.chapter if isinstance(agent.chapter, dict) else None
        if chapter is not None:
            body = (
                f"{body}\n\n本章结构化开头："
                f"{json.dumps(chapter['opening'], ensure_ascii=False)}\n"
                f"本章计划结尾（不得冒充实际结果）："
                f"{json.dumps(chapter['closing'], ensure_ascii=False)}"
            )
        if kind in {"cross_validation", "comparison", "report", "report_writing"}:
            rows = self.store.list_chapters(plan.research_id)
            ledger_inputs = [
                {
                    "goal_id": row["goal_id"],
                    "chapter_id": row["chapter_id"],
                    "status": row["status"],
                    "path": row["actual_output_path"] if row["status"] == "done" else None,
                    "actual_count": row["actual_count"] if row["status"] == "done" else None,
                    "reason": row["reason"] if row["status"] in {"missing", "deferred"} else None,
                }
                for row in rows
            ]
            body = (
                f"{body}\n\n执行账本输入（只按 status/path/reason 读取，不猜测措辞）：\n"
                f"{json.dumps(ledger_inputs, ensure_ascii=False, indent=2)}"
            )
        if kind in {"report", "report_writing"}:
            body = (
                f"{body}\n\n{self._decision_context(plan)}\n"
                "报告必须包含标题为‘缺失清单’的小节；逐条写出账本中的 "
                "missing 章 chapter_id 及其 reason，若没有则明确写‘无’。"
            )
        feedback = getattr(context, "failure_feedback", None)
        if feedback:
            body = (
                f"{body}\n\n上一轮判定失败原因（逐条修正后重做，"
                f"不要原样重复上一轮输出）：\n{feedback}"
            )
        return EngineTask(
            body=body,
            output_path=output_path,
            output_format=str(agent.output["format"]),
            research_id=plan.research_id,
            goal_id=context.goal_id,
            agent_id=agent.agent_id,
            agent_kind=kind,
            validators=list(agent.output["validators"]),
            capability=Capability(**agent.capability),
            model=agent.model,
            user_override=context.engine,
            source_item_limit=source_item_limit,
            runs_root=self.runs_root,
        )

    def _plain(self, value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, Enum):
            return self._plain(value.value)
        if isinstance(value, dict):
            return {str(key): self._plain(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._plain(item) for item in value]
        if hasattr(value, "__dict__"):
            return self._plain(vars(value))
        return repr(value)

    async def _run_task(self, plan: Plan, agent: Any, context: Any) -> Any:
        kind = self._agent_kind(agent)
        task = self._task(plan, agent, context)
        adapter = self._adapters[plan.research_id]
        observed_causes: set[str] = set()

        async def on_event(event: Any) -> None:
            if isinstance(event, dict):
                cause = event.get("cause")
                raw = event.get("raw")
            else:
                cause = getattr(event, "cause", None)
                raw = getattr(event, "raw", None)
            if cause is not None:
                observed_causes.add(str(getattr(cause, "value", cause)))
            if isinstance(raw, dict) and raw.get("http_status", raw.get("status_code")) == 429:
                observed_causes.add("rate_limit")
            if isinstance(event, dict):
                payload = dict(event)
                if payload.get("type") == "card_update":
                    await self._emit_scheduler_event(plan.research_id, payload)
                    return
                await self.events.publish(plan.research_id, payload)
                signal = payload.get("data")
                await context.on_event(signal if isinstance(signal, dict) else payload)
                return
            await self.events.publish(
                plan.research_id,
                {
                    "type": "normalized_event",
                    "raw": self._plain(getattr(event, "raw", None)),
                    "data": {
                        "goal_id": context.goal_id,
                        "agent_id": agent.agent_id,
                        "item_kind": self._plain(getattr(event, "item_kind", None)),
                        "text": str(getattr(event, "text", "")),
                        "is_error": bool(getattr(event, "is_error", False)),
                    },
                },
            )
            await context.on_event(event)

        if should_section(kind, task.output_format):
            return await run_sectioned_task(
                plan=plan,
                agent=agent,
                context=context,
                base_task=task,
                adapter=adapter,
                store=self.store,
                runs_root=self.runs_root,
                now_iso=self.now_iso,
                on_event=on_event,
            )

        result = await adapter.run(task, self._ctx(task), on_event=on_event)
        engine_error = getattr(result, "engine_error", None)
        conclusion_error = getattr(result, "conclusion_error", None)
        if (
            kind == "reliability_audit"
            and not bool(getattr(result, "succeeded", False))
            and task.output_path.is_file()
        ):
            closed_set_report = validation.validate(
                self._ctx(task), ["field_domain_whitelist:reliability_closed_set"]
            )
            closed_set_failed = closed_set_report.verdict is validation.Verdict.FAIL
            if closed_set_failed and context.attempt < 3:
                return TaskRunResult(
                    False,
                    engine=context.engine,
                    failure_feedback=(
                        "authority_kind / interest_relation 越出 source-reliability "
                        "§1.1/§1.5 闭集；必须逐条改为闭集字面值"
                    ),
                    engine_error=engine_error,
                    conclusion_error=conclusion_error,
                )
            if closed_set_failed and context.attempt >= 3:
                try:
                    items = json.loads(task.output_path.read_text(encoding="utf-8"))
                    if not isinstance(items, list):
                        return result
                    degraded = degrade_after_closed_set_retry(items)
                    task.output_path.write_text(
                        json.dumps(degraded, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    repaired = validation.validate(self._ctx(task), task.validators)
                except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
                    return result
                if repaired.verdict is validation.Verdict.PASS:
                    conclusion = getattr(result, "conclusion", None)
                    if conclusion is not None and not getattr(result, "conclusion_error", None):
                        from dataclasses import replace

                        try:
                            return replace(result, validation=repaired)
                        except TypeError:
                            pass
                    # 只修复产物腿；缺 owli-result 结构化结论时仍保留原失败。
                    return result
        conclusion = getattr(result, "conclusion", None)
        reason = getattr(conclusion, "reason", None) if conclusion is not None else None
        causes = {
            getattr(event, "cause", None)
            for event in (getattr(result, "events", None) or [])
        }
        causes.update(observed_causes)
        actual_path = str(task.output_path) if task.output_path.is_file() else None
        actual_count = None
        if task.output_path.is_file() and task.output_format == "json":
            try:
                artifact = json.loads(task.output_path.read_text(encoding="utf-8"))
                if isinstance(artifact, list):
                    actual_count = len(artifact)
            except (OSError, UnicodeError, json.JSONDecodeError):
                pass
        if reason == "quota_exhausted" or "rate_limit" in causes:
            return TaskRunResult(
                False, engine=context.engine, chapter_status="deferred",
                reason="quota_exhausted", actual_output_path=actual_path,
                actual_count=actual_count,
                engine_error=engine_error, conclusion_error=conclusion_error,
            )
        # succeeded 必须排在 reason 短路之前：硬约束 4a 的前提是「产物为空」，
        # 而「产物为空」由 succeeded（validation PASS + min_items + unmet 非空）天然承担。
        # 反过来先看 reason，会把「产物合法 + partial + unmet 齐全 + 如实写 reason」
        # 的章误判成 missing，C7 的 _record_unmet() 在这条路径上永不执行。
        if bool(getattr(result, "succeeded", False)):
            if (
                conclusion is not None
                and getattr(conclusion, "status", None) == "partial"
                and getattr(conclusion, "unmet", None)
            ):
                self._record_unmet(plan.research_id, context.goal_id, agent, conclusion)
            return TaskRunResult(
                True, engine=context.engine, actual_output_path=actual_path,
                actual_count=actual_count,
                engine_error=engine_error, conclusion_error=conclusion_error,
            )
        if reason in {"empty_result", "tool_unavailable"}:
            return TaskRunResult(
                False, engine=context.engine, chapter_status="missing", reason=reason,
                actual_output_path=actual_path, actual_count=actual_count,
                engine_error=engine_error, conclusion_error=conclusion_error,
            )
        return result

    def _record_unmet(
        self, research_id: str, goal_id: str, agent: Any, conclusion: Any
    ) -> None:
        root = self.runs_root / research_id / "goals" / goal_id
        root.mkdir(parents=True, exist_ok=True)
        path = root / f".owli-unmet-{agent.agent_id}.json"
        chapter = agent.chapter if isinstance(agent.chapter, dict) else {}
        payload = {
            "goal_id": goal_id,
            "chapter_id": str(chapter.get("chapter_id") or agent.agent_id),
            "agent_id": agent.agent_id,
            "unmet": list(getattr(conclusion, "unmet", []) or []),
            "reason": getattr(conclusion, "reason", None),
        }
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _unmet_items(self, research_id: str) -> list[dict[str, Any]]:
        root = self.runs_root / research_id
        items: list[dict[str, Any]] = []
        if not root.exists():
            return items
        for path in sorted(root.glob("goals/goal-*/.owli-unmet-*.json")):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if isinstance(value, dict) and isinstance(value.get("unmet"), list):
                items.append(value)
        return items

    def _expanded_actions(self, card: dict[str, Any]) -> list[dict[str, Any]]:
        actions = list(card.get("actions", []))
        if len(actions) != 1 or not isinstance(actions[0].get("options"), list):
            return actions
        result = []
        for index, label in enumerate(actions[0]["options"]):
            normalized = str(label)
            lowered = normalized.casefold()
            if normalized == "继续":
                action_id, value = "continue", "continue"
            elif "调整" in normalized:
                action_id, value = "adjust", "adjust"
            elif "不接受" in normalized or "保持" in normalized:
                action_id, value = "reject", "reject"
            else:
                action_id, value = f"choice-{index}", lowered
            result.append({
                "type": actions[0]["type"],
                "id": action_id,
                "label": normalized,
                "value": value,
                "default": index == 0,
            })
        return result

    def _external_card(self, source: dict[str, Any]) -> Card:
        payload = dict(source)
        payload["actions"] = self._expanded_actions(payload)
        return Card(**payload)

    async def _emit_scheduler_event(self, research_id: str, event: Any) -> None:
        payload = dict(event)
        state = self.researches[research_id]
        kind = payload.get("type")
        data = payload.get("data", {})
        adapter = self._adapters.get(research_id)
        if kind == "route_override_requested" and adapter is not None:
            requester = getattr(adapter, "request_alternate", None)
            if requester is not None:
                requester(
                    research_id,
                    agent_id=(
                        str(data["agent_id"])
                        if data.get("scope") == "agent" and data.get("agent_id")
                        else None
                    ),
                    after_attempt=int(data.get("after_attempt", 0)),
                )
        elif kind == "route_gate_release_requested" and adapter is not None:
            release = getattr(adapter, "release_route_gate", None)
            if release is not None:
                release(research_id)
        if kind == "goal_update":
            goal = next(item for item in state["goals"] if item["id"] == data["goal_id"])
            goal["status"] = data["status"]
        elif kind == "agent_update":
            goal = next(item for item in state["goals"] if item["id"] == data["goal_id"])
            agent = next(item for item in goal["agents"] if item["id"] == data["agent_id"])
            agent["status"] = data["status"]
        elif kind == "progress":
            state["progress"].update(done=data["done"], total=data["total"])
        elif kind == "scheduler_update":
            state["status"] = data["status"]
            state["status_label"] = {
                "paused": "已暂停",
                "running": "运行中",
                "stopped": "已终止",
            }.get(data["status"], data["status"])
        elif kind == "card_update":
            card = self._external_card(data["card"])
            self.cards[card.card_id] = card
            state["cards"] = [
                card.to_dict(),
                *[
                    item for item in state.get("cards", [])
                    if item.get("card_id") != card.card_id
                ],
            ]
            payload = card.to_event()
            current_plan = load_plan(self.store, research_id)
            auto_intervene = self.auto_confirm or self.unattended or (
                current_plan is not None
                and current_plan.scale == "fast"
                and state.get("status") == "running"
                and any(
                    goal.get("status") == "awaiting_intervention"
                    and goal.get("id") == card.goal_id
                    for goal in state.get("goals", [])
                )
            )
            if auto_intervene and card.card_type is CardType.INTERVENE and card.status is CardStatus.PENDING:
                task = asyncio.create_task(self.respond_card(
                    card.card_id,
                    action="continue",
                    payload={"choice": "continue", "auto": True},
                ))
                self._track_auto_task(task)
        await self.events.publish(research_id, payload)

    async def start_research(self, plan: Plan) -> None:
        state = self.researches[plan.research_id]
        state["status"] = "running"
        state["status_label"] = "运行中"
        state["actions"] = self.running_actions(plan.research_id)
        state["progress"]["summary"] = "Scheduler 正在按计划推进"
        await self.events.publish(
            plan.research_id,
            {"type": "research_update", "data": state},
        )
        scheduler: Scheduler
        scheduler = Scheduler(
            plan,
            lambda agent, context: self._run_task(scheduler.plan, agent, context),
            lambda event: self._emit_scheduler_event(plan.research_id, event),
            self.now,
            self.timer,
            chapter_ledger=self.store,
        )
        self._schedulers[plan.research_id] = scheduler
        await scheduler.start()
        await self._drain_auto_tasks()
        await self._finalize_if_terminal(plan.research_id)

    async def respond_card(self, card_id: str, *, action: str, payload: dict[str, Any]) -> Card:
        card = self.cards[card_id]
        if card.status is not CardStatus.PENDING:
            raise RuntimeError(f"卡片已处理：{card_id}")
        if card.card_type is CardType.QUESTION:
            plan = load_plan(self.store, card.research_id)
            if plan is None:
                raise RuntimeError("QUESTION 卡片对应计划不存在")
            submitted = plan.to_dict()
            q_id = str(card.target["q_id"])
            answer = payload.get("choice", payload.get("value"))
            for item in submitted["decision_balance"]:
                if item["q_id"] == q_id:
                    item["answer"] = answer
                    item["answered_at"] = self.now_iso()
                    break
            apply_edit(self.store, plan, submitted, at=self.now_iso())
            card.status = CardStatus.ANSWERED
            card.result = {"action": action, **dict(payload)}
            card.resolved_at = self.now_iso()
            state = self.researches[card.research_id]
            state["cards"] = [
                card.to_dict() if item.get("card_id") == card.card_id else item
                for item in state["cards"]
            ]
            await self.events.publish(card.research_id, card.to_event())
            return card
        scheduler = self.scheduler_for(card.research_id)
        if scheduler is None:
            card.status = CardStatus.ANSWERED
            card.result = {"action": action, **dict(payload)}
            card.resolved_at = self.now_iso()
            state = self.researches[card.research_id]
            state["cards"] = [
                card.to_dict() if item.get("card_id") == card.card_id else item
                for item in state.get("cards", [])
            ]
            await self.events.publish(card.research_id, card.to_event())
            return card
        await scheduler.answer_card(card_id, {"action": action, **dict(payload)})
        await self._finalize_if_terminal(card.research_id)
        return self.cards[card_id]

    async def pause(self, research_id: str) -> None:
        scheduler = self.scheduler_for(research_id)
        if scheduler is None:
            raise RuntimeError("Scheduler 尚未启动")
        await scheduler.pause()

    async def resume(self, research_id: str) -> None:
        scheduler = self.scheduler_for(research_id)
        if scheduler is None:
            raise RuntimeError("Scheduler 尚未启动")
        await scheduler.resume()
        await self._drain_auto_tasks()
        await self._finalize_if_terminal(research_id)

    async def stop(self, research_id: str) -> None:
        scheduler = self.scheduler_for(research_id)
        if scheduler is None:
            raise RuntimeError("Scheduler 尚未启动")
        await scheduler.stop()

    def _report_agents(self, plan: Plan) -> list[Any]:
        """全卷的报告章，按计划顺序；**不按 output.format 过滤**。

        以前两层筛选都要求 markdown，报告章声明成 json 时一个都匹配不上，
        收尾就兜底到一个没有任何 agent 会写的 report.md（缺陷 D 的病根）。
        """
        agents = [
            agent
            for goal in plan.goals
            for agent in goal.agents
            if self._agent_kind(agent) in {"report", "report_writing"}
        ]
        if agents:
            return agents
        return [
            agent
            for goal in plan.goals
            for agent in goal.agents
            if str(agent.capability.get("profile")) == "report-writer"
        ]

    def _report_target(self, plan: Plan) -> tuple[Path, str, bool]:
        """(报告产物路径, 声明格式, 计划是否声明了报告章)。

        取计划里最后一个报告章的**真实声明产物**（它才是全卷交付物），
        格式原样保留；计划没有报告章时才落到 goals/<末 goal>/report.md 由收尾自行汇总。
        """
        agents = self._report_agents(plan)
        if agents:
            agent = agents[-1]
            fmt = str(agent.output.get("format") or "markdown")
            return (
                self.runs_root / plan.research_id / str(agent.output["path"]),
                fmt,
                True,
            )
        fallback = (
            self.runs_root / plan.research_id / "goals"
            / plan.goals[-1].goal_id / "report.md"
        )
        return fallback, "markdown", False

    def _report_path(self, plan: Plan) -> Path:
        return self._report_target(plan)[0]

    def _missing_entries(self, research_id: str) -> list[dict[str, Any]]:
        """章节缺失 + unmet，逐条带 goal/chapter/reason 与成文文本。"""
        entries = [
            {
                "goal_id": row["goal_id"],
                "chapter_id": row["chapter_id"],
                "reason": row["reason"],
                "text": (
                    f"此处缺失：{row['goal_id']}/{row['chapter_id']}"
                    f"；原因：{row['reason']}"
                ),
            }
            for row in self.store.list_chapters(research_id)
            if row["status"] == "missing"
        ]
        for item in self._unmet_items(research_id):
            for index, unmet_text in enumerate(item["unmet"], start=1):
                chapter_id = f"{item['chapter_id']}/unmet-{index}"
                entries.append({
                    "goal_id": item["goal_id"],
                    "chapter_id": chapter_id,
                    "reason": unmet_text,
                    "text": (
                        f"此处缺失：{item['goal_id']}/{chapter_id}；原因：{unmet_text}"
                    ),
                })
        return entries

    def _finalization_notes(self, plan: Plan, scheduler: Any) -> dict[str, Any]:
        return {
            "决策天平": [
                {
                    "q_id": item["q_id"],
                    "问题": item["question"],
                    "答案": item["answer"],
                }
                for item in plan.decision_balance
            ],
            "未完成 goal": [
                goal_id for goal_id, status in scheduler.goal_statuses.items()
                if status in {"failed", "skipped"}
            ],
            "缺失清单": self._missing_entries(plan.research_id),
        }

    def _summary_body(self, plan: Plan) -> str:
        """计划没有报告章时的收尾正文：按账本汇总已完成章，而不是硬写「未生成」。"""
        done = [
            row for row in self.store.list_chapters(plan.research_id)
            if row["status"] == "done"
        ]
        if not done:
            return "# 结论\n\n- 本次运行未生成完整结论。\n\n# 信息源\n\n- 无可用信息源。"
        lines = ["# 结论", "", "- 本计划未声明报告章，以下按章节账本汇总已完成产物。"]
        lines.extend(
            f"- {row['goal_id']}/{row['chapter_id']}：{row['actual_output_path']}"
            for row in done
        )
        lines.extend(["", "# 信息源", ""])
        lines.extend(
            f"- {row['goal_id']}/{row['chapter_id']} 产物：{row['actual_output_path']}"
            for row in done
        )
        return "\n".join(lines)

    def _append_decision_notes(self, path: Path, plan: Plan, scheduler: Any) -> None:
        """把决策天平注释与缺失清单写进报告产物；按产物格式落盘，不制造假 json。"""
        notes = self._finalization_notes(plan, scheduler)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix.lower() == ".json":
            self._write_json_finalization(path, notes)
            return
        if path.is_file():
            text = path.read_text(encoding="utf-8").rstrip()
            evidence = load_evidence_artifacts(self.runs_root / plan.research_id)
            if evidence:
                text = enrich_source_section(text, evidence).rstrip()
        elif self._report_target(plan)[2]:
            # 声明了报告章却没有产物：如实写「未生成」，收尾据此判 failed。
            text = "# 结论\n\n- 本次运行未生成完整结论。\n\n# 信息源\n\n- 无可用信息源。"
        else:
            text = self._summary_body(plan)
        references = " ".join(f"[^{item['q_id']}]" for item in notes["决策天平"])
        block = ["", "## 决策天平注释", f"- 本报告按已确认的调研口径生成。{references}"]
        if notes["未完成 goal"]:
            block.append(f"- 未完成 goal：{', '.join(notes['未完成 goal'])}。")
        for item in notes["决策天平"]:
            answer = json.dumps(item["答案"], ensure_ascii=False)
            block.append(f"[^{item['q_id']}]: 问题：{item['问题']}；答案：{answer}")
        block.extend(["", "## 缺失清单"])
        if notes["缺失清单"]:
            block.extend(f"- {item['text']}" for item in notes["缺失清单"])
        else:
            block.append("- 无。")
        path.write_text(f"{text}\n" + "\n".join(block) + "\n", encoding="utf-8")

    def _write_json_finalization(self, path: Path, notes: dict[str, Any]) -> None:
        """JSON 报告产物：注释与缺失清单进结构化字段，绝不把 Markdown 拼进 .json。"""
        if path.is_file():
            raw = path.read_text(encoding="utf-8")
            try:
                parsed: Any = json.loads(raw)
            except (json.JSONDecodeError, UnicodeError):
                # 历史遗留的「假 json」（后缀 .json、内容 Markdown）在收尾时修回真 json
                parsed = {"报告正文": raw}
            document = parsed if isinstance(parsed, dict) else {"报告正文": parsed}
        else:
            document = {"报告正文": "本次运行未生成完整结论。"}
        document["收尾注释"] = notes
        path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    async def _finalize_if_terminal(self, research_id: str) -> None:
        if research_id in self._finalized:
            return
        scheduler = self.scheduler_for(research_id)
        if scheduler is None or scheduler.status != "completed":
            return
        self._finalized.add(research_id)
        plan = load_plan(self.store, research_id)
        if plan is None:
            raise RuntimeError("终态计划不存在")
        report_path, report_format, report_declared = self._report_target(plan)
        # 收尾**之前**报告产物是否已存在，是「报告到底生成了没有」的唯一依据；
        # _append_decision_notes 之后文件必然存在，那时再判就永远判不出来。
        report_ready = report_path.is_file()
        self._append_decision_notes(report_path, plan, scheduler)
        if report_format == "markdown":
            report_validators = [
                "file_exists",
                "sections_exist:结论,信息源,缺失清单",
                "citation_marks_resolvable",
                "no_orphan_citation",
                "chapter_missing_items_reported",
            ]
        else:
            # 非 Markdown 报告产物不套 Markdown 章节/角标校验，只验存在与缺失清单齐全。
            report_validators = ["file_exists", "chapter_missing_items_reported"]
        relative = report_path.relative_to(self.runs_root / research_id)
        goal_id = relative.parts[1] if len(relative.parts) > 1 and relative.parts[0] == "goals" else plan.goals[-1].goal_id
        validation_ctx = validation.Ctx(
            output_path=report_path,
            output_format=report_format,
            research_id=research_id,
            goal_id=goal_id,
            agent_id="report-finalizer",
            read_text=lambda: report_path.read_text(encoding="utf-8"),
            read_json=lambda: json.loads(report_path.read_text(encoding="utf-8")),
            store=self.store,
            source_domains=frozenset({"news.ycombinator.com"}),
            runs_root=self.runs_root,
        )
        validation_report = validation.validate(validation_ctx, report_validators)
        failures = [
            {"validator": item.name, "message": item.message, "offenders": item.offenders}
            for item in validation_report.results
            if item.verdict is not validation.Verdict.PASS
        ]
        await self.events.publish(
            research_id,
            {
                "type": "report_validation",
                "data": {
                    "verdict": validation_report.verdict.value,
                    "validators": report_validators,
                    "failures": failures,
                },
            },
        )
        validation_failed = validation_report.verdict is not validation.Verdict.PASS
        # 硬约束 4：报告能生成就 completed，failed 只留给「报告根本没生成」。
        # 校验没过是报告质量告警（已随 report_validation 事件发出），不是研究失败。
        report_missing = report_declared and not report_ready
        report_status = "failed" if report_missing else "completed"
        unfinished_goals = [
            goal_id for goal_id, status in scheduler.goal_statuses.items()
            if status in {"failed", "skipped"}
        ]
        if validation_failed and not report_missing:
            await self.events.publish(
                research_id,
                {
                    "type": "report_warning",
                    "data": {
                        "research_id": research_id,
                        "report_path": str(report_path),
                        "reason": "report_validation_failed",
                        "failures": failures,
                    },
                },
            )
        try:
            stored_path = str(report_path.relative_to(Path(__file__).resolve().parents[2]))
        except ValueError:
            stored_path = str(report_path)
        self.store.finish_report(
            research_id,
            status=report_status,
            completed_at=self.now_iso(),
            summary="计划执行完成，报告已生成" if not report_missing else "报告未生成",
            summary_line=(
                "报告未生成" if report_missing
                else "部分 goal 失败" if unfinished_goals
                else "全部 goal 已完成"
            ),
            report_path=stored_path,
        )
        state = self.researches[research_id]
        state["status"] = report_status
        state["status_label"] = "执行失败" if report_missing else "已完成"
        state["actions"] = []
        if report_missing:
            summary = "报告未生成，请查看章节账本缺失项"
        elif validation_failed:
            summary = "报告已生成，收尾校验有告警"
        elif unfinished_goals:
            summary = "报告已生成，包含失败 goal 说明"
        else:
            summary = "报告已生成并通过计划执行"
        state["progress"]["summary"] = summary
        await self.events.publish(
            research_id,
            {
                "type": "research_update",
                "data": {
                    "status": state["status"],
                    "status_label": state["status_label"],
                    "actions": [],
                    "goals": state["goals"],
                    "report_path": stored_path,
                },
            },
        )


__all__ = ["RuntimeCoordinator"]
