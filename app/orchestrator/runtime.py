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
from app.orchestrator.scheduler import Scheduler
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
    ) -> None:
        self.store = store
        self.events = event_buffer
        self.researches = researches
        self.cards = cards
        self.adapter_factory = adapter_factory or RoutedAdapter
        self.runs_root = Path(runs_root)
        self.auto_confirm = (
            os.getenv("OWLI_AUTO_CONFIRM") == "1"
            if auto_confirm is None
            else auto_confirm
        )
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

    async def prepare_research(self, research_id: str, query: str) -> Plan:
        adapter = self.adapter_factory()
        self._adapters[research_id] = adapter
        plan = await generate_plan(query, self.store, adapter)
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
        acceptance = "；".join(str(item) for item in goal.acceptance)
        body = (
            f"Goal 目标：{goal.objective}\n"
            f"Agent 任务：{agent.task}\n"
            f"验收条件：{acceptance}\n\n"
            f"{agent.prompt['body']}"
        )
        if kind in {"report", "report_writing"}:
            body = f"{body}\n\n{self._decision_context(plan)}"
        return EngineTask(
            body=body,
            output_path=self.runs_root / plan.research_id / str(agent.output["path"]),
            output_format=str(agent.output["format"]),
            research_id=plan.research_id,
            goal_id=context.goal_id,
            agent_id=agent.agent_id,
            agent_kind=kind,
            validators=list(agent.output["validators"]),
            capability=Capability(**agent.capability),
            model=agent.model,
            user_override=context.engine,
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
        task = self._task(plan, agent, context)
        adapter = self._adapters[plan.research_id]

        async def on_event(event: Any) -> None:
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

        return await adapter.run(task, self._ctx(task), on_event=on_event)

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
            if self.auto_confirm and card.card_type is CardType.INTERVENE and card.status is CardStatus.PENDING:
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
        )
        self._schedulers[plan.research_id] = scheduler
        await scheduler.start()
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
        await self._finalize_if_terminal(research_id)

    async def stop(self, research_id: str) -> None:
        scheduler = self.scheduler_for(research_id)
        if scheduler is None:
            raise RuntimeError("Scheduler 尚未启动")
        await scheduler.stop()

    def _report_path(self, plan: Plan) -> Path:
        report_agents = [
            agent
            for goal in plan.goals
            for agent in goal.agents
            if str(agent.capability.get("profile")) == "report-writer"
            and str(agent.output.get("format")) == "markdown"
        ]
        if report_agents:
            return self.runs_root / plan.research_id / str(report_agents[-1].output["path"])
        return self.runs_root / plan.research_id / "goals" / plan.goals[-1].goal_id / "report.md"

    def _append_decision_notes(self, path: Path, plan: Plan, scheduler: Any) -> None:
        if path.is_file():
            text = path.read_text(encoding="utf-8").rstrip()
        else:
            text = "# 结论\n\n- 本次运行未生成完整结论。\n\n# 信息源\n\n- 无可用信息源。"
        failed = [
            goal_id for goal_id, status in scheduler.goal_statuses.items()
            if status in {"failed", "skipped"}
        ]
        references = " ".join(f"[^{item['q_id']}]" for item in plan.decision_balance)
        block = ["", "## 决策天平注释", f"- 本报告按已确认的调研口径生成。{references}"]
        if failed:
            block.append(f"- 未完成 goal：{', '.join(failed)}。")
        for item in plan.decision_balance:
            answer = json.dumps(item["answer"], ensure_ascii=False)
            block.append(f"[^{item['q_id']}]: 问题：{item['question']}；答案：{answer}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{text}\n" + "\n".join(block) + "\n", encoding="utf-8")

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
        report_path = self._report_path(plan)
        self._append_decision_notes(report_path, plan, scheduler)
        report_validators = [
            "file_exists",
            "sections_exist:结论,信息源",
            "citation_marks_resolvable",
            "no_orphan_citation",
        ]
        relative = report_path.relative_to(self.runs_root / research_id)
        goal_id = relative.parts[1] if len(relative.parts) > 1 and relative.parts[0] == "goals" else plan.goals[-1].goal_id
        validation_ctx = validation.Ctx(
            output_path=report_path,
            output_format="markdown",
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
        failed = validation_report.verdict is not validation.Verdict.PASS or any(
            status in {"failed", "skipped"}
            for status in scheduler.goal_statuses.values()
        )
        report_status = "failed" if failed else "completed"
        try:
            stored_path = str(report_path.relative_to(Path(__file__).resolve().parents[2]))
        except ValueError:
            stored_path = str(report_path)
        self.store.finish_report(
            research_id,
            status=report_status,
            completed_at=self.now_iso(),
            summary="计划执行完成，报告已生成",
            summary_line="部分 goal 失败" if failed else "全部 goal 已完成",
            report_path=stored_path,
        )
        state = self.researches[research_id]
        state["status"] = report_status
        state["status_label"] = "已完成" if not failed else "执行失败"
        state["actions"] = []
        state["progress"]["summary"] = (
            "报告已生成，包含失败 goal 说明" if failed else "报告已生成并通过计划执行"
        )
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
