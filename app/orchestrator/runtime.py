"""M2-e 运行期协调层：计划生成、真实时间、Scheduler 注册与 SSE 投影。"""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
import logging
import os
import re
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from app.adapters import validation
from app.adapters.capability import Capability
from app.adapters.contracts import EngineTask
from app.adapters.routing import RoutedAdapter
from app.config import ResearchScaleConfig, load_research_scale_config
from app.orchestrator.background import guard_task
from app.orchestrator.scheduler import Scheduler, TaskRunResult
from app.orchestrator.sectioning import (
    _assemble as assemble_sections,
    _section_specs as section_specs,
    run_sectioned_task,
    should_section,
)
from app.plan.cards import (
    Card,
    CardActionType,
    CardBlocking,
    CardStatus,
    CardType,
)
from app.plan.generate import generate_plan
from app.plan.editing import apply_edit, approve
from app.plan.model import (
    SECTIONED_CHAPTER_KINDS,
    Goal,
    Plan,
    agent_kind_of,
)
from app.plan.store import load_plan, save_plan
from app.report.markdown import (
    enrich_source_section,
    load_evidence_artifacts,
    source_citations,
)
from app.reliability.audit import degrade_after_closed_set_retry
from app.reliability.claims import (
    ClaimsRegistrationError,
    claims_from_documents,
    register_claims,
)
from app.store import evidence_artifacts
from app.store.evidence_artifacts import load_evidence_payloads


AdapterFactory = Callable[[], Any]
logger = logging.getLogger(__name__)

#: 哪些章状态的产物可以投影入库（§SRC-1 货 4）。
#: `done` 是原口径；`missing`/`deferred` 是「章没跑完但文件已经落盘」，
#: 捡回来比丢掉划算——第 6 轮白丢过 20 条带 permalink 的网页搜索证据。
#: 刻意**不含** `running`/`pending`：那时文件可能正写到一半。
_EVIDENCE_PROJECTABLE_STATUSES = frozenset({"done", "missing", "deferred"})
#: 捡回来的证据在 extra 里的留痕键，下游据此分辨来源章没跑完。
_INCOMPLETE_CHAPTER_KEY = "from_incomplete_chapter"

#: 调度器状态 → 工作板状态文案。API 只做映射，不自己猜研究处于什么状态。
SCHEDULER_STATUS_LABELS = {
    "ready": "等待开始",
    "running": "运行中",
    "paused": "已暂停",
    "stopped": "已终止",
    "completed": "已完成",
}
#: 收尾已经落定的状态，不再被调度器状态覆盖。
REPORT_TERMINAL_STATUSES = frozenset({"completed", "failed"})
EMPTY_LLM_USAGE = {
    "input_tokens": 0,
    "cached_input_tokens": 0,
    "cache_creation_input_tokens": 0,
    "cache_write_input_tokens": 0,
    "output_tokens": 0,
    "reasoning_output_tokens": 0,
    "cost_usd": 0.0,
    "calls": 0,
    "costed_calls": 0,
}

REUSE_CONCLUSION_GUARD = "只复用方法与来源配置，不沿用旧报告结论。"


def _replace_reused_subjects(value: str, subjects: list[str], query: str) -> str:
    """把历史研究实体按声明顺序替换成可编辑占位符。"""

    del query
    declared_subjects = list(dict.fromkeys(
        subject.strip() for subject in subjects if subject.strip()
    ))
    if not declared_subjects:
        return value
    placeholders = {
        subject: f"待定实体{index}"
        for index, subject in enumerate(declared_subjects, start=1)
    }
    pattern = re.compile("|".join(
        re.escape(subject)
        for subject in sorted(declared_subjects, key=len, reverse=True)
    ))
    return pattern.sub(lambda match: placeholders[match.group(0)], value)


def _reused_entity_order(raw: dict[str, Any], subjects: list[str]) -> list[str]:
    """合并历史 subjects 与采集章 entity，固定复用占位符的唯一顺序。"""

    entities = [subject.strip() for subject in subjects if subject.strip()]
    for goal in raw.get("goals", []):
        for agent in goal.get("agents", []):
            chapter = agent.get("chapter")
            if (
                not isinstance(chapter, dict)
                or chapter.get("chapter_type") != "collection"
            ):
                continue
            entity = str(agent.get("entity") or "").strip()
            if entity:
                entities.append(entity)
    return list(dict.fromkeys(entities))


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
                source_store=self.store,
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
        self._starting: set[str] = set()
        self._finalized: set[str] = set()
        self._auto_tasks: set[asyncio.Task[Any]] = set()
        self._drive_watchers: set[asyncio.Task[Any]] = set()
        setattr(self.store, "runs_root", self.runs_root)

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def now_iso(self) -> str:
        return self.now().isoformat()

    def _research_usage(self, research_id: str) -> dict[str, int | float]:
        aggregate = getattr(self.store, "aggregate_research_usage", None)
        return dict(EMPTY_LLM_USAGE) if aggregate is None else aggregate(research_id)

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
        # D-013 货 2：后台任务的异常必须被取走并留痕，
        # 否则只剩解释器那句 `Task exception was never retrieved`，goal 已经死等完了。
        guard_task(task, logger=logger, context="自动操作")

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
            "usage": self._research_usage(research_id),
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

    def resume_actions(self, research_id: str) -> list[dict[str, str]]:
        return [
            {
                "id": "resume",
                "label": "继续",
                "method": "POST",
                "href": f"/api/researches/{research_id}/resume",
            },
            {
                "id": "stop",
                "label": "停止",
                "method": "POST",
                "href": f"/api/researches/{research_id}/stop",
            },
        ]

    def sync_state_with_scheduler(self, research_id: str) -> dict[str, Any] | None:
        """把工作板状态对齐到调度器的真实状态；API 回报只读这里，不自己猜。

        已收尾（completed / failed）的研究以收尾结论为准；规划期没有调度器时原样返回。
        """
        state = self.researches.get(research_id)
        if state is None:
            return None
        scheduler = self.scheduler_for(research_id)
        if scheduler is None or state.get("status") in REPORT_TERMINAL_STATUSES:
            state["usage"] = self._research_usage(research_id)
            return state
        status = str(getattr(scheduler, "status", "") or "")
        if status in {"", "ready"}:
            return state
        state["status"] = status
        state["status_label"] = SCHEDULER_STATUS_LABELS.get(status, status)
        if status == "running":
            state["actions"] = self.running_actions(research_id)
        elif status == "completed":
            state["actions"] = []
        state["usage"] = self._research_usage(research_id)
        return state

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
            "usage": self._research_usage(plan.research_id),
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

    async def _publish_question(
        self,
        plan: Plan,
        question: dict[str, Any],
        *,
        auto_respond: bool = True,
    ) -> None:
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
        if self.auto_confirm and auto_respond:
            first = card.actions[0]
            await self.respond_card(
                card.card_id,
                action=str(first["id"]),
                payload={"choice": first["value"], "auto": True},
            )

    async def prepare_research(
        self,
        research_id: str,
        query: str,
        *,
        scale: str = "standard",
    ) -> Plan:
        history_cards_at_start = [
            item for item in self.researches.get(research_id, {}).get("cards", [])
            if item.get("card_type") == CardType.HISTORY_REUSE.value
        ]
        history_gate_seen = bool(history_cards_at_start)
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
        current_history_cards = [
            (
                self.cards[str(item["card_id"])].to_dict()
                if str(item.get("card_id")) in self.cards
                else copy.deepcopy(item)
            )
            for item in history_cards_at_start
        ]
        self.researches[research_id] = self._state_from_plan(plan)
        self.researches[research_id]["cards"] = current_history_cards
        await self.events.publish(
            research_id,
            {"type": "research_update", "data": self.researches[research_id]},
        )
        for question in plan.decision_balance:
            await self._publish_question(
                plan,
                question,
                auto_respond=not history_gate_seen,
            )
        if self.auto_confirm and not history_gate_seen:
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

    async def reuse_plan(
        self,
        research_id: str,
        source_research_id: str,
        query: str,
        *,
        scale: str = "standard",
    ) -> Plan:
        """复用历史结构，重写为当前题目的可编辑初稿。"""

        source = load_plan(self.store, source_research_id)
        if source is None:
            raise ValueError("这条历史记录没有可复用计划，请选择全新开始")
        raw = source.to_dict()
        source_subjects = list(raw.get("subjects", []))
        source_entities = _reused_entity_order(raw, source_subjects)
        decision_balance = copy.deepcopy(raw["decision_balance"])
        for question in decision_balance:
            question["question"] = _replace_reused_subjects(
                question["question"], source_entities, query
            )
            question["options"] = [
                _replace_reused_subjects(option, source_entities, query)
                for option in question["options"]
            ]
            question["answer"] = None
            question["answered_at"] = None
        now = self.now_iso()
        current = load_plan(self.store, research_id)
        expected_rev = 0 if current is None else current.plan_rev
        role_names = {
            "data_collection": "信息采集",
            "data_cleaning": "数据清洗",
            "reliability_audit": "可靠度审计",
            "cross_validation": "交叉验证",
            "consistency_check": "一致性检查",
            "report_writing": "报告撰写",
            "summary": "摘要生成",
            "tagging": "标签生成",
        }
        raw.update({
            "research_id": research_id,
            "plan_rev": expected_rev + 1,
            "title": query[:40],
            "research_question": query,
            "use_case": (
                "social_competitor"
                if any(word in query for word in ("社媒", "小红书", "抖音", "舆情"))
                else "product_competitor"
                if any(word in query for word in ("竞品", "优缺点", "对比", " vs "))
                else "other"
            ),
            "market_profile_justification": (
                "沿用同一研究事项历史计划的市场范围配置，用户需在计划编辑器核对。"
            ),
            "subjects": [],
            "subjects_justification": (
                "复用历史方法时不携带旧题目的实体，用户需在计划编辑器按当前问题补充。"
            ),
            "scale": scale,
            "status": "awaiting_review",
            "approved_at": None,
            "decision_balance": decision_balance,
            "expert_panel": None,
            "change_log": [],
            "baseline": None,
            "baseline_source": f"reused:{source_research_id}",
            "created_at": now,
            "updated_at": now,
        })
        for goal in raw["goals"]:
            goal["title"] = _replace_reused_subjects(
                goal["title"], source_entities, query
            )
            goal["objective"] = _replace_reused_subjects(
                goal["objective"], source_entities, query
            )
            goal["deliverable"]["description"] = _replace_reused_subjects(
                goal["deliverable"]["description"], source_entities, query
            )
            goal["acceptance"] = [
                _replace_reused_subjects(item, source_entities, query)
                for item in goal["acceptance"]
            ]
            goal["intervention"]["prompt"] = _replace_reused_subjects(
                goal["intervention"]["prompt"], source_entities, query
            )
            goal["status"] = "pending"
            for agent in goal["agents"]:
                source_entity = str(agent.get("entity") or "").strip()
                agent["entity"] = None
                kind = agent_kind_of(
                    str(agent["agent_id"]),
                    agent.get("capability", {}).get("profile"),
                )
                agent["display_name"] = role_names.get(kind, "研究分析")
                agent["task"] = _replace_reused_subjects(
                    agent["task"], source_entities, query
                )
                prompt_body = _replace_reused_subjects(
                    agent["prompt"]["body"], source_entities, query
                )
                if "只复用方法与来源配置，不沿用旧报告结论" not in prompt_body:
                    prompt_body = f"{prompt_body.rstrip()}\n复用边界：{REUSE_CONCLUSION_GUARD}"
                agent["prompt"]["body"] = prompt_body
                agent["origin"] = {
                    key: "generated" for key in agent.get("origin", {"_node": "generated"})
                }
                agent["origin"].setdefault("_node", "generated")
                agent["status"] = "queued"
                chapter = agent.get("chapter")
                if isinstance(chapter, dict):
                    is_collection = chapter.get("chapter_type") == "collection"
                    if is_collection:
                        agent["entity"] = _replace_reused_subjects(
                            source_entity, source_entities, query
                        )
                    opening = chapter.get("opening")
                    if isinstance(opening, dict):
                        opening["task"] = agent["task"]
                        opening["acceptance"] = list(goal["acceptance"])
                    closing = chapter.get("closing")
                    if isinstance(closing, dict):
                        closing["entities"] = (
                            [agent["entity"]]
                            if is_collection and agent["entity"]
                            else []
                        )
                        closing["notes"] = {}
        plan = Plan.from_dict(raw)
        save_plan(self.store, plan, expected_rev=expected_rev)
        history_cards = [
            item.to_dict()
            for item in self.cards.values()
            if item.research_id == research_id
            and item.card_type is CardType.HISTORY_REUSE
        ]
        self.researches[research_id] = self._state_from_plan(plan)
        self.researches[research_id]["cards"] = history_cards
        await self.events.publish(
            research_id,
            {"type": "research_update", "data": self.researches[research_id]},
        )
        for question in plan.decision_balance:
            await self._publish_question(plan, question, auto_respond=False)
        return plan

    def _agent_kind(self, agent: Any) -> str:
        return agent_kind_of(agent.agent_id, agent.capability.get("profile"))

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
            source_store_path=getattr(self.store, "_database_path", None),
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
        chapter = agent.chapter if isinstance(agent.chapter, dict) else {}
        chapter_id = str(chapter.get("chapter_id") or agent.agent_id)

        async def on_event(event: Any) -> None:
            if isinstance(event, dict):
                cause = event.get("cause")
                raw = event.get("raw")
            else:
                cause = getattr(event, "cause", None)
                raw = getattr(event, "raw", None)
            usage = None if isinstance(event, dict) else getattr(event, "usage", None)
            research_usage = None
            if isinstance(usage, dict):
                try:
                    self.store.record_chapter_usage(
                        plan.research_id,
                        context.goal_id,
                        chapter_id,
                        usage,
                    )
                    research_usage = self._research_usage(plan.research_id)
                    state = self.researches.get(plan.research_id)
                    if state is not None:
                        state["usage"] = research_usage
                except Exception:
                    logger.exception(
                        "LLM usage 计量失败，不改变章节调度结果：%s/%s/%s",
                        plan.research_id,
                        context.goal_id,
                        chapter_id,
                    )
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
                        "usage": self._plain(usage),
                        "research_usage": self._plain(research_usage),
                    },
                },
            )
            await context.on_event(event)

        if should_section(kind, task.output_format):
            try:
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
                    timer=self.timer,
                    now=self.now,
                    deadline_at=getattr(context, "deadline_at", None),
                    engine_timeout_seconds=getattr(adapter, "timeout_seconds", None),
                    persist_goal_evidence=self._persist_goal_evidence,
                )
            except asyncio.CancelledError:
                # 墙钟取消 / stop 打断落在节执行中：在跑节复位成 pending，不留
                # running 幽灵行（worklog 6b §九小缺陷 3）；父章终态由调度器按
                # 取消原因落账（timeout → missing/deferred，stop → 复位）。
                self._reset_running_sections(plan.research_id, context.goal_id)
                self._salvage_partial_sections(plan, agent, task)
                raise

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
        # 章终态判定的先后顺序（D-001 缺陷 A + D-005 + D-006，三条一起看才完整）：
        # 1) reason == tool_unavailable：章声明的源/工具不可达，**产物无论空不空都不构成
        #    契约履行** —— agent 自行改抓替代源写出的非空数组也一样 —— 一律 missing/
        #    tool_unavailable，不记 unmet、不烧重试（attempts=1）。替代源条目留在磁盘，
        #    但不进账本 done（D-006）。
        # 2) reason == empty_result + 产物有效条目为 0 → missing/empty_result。succeeded
        #    并不能替它把关：真实引擎里 partial + unmet 非空 + validators 全 PASS（空数组
        #    按 missing_reason 被接收）也会 succeeded=True（D-005）。
        # 3) reason == empty_result + 产物非空时才轮到 succeeded：产物合法 + partial +
        #    unmet 齐全 + 如实写 reason 的章判 done 并执行 C7 的 _record_unmet()，
        #    不能被 reason 短路成 missing（D-001 缺陷 A 的原始场景）。
        if reason == "tool_unavailable" or (
            reason == "empty_result" and self._artifact_is_empty(task)
        ):
            return TaskRunResult(
                False, engine=context.engine, chapter_status="missing", reason=reason,
                actual_output_path=actual_path, actual_count=actual_count,
                engine_error=engine_error, conclusion_error=conclusion_error,
            )
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

    def _salvage_partial_sections(self, plan: Plan, agent: Any, task: Any) -> None:
        """被取消的节化章只要有 done 节，就把父章产物组装落盘（D-009）。

        产物在不在盘上决定收尾判 completed 还是 failed（硬约束 4）；取消直接
        丢内容会让报告章中招时整卷归零（r-ca3a3f4eb587 实锤）。尽力而为：
        组装失败不掩盖取消本身。无终态的节按 missing/timeout 占位——只改传给
        组装器的行副本、不写账本；reason 必须落闭集，/stop 场景 resume 重派后
        会重新组装覆盖这份快照。
        """
        try:
            sections = section_specs(plan, agent)
            section_ids = {item["section_id"] for item in sections}
            rows = [dict(row) for row in self.store.list_chapters(plan.research_id)]
            has_done = any(
                row["goal_id"] == task.goal_id
                and row["chapter_id"] in section_ids
                and row["status"] == "done"
                for row in rows
            )
            if not has_done:
                return
            for row in rows:
                if (
                    row["goal_id"] == task.goal_id
                    and row["chapter_id"] in section_ids
                    and row["status"] not in {"done", "missing"}
                ):
                    row["status"] = "missing"
                    row["reason"] = row["reason"] or "timeout"
            assemble_sections(
                plan=plan,
                agent=agent,
                output_path=task.output_path,
                output_format=task.output_format,
                section_root=task.output_path.parent / Path(task.output_path.stem),
                sections=sections,
                rows=rows,
            )
        except Exception:
            # 抢救失败只损失部分产物，不改变取消路径的任何既有语义。
            return

    def _reset_running_sections(self, research_id: str, goal_id: str) -> None:
        for row in self.store.list_chapters(research_id):
            if (
                row["goal_id"] == goal_id
                and "/" in str(row["chapter_id"])
                and row["status"] == "running"
            ):
                self.store.reset_running_chapter(
                    research_id, goal_id, row["chapter_id"],
                    updated_at=self.now_iso(),
                )

    def _artifact_is_empty(self, task: Any) -> bool:
        """产物「有效条目为 0」——按 output 形态判，与 4a 的既有口径同源。

        文件缺失 / 空白正文一律算空（同 chapter_failure 的 empty_result 口径）；
        json 数组看 len==0，json object 看有没有键，且节化文档信封（§2.3.1）的
        必备键 `sections` 为空即空（裁决点 6：字段都在但零收获不算完成）；
        其余格式只要正文非空就不算空。
        与引擎无关，纯看落盘产物，所以放在 runtime 的章终态判定处。
        """
        path = getattr(task, "output_path", None)
        if path is None:
            return True
        try:
            if not path.is_file():
                return True
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return True
        if not text.strip():
            return True
        if getattr(task, "output_format", None) != "json":
            return False
        try:
            artifact = json.loads(text)
        except json.JSONDecodeError:
            return False
        if isinstance(artifact, list):
            return not artifact
        if isinstance(artifact, dict):
            if not artifact:
                return True
            return "sections" in artifact and not artifact["sections"]
        return False

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
            state["status_label"] = SCHEDULER_STATUS_LABELS.get(
                data["status"], data["status"]
            )
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

    def _build_scheduler(self, plan: Plan) -> Scheduler:
        scheduler: Scheduler
        scheduler = Scheduler(
            plan,
            lambda agent, context: self._run_task(scheduler.plan, agent, context),
            lambda event: self._emit_scheduler_event(plan.research_id, event),
            self.now,
            self.timer,
            chapter_ledger=self.store,
        )
        scheduler._before_goal_complete = (
            lambda goal: self._persist_goal_evidence(scheduler.plan, goal)
        )
        return scheduler

    def _claim_execution(self, research_id: str) -> bool:
        """认领一个研究的起跑权：**一个研究只能有一套执行器**。

        自动批准（`prepare_research`）与显式批准（`POST /plan/approve`）是两条独立的
        启动路径，谁都不知道对方存在；从前 `_schedulers[rid] = scheduler` 直接覆盖，
        被挤掉的那套没人注销、继续跑，同一章就被跑两遍（缺陷 D-021）。
        认领是**同步**完成的，跨 `await` 也抢不走：`_schedulers` 要等 Scheduler
        造好才登记，中间那段空窗由 `_starting` 顶着。
        """

        if research_id in self._starting or research_id in self._schedulers:
            return False
        self._starting.add(research_id)
        return True

    async def start_research(self, plan: Plan) -> None:
        if not self._claim_execution(plan.research_id):
            logger.warning(
                "研究已在运行，忽略重复起跑（不再起第二套执行器）：research_id=%s",
                plan.research_id,
            )
            return
        try:
            state = self.researches[plan.research_id]
            state["status"] = "running"
            state["status_label"] = "运行中"
            state["actions"] = self.running_actions(plan.research_id)
            state["progress"]["summary"] = "Scheduler 正在按计划推进"
            await self.events.publish(
                plan.research_id,
                {"type": "research_update", "data": state},
            )
            scheduler = self._build_scheduler(plan)
            self._schedulers[plan.research_id] = scheduler
            await scheduler.start()
            await self._drain_auto_tasks()
            await self._finalize_if_terminal(plan.research_id)
        finally:
            # 起跑成功后由 `_schedulers` 继续挡住重复起跑；中途炸了则把起跑权还回去。
            self._starting.discard(plan.research_id)

    async def rehydrate_running_researches(self) -> list[str]:
        """从报告与章账本重建可 resume 的运行态；启动时绝不驱动 Scheduler。"""

        restored: list[str] = []
        for report in self.store.list_running_reports():
            plan_snapshot = report.get("plan_snapshot")
            if not isinstance(plan_snapshot, dict):
                continue
            plan = Plan.from_dict(plan_snapshot)
            chapters = [
                {
                    "goal_id": goal.goal_id,
                    "chapter_id": Scheduler._chapter_id(agent),
                }
                for goal in plan.goals
                for agent in goal.agents
            ]
            self.store.ensure_chapters(
                plan.research_id,
                chapters,
                updated_at=self.now_iso(),
                reset_running=True,
            )
            state = self._state_from_plan(plan)
            self.researches[plan.research_id] = state
            self._adapters[plan.research_id] = self.adapter_factory()
            scheduler = self._build_scheduler(plan)
            self._schedulers[plan.research_id] = scheduler
            for goal_state in state["goals"]:
                for agent_state in goal_state["agents"]:
                    agent_state["status"] = scheduler.agent_statuses[agent_state["id"]]
            state["progress"] = {
                "done": sum(
                    bool(goal.agents) and all(
                        scheduler.agent_statuses[agent.agent_id] in {"done", "missing"}
                        for agent in goal.agents
                    )
                    for goal in plan.goals
                ),
                "total": len(plan.goals),
                "summary": "已从章节账本恢复，等待用户继续",
            }
            await scheduler.pause()
            state["actions"] = self.resume_actions(plan.research_id)
            restored.append(plan.research_id)
        return restored

    async def _persist_goal_evidence(self, plan: Plan, goal: Goal) -> None:
        """兼容/恢复投影：幂等写产物，四个内容字段不覆盖适配器真值。

        D-020：产物里 `platform` 越出七值闭集时（引擎把发布方名写进了平台列），
        投影层已把列收回闭集、原值留痕到 `extra.artifact_platform`；这里把这件事
        发成事件，让它**不是静默发生**的——两个调用点都已按 awaitable 处理。
        """

        payloads: list[dict[str, Any]] = []
        rows_by_chapter = {
            str(row["chapter_id"]): row
            for row in self.store.list_chapters(plan.research_id)
            if row["goal_id"] == goal.goal_id
        }
        salvaged: list[dict[str, str]] = []
        for agent in goal.agents:
            chapter = agent.chapter if isinstance(
                getattr(agent, "chapter", None), dict,
            ) else {}
            chapter_id = str(chapter.get("chapter_id") or agent.agent_id)
            if str(agent.output.get("format")) != "json":
                continue
            row = rows_by_chapter.get(chapter_id)
            status = str(row["status"]) if row is not None else ""
            if status not in _EVIDENCE_PROJECTABLE_STATUSES:
                continue
            sources = list(agent.capability.get("sources", []))
            platform_hint = str(sources[0]) if len(sources) == 1 else None
            path = self.runs_root / plan.research_id / str(agent.output["path"])
            items = load_evidence_payloads(
                path,
                report_id=plan.research_id,
                goal_id=goal.goal_id,
                agent_name=agent.agent_id,
                platform_hint=platform_hint,
            )
            if status != "done" and items:
                # §SRC-1 货 4：章超时/失败不再把已落盘的产物整章作废。
                # 第 6 轮 goal-1 四章全 timeout，盘上却躺着 20 条带 permalink 的
                # 网页搜索证据，一条都没进库——搜到了却当没搜过。
                # 这里只捡「文件仍能解析成合法 evidence」的那部分，并逐条留痕，
                # 让下游分得清它来自一个没跑完的章。
                reason = str(row["reason"] or "") if row is not None else ""
                for payload in items:
                    extra = payload.get("extra")
                    if not isinstance(extra, dict):
                        extra = {}
                        payload["extra"] = extra
                    extra[_INCOMPLETE_CHAPTER_KEY] = True
                    extra["incomplete_chapter_id"] = chapter_id
                    extra["incomplete_chapter_status"] = status
                    if reason:
                        extra["incomplete_chapter_reason"] = reason
                salvaged.append({
                    "chapter_id": chapter_id,
                    "status": status,
                    "reason": reason,
                    "count": str(len(items)),
                })
            payloads.extend(items)
        if not payloads:
            return
        existing = {
            str(item["permalink"]): item
            for item in self.store.list_evidence(plan.research_id)
        }
        content_fields = (
            "title", "content_excerpt", "author_name", "raw_metrics",
        )
        for payload in payloads:
            stored = existing.get(str(payload["permalink"]))
            if stored is None:
                continue
            for field in content_fields:
                value = stored.get(field)
                if value not in (None, "", {}):
                    payload[field] = value
        self.store.upsert_evidence_batch(payloads)
        if salvaged:
            total = sum(int(item["count"]) for item in salvaged)
            logger.warning(
                "未完成章的产物已捡回入库：research=%s goal=%s 章数=%d 条数=%d",
                plan.research_id, goal.goal_id, len(salvaged), total,
            )
            await self.events.publish(
                plan.research_id,
                {
                    "type": "evidence_salvaged_from_incomplete_chapter",
                    "data": {
                        "research_id": plan.research_id,
                        "goal_id": goal.goal_id,
                        "count": total,
                        "chapters": salvaged,
                    },
                },
            )
        downgraded = [
            {
                "permalink": str(payload["permalink"]),
                "artifact_platform": str(
                    payload["extra"][evidence_artifacts.ARTIFACT_PLATFORM_KEY]
                ),
                "platform": str(payload["platform"]),
            }
            for payload in payloads
            if isinstance(payload.get("extra"), dict)
            and payload["extra"].get(evidence_artifacts.ARTIFACT_PLATFORM_KEY)
        ]
        if downgraded:
            logger.warning(
                "产物 platform 越出闭集，已降级并留痕：research=%s goal=%s 条数=%d",
                plan.research_id, goal.goal_id, len(downgraded),
            )
            await self.events.publish(
                plan.research_id,
                {
                    "type": "evidence_platform_downgraded",
                    "data": {
                        "research_id": plan.research_id,
                        "goal_id": goal.goal_id,
                        "count": len(downgraded),
                        "vocabulary": sorted(
                            evidence_artifacts.PLATFORM_VOCABULARY
                        ),
                        "items": downgraded,
                    },
                },
            )

    async def respond_card(self, card_id: str, *, action: str, payload: dict[str, Any]) -> Card:
        card = self.cards[card_id]
        if card.status is not CardStatus.PENDING:
            # D-013 货 1：重复回复幂等成功。这里检查的是 runtime 自己那份 Card **副本**
            # （`_emit_scheduler_event` 里 `_external_card` 重建的），scheduler 才是权威，
            # 副本落后一拍时第二路调用照样能过这道检查。抛出去只有两种下场：
            # 走后台任务就被吞（goal 死等），走 API 就给用户一句「卡片仍保留，可直接重试」——
            # 而卡片其实已经答过了。故一律幂等返回已解析的卡片。
            logger.info(
                "卡片已处理，忽略重复回复：card_id=%s status=%s action=%s",
                card_id,
                card.status.value,
                action,
            )
            return card
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
        await scheduler.resume(wait=False)
        task = asyncio.create_task(
            self._finalize_after_drive(research_id, scheduler),
            name=f"owli:resume-finalize:{research_id}",
        )
        self._drive_watchers.add(task)
        task.add_done_callback(self._drive_watchers.discard)
        guard_task(task, logger=logger, context="resume 收尾")

    async def _finalize_after_drive(self, research_id: str, scheduler: Any) -> None:
        """后台驱动跑完（含自动干预派生的续跑）后收尾，`/resume` 不必阻塞到整轮结束。"""
        for _ in range(20):
            await scheduler.wait_idle()
            await self._drain_auto_tasks()
            if not scheduler.drive_pending:
                break
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

    def _completed_agent_tags(self, plan: Plan) -> list[str] | None:
        """只读取账本已完成的 tagging 章产物，不在运行期生成标签。"""

        completed = {
            (row["goal_id"], row["chapter_id"])
            for row in self.store.list_chapters(plan.research_id)
            if row["status"] == "done"
        }
        latest: list[str] | None = None
        for goal in plan.goals:
            for agent in goal.agents:
                if self._agent_kind(agent) != "tagging":
                    continue
                chapter = agent.chapter if isinstance(agent.chapter, dict) else {}
                chapter_id = str(chapter.get("chapter_id") or agent.agent_id)
                if (goal.goal_id, chapter_id) not in completed:
                    continue
                path = self.runs_root / plan.research_id / str(agent.output["path"])
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                    raise ValueError(
                        f"已完成 tagging 章产物不可读取：{goal.goal_id}/{chapter_id}"
                    ) from exc
                if not isinstance(value, list) or not all(
                    isinstance(item, str) for item in value
                ):
                    raise ValueError(
                        f"已完成 tagging 章产物必须是字符串数组："
                        f"{goal.goal_id}/{chapter_id}"
                    )
                latest = list(value)
        return latest

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

    def _claim_documents(self, plan: Plan) -> list[dict[str, Any]]:
        """只读计划声明的 JSON 报告章，不扫描正文或其他 JSON 产物。"""

        research_root = (self.runs_root / plan.research_id).resolve(strict=False)
        documents: list[dict[str, Any]] = []
        seen: set[Path] = set()
        for goal in plan.goals:
            for agent in goal.agents:
                if (
                    self._agent_kind(agent) not in SECTIONED_CHAPTER_KINDS
                    or str(agent.output.get("format")) != "json"
                ):
                    continue
                path = (research_root / str(agent.output.get("path", ""))).resolve(
                    strict=False
                )
                if path in seen:
                    continue
                seen.add(path)
                if not path.is_relative_to(research_root):
                    raise ValueError(f"断言章产物路径越界：{path}")
                if not path.is_file():
                    continue
                document = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(document, dict):
                    documents.append(document)
        return documents

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
        # 兼容升级前已经越过 goal 闸门、但原始采集项尚未投影入库的运行。
        # upsert 使用稳定身份键，因此终态补扫与逐 goal 写入可以安全并存。
        for goal in plan.goals:
            await self._persist_goal_evidence(plan, goal)
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
        citation_error: str | None = None
        claims_error: str | None = None
        claims_offenders: list[str] = []
        if validation_report.verdict is validation.Verdict.PASS:
            try:
                self.store.replace_evidence_citations(
                    research_id,
                    source_citations(report_path.read_text(encoding="utf-8")),
                )
            except (KeyError, TypeError, ValueError) as exc:
                citation_error = f"成稿角标回填失败：{exc}"
        if (
            validation_report.verdict is validation.Verdict.PASS
            and citation_error is None
        ):
            try:
                documents = self._claim_documents(plan)
                if any("claims" in document for document in documents):
                    register_claims(
                        self.store,
                        research_id,
                        claims_from_documents(documents),
                        source="chapter",
                    )
            except ClaimsRegistrationError as exc:
                claims_error = str(exc)
                claims_offenders = exc.offenders
            except (json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
                claims_error = f"断言登记失败：{type(exc).__name__}: {exc}"
        failures = [
            {"validator": item.name, "message": item.message, "offenders": item.offenders}
            for item in validation_report.results
            if item.verdict is not validation.Verdict.PASS
        ]
        if citation_error is not None:
            failures.append({
                "validator": "evidence_citation_backfill",
                "message": citation_error,
                "offenders": [],
            })
        if claims_error is not None:
            failures.append({
                "validator": "claims_registration",
                "message": claims_error,
                "offenders": claims_offenders,
            })
        await self.events.publish(
            research_id,
            {
                "type": "report_validation",
                "data": {
                    "verdict": (
                        validation.Verdict.FAIL.value
                        if citation_error is not None or claims_error is not None
                        else validation_report.verdict.value
                    ),
                    "validators": report_validators,
                    "failures": failures,
                },
            },
        )
        validation_failed = (
            validation_report.verdict is not validation.Verdict.PASS
            or citation_error is not None
            or claims_error is not None
        )
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
        finish_payload = {
            "status": report_status,
            "completed_at": self.now_iso(),
            "summary": "计划执行完成，报告已生成" if not report_missing else "报告未生成",
            "summary_line": (
                "报告未生成" if report_missing
                else "部分 goal 失败" if unfinished_goals
                else "全部 goal 已完成"
            ),
            "report_path": stored_path,
        }
        agent_tags = self._completed_agent_tags(plan)
        try:
            self.store.finish_report(
                research_id, **finish_payload, agent_tags=agent_tags,
            )
        except (TypeError, ValueError) as exc:
            if agent_tags is None:
                raise
            logger.warning("tagging 产物不合规，忽略标签继续收尾：%s", exc)
            await self.events.publish(
                research_id,
                {
                    "type": "report_tagging_warning",
                    "data": {
                        "research_id": research_id,
                        "reason": "invalid_agent_tags",
                        "message": str(exc),
                    },
                },
            )
            self.store.finish_report(
                research_id, **finish_payload, agent_tags=None,
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
