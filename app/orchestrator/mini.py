"""M0 最小编排器：固定单 goal 的关键词、采集、报告真实链路。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Awaitable, Callable

from app.adapters import validation
from app.adapters.claude import ClaudeAdapter, ClaudeRunResult, ClaudeTask
from app.sources.hn import search as search_hn


GOAL_ID = "goal-1"
TOTAL_STEPS = 4
MAX_ATTEMPTS = 3
MAX_CITATIONS = 99
MIN_EVIDENCE = 10
REPORT_VALIDATORS = [
    "file_exists",
    "sections_exist:结论,信息源",
    "citation_marks_resolvable",
    "no_orphan_citation",
    "db_row_exists:evidence",
]


class StepVerdict(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class StepAttempt:
    verdict: StepVerdict
    message: str
    raw: Any = None


@dataclass(frozen=True)
class RetryResult:
    attempt: StepAttempt
    attempts: int


async def run_step_with_retry(
    operation: Callable[[int], Awaitable[StepAttempt]],
    on_retry: Callable[[int, StepAttempt], Awaitable[None]],
    *,
    max_attempts: int = MAX_ATTEMPTS,
) -> RetryResult:
    """只重试 FAIL；UNAVAILABLE 立即返回且不消耗后续尝试。"""
    for attempt_number in range(1, max_attempts + 1):
        result = await operation(attempt_number)
        if result.verdict is not StepVerdict.FAIL:
            return RetryResult(result, attempt_number)
        if attempt_number < max_attempts:
            await on_retry(attempt_number + 1, result)
    return RetryResult(result, max_attempts)


def build_actions(research_id: str) -> list[dict[str, Any]]:
    return [
        {
            "id": "pause",
            "label": "暂停",
            "method": "POST",
            "href": f"/api/researches/{research_id}/pause",
        },
        {
            "id": "stop",
            "label": "终止",
            "danger": True,
            "confirm": "终止本次调研？已入库证据与产物会保留。",
            "method": "POST",
            "href": f"/api/researches/{research_id}/stop",
        },
    ]


def build_initial_state(research_id: str, query: str) -> dict[str, Any]:
    """真实 M0 单 goal 快照，不含演示 goal 或假进度。"""
    return {
        "research_id": research_id,
        "title": query,
        "status": "running",
        "status_label": "运行中",
        "progress": {"done": 0, "total": TOTAL_STEPS, "summary": "等待关键词提取"},
        "actions": build_actions(research_id),
        "goals": [
            {
                "id": GOAL_ID,
                "title": "Hacker News 竞品证据与报告",
                "status": "queued",
                "summary": "等待关键词提取",
                "agents": [
                    {
                        "id": "keyword-extractor",
                        "name": "英文关键词提取",
                        "engine": "Claude",
                        "status": "queued",
                        "activity": "等待执行",
                    },
                    {
                        "id": "hn-collector",
                        "name": "Hacker News 采集",
                        "engine": "Owli",
                        "status": "queued",
                        "activity": "等待关键词",
                    },
                    {
                        "id": "report-writer",
                        "name": "Markdown 报告成稿",
                        "engine": "Claude",
                        "status": "queued",
                        "activity": "等待证据入库",
                    },
                ],
            }
        ],
        "cards": [],
        "events": [],
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _plain_raw(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return value
    if isinstance(value, (list, tuple)):
        return [_plain_raw(item) for item in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump()
    if is_dataclass(value):
        return asdict(value)
    attributes = getattr(value, "__dict__", None)
    if isinstance(attributes, dict):
        return {key: _plain_raw(item) for key, item in attributes.items()}
    return {"type": type(value).__name__, "text": str(value)}


def _adapter_raw(result: ClaudeRunResult) -> Any:
    for event in reversed(result.events):
        if event.kind == "error":
            return _plain_raw(event.raw)
    return {
        "engine_error": result.engine_error,
        "conclusion_error": result.conclusion_error,
        "validation": [
            {
                "name": item.name,
                "verdict": item.verdict.value,
                "message": item.message,
                "detail": item.detail,
            }
            for item in result.validation.results
        ],
    }


def _classify_adapter_result(result: ClaudeRunResult) -> StepAttempt:
    if (
        result.engine_error is not None
        or result.validation.verdict is validation.Verdict.UNAVAILABLE
    ):
        return StepAttempt(
            StepVerdict.UNAVAILABLE,
            result.engine_error or "产物校验器不可用",
            _adapter_raw(result),
        )
    if not result.succeeded:
        messages = [item.message for item in result.validation.failures]
        if result.conclusion_error:
            messages.append(result.conclusion_error)
        if result.conclusion is not None and result.conclusion.status != "done":
            messages.extend(result.conclusion.unmet or [f"结论状态为 {result.conclusion.status}"])
        return StepAttempt(
            StepVerdict.FAIL,
            "；".join(messages) or "产物与结构化结论未同时通过",
            _adapter_raw(result),
        )
    return StepAttempt(StepVerdict.PASS, result.conclusion.summary)


class MiniOrchestrator:
    """固定执行 M0 的三个 agent 卡步骤与最终收口校验。"""

    def __init__(
        self,
        *,
        research_id: str,
        query: str,
        store: Any,
        event_buffer: Any,
        state: dict[str, Any],
        adapter: Any | None = None,
        source_search: Callable[[str, str], list[dict[str, Any]]] = search_hn,
        runs_root: Path = validation.RUNS_ROOT,
    ) -> None:
        self.research_id = research_id
        self.query = query
        self.store = store
        self.events = event_buffer
        self.state = state
        self.adapter = adapter or ClaudeAdapter()
        self.source_search = source_search
        self.goal_root = runs_root / research_id / "goals" / GOAL_ID
        self.keywords_path = self.goal_root / "keywords.json"
        self.evidence_path = self.goal_root / "evidence.json"
        self.report_path = self.goal_root / "report.md"
        self._report_created = False

    @property
    def goal(self) -> dict[str, Any]:
        return self.state["goals"][0]

    def _agent(self, agent_id: str) -> dict[str, Any]:
        return next(item for item in self.goal["agents"] if item["id"] == agent_id)

    def _ctx(self, path: Path, output_format: str, agent_id: str) -> validation.Ctx:
        cache: dict[str, Any] = {}

        def read_text() -> str:
            if "text" not in cache:
                cache["text"] = path.read_text(encoding="utf-8")
            return cache["text"]

        def read_json() -> Any:
            if "json" not in cache:
                cache["json"] = json.loads(read_text())
            return cache["json"]

        return validation.Ctx(
            output_path=path,
            output_format=output_format,
            research_id=self.research_id,
            goal_id=GOAL_ID,
            agent_id=agent_id,
            read_text=read_text,
            read_json=read_json,
            store=self.store,
            source_domains=frozenset({"news.ycombinator.com"}),
        )

    async def _publish_state(self) -> None:
        await self.events.publish(
            self.research_id,
            {
                "type": "research_update",
                "data": {
                    "status": self.state["status"],
                    "status_label": self.state["status_label"],
                    "actions": self.state["actions"],
                    "goals": self.state["goals"],
                },
            },
        )

    async def _publish_progress(self) -> None:
        await self.events.publish(
            self.research_id,
            {"type": "progress", "data": dict(self.state["progress"])},
        )

    async def _agent_update(
        self,
        agent_id: str,
        status: str,
        activity: str,
        *,
        attempt: int | None = None,
        phase: str | None = None,
    ) -> None:
        agent = self._agent(agent_id)
        agent["status"] = status
        agent["activity"] = activity
        data = {
            "goal_id": GOAL_ID,
            "agent_id": agent_id,
            "engine": agent["engine"],
            "status": status,
            "activity": activity,
        }
        if attempt is not None:
            data["attempt"] = attempt
        if phase is not None:
            data["phase"] = phase
        await self.events.publish(
            self.research_id, {"type": "agent_update", "data": data}
        )

    async def _start_step(
        self, agent_id: str, summary: str, *, phase: str | None = None
    ) -> None:
        self.goal["status"] = "running"
        self.goal["summary"] = summary
        self.state["progress"]["summary"] = summary
        await self._agent_update(
            agent_id, "running", summary, attempt=1, phase=phase
        )
        await self._publish_state()
        await self._publish_progress()

    async def _retry_step(
        self,
        agent_id: str,
        next_attempt: int,
        result: StepAttempt,
        *,
        phase: str | None = None,
    ) -> None:
        summary = f"第 {next_attempt} 次尝试：{result.message}"
        self.goal["summary"] = summary
        self.state["progress"]["summary"] = summary
        await self._agent_update(
            agent_id,
            "retrying",
            summary,
            attempt=next_attempt,
            phase=phase,
        )
        await self._publish_state()
        await self._publish_progress()

    async def _finish_step(
        self,
        agent_id: str,
        summary: str,
        artifact: Path | None,
        *,
        phase: str | None = None,
    ) -> None:
        await self._agent_update(agent_id, "done", summary, phase=phase)
        self.state["progress"]["done"] += 1
        self.state["progress"]["summary"] = summary
        self.goal["summary"] = summary
        await self._publish_state()
        await self._publish_progress()
        if artifact is not None:
            await self.events.publish(
                self.research_id,
                {
                    "type": "artifact",
                    "data": {
                        "goal_id": GOAL_ID,
                        "agent_id": agent_id,
                        "path": str(artifact),
                        "summary": summary,
                    },
                },
            )

    async def _terminal(
        self,
        agent_id: str,
        result: StepAttempt,
        *,
        phase: str | None = None,
    ) -> None:
        unavailable = result.verdict is StepVerdict.UNAVAILABLE
        self.state["status"] = "unavailable" if unavailable else "failed"
        self.state["status_label"] = "引擎不可用" if unavailable else "执行失败"
        self.state["actions"] = []
        self.goal["status"] = "failed"
        self.goal["summary"] = result.message
        self.state["progress"]["summary"] = result.message
        await self._agent_update(
            agent_id, "failed", result.message, phase=phase
        )
        raw = _plain_raw(result.raw)
        if self._report_created:
            try:
                self.store.finish_report(
                    self.research_id,
                    status="failed",
                    completed_at=_utc_now(),
                )
            except Exception as exc:
                storage_error = {
                    "exception": type(exc).__name__,
                    "message": str(exc),
                }
                raw = {
                    "original": raw,
                    "storage_finalize_error": storage_error,
                }
        await self.events.publish(
            self.research_id,
            {
                "type": "error",
                "raw": raw,
                "data": {
                    "goal_id": GOAL_ID,
                    "agent_id": agent_id,
                    "status": self.state["status"],
                    "summary": result.message,
                    **({"phase": phase} if phase is not None else {}),
                },
            },
        )
        await self._publish_progress()
        await self._publish_state()

    async def _run_engine_task(self, task: ClaudeTask) -> StepAttempt:
        ctx = self._ctx(task.output_path, task.output_format, task.agent_id)

        async def on_event(event: Any) -> None:
            if event.kind == "error":
                await self.events.publish(
                    self.research_id,
                    {
                        "type": "error",
                        "raw": _plain_raw(event.raw),
                        "data": {
                            "goal_id": GOAL_ID,
                            "agent_id": task.agent_id,
                            "summary": event.text,
                        },
                    },
                )

        try:
            run_result = await self.adapter.run(task, ctx, on_event=on_event)
        except Exception as exc:
            return StepAttempt(
                StepVerdict.UNAVAILABLE,
                f"引擎调用不可用：{type(exc).__name__}: {exc}",
                {"exception": type(exc).__name__, "message": str(exc)},
            )
        return _classify_adapter_result(run_result)

    async def _keywords_attempt(self, attempt_number: int) -> StepAttempt:
        del attempt_number
        task = ClaudeTask(
            body=(
                f"用户调研需求：{self.query}\n\n"
                "从该中文需求提取 3–6 个适合 Hacker News 近 90 天检索的宽泛英文检索词。"
                "每项只能是 1–2 个英文单词；优先独立产品名和品类词；禁止加入 vs、review、"
                "pros、cons、competitor 等比较修饰词。若需求涉及飞书，数组必须包含"
                " Lark、Slack、Teams、Notion 四个独立词。只输出 JSON 字符串数组，"
                "不要对象，不要 Markdown。\n"
                f"把数组写入：{self.keywords_path}\n"
                "验收：文件存在且非空；JSON 顶层为字符串数组；至少 3 项。\n"
                "最终 owli-result 结论块的 summary 固定填写『英文检索词已写入』，"
                "不要添加其他文字。"
            ),
            output_path=self.keywords_path,
            output_format="json",
            research_id=self.research_id,
            goal_id=GOAL_ID,
            agent_id="keyword-extractor",
            validators=["file_exists", "json_array_min_items:3"],
            tools=frozenset({"Write"}),
        )
        result = await self._run_engine_task(task)
        if result.verdict is not StepVerdict.PASS:
            return result
        keywords, keyword_error = self._read_keywords()
        if keyword_error is not None:
            return keyword_error
        return StepAttempt(
            StepVerdict.PASS,
            f"已生成 {len(keywords)} 个英文检索词",
        )

    def _read_keywords(self) -> tuple[list[str], StepAttempt | None]:
        try:
            value = json.loads(self.keywords_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError, TypeError) as exc:
            return [], StepAttempt(
                StepVerdict.FAIL,
                f"关键词产物不是合法 JSON：{type(exc).__name__}: {exc}",
                {"exception": type(exc).__name__, "message": str(exc)},
            )
        except OSError as exc:
            return [], StepAttempt(
                StepVerdict.UNAVAILABLE,
                f"关键词产物无法读取：{type(exc).__name__}: {exc}",
                {"exception": type(exc).__name__, "message": str(exc)},
            )
        if not isinstance(value, list) or not 3 <= len(value) <= 6:
            actual = len(value) if isinstance(value, list) else type(value).__name__
            return [], StepAttempt(
                StepVerdict.FAIL,
                f"关键词必须是 3–6 项字符串数组，实际：{actual}",
                {"actual": actual},
            )
        invalid = [
            {"index": index, "value": item}
            for index, item in enumerate(value)
            if not isinstance(item, str)
            or not re.fullmatch(r"[A-Za-z0-9.+#-]+(?:\s+[A-Za-z0-9.+#-]+)?", item.strip())
        ]
        if invalid:
            return [], StepAttempt(
                StepVerdict.FAIL,
                "关键词每项必须是 1–2 个英文检索词",
                {"invalid_items": invalid},
            )
        return [item.strip() for item in value], None

    async def _collect_attempt(self, attempt_number: int) -> StepAttempt:
        del attempt_number
        keywords, keyword_error = self._read_keywords()
        if keyword_error is not None:
            return keyword_error
        try:
            batches = await asyncio.gather(
                *(
                    asyncio.to_thread(self.source_search, keyword, "90d")
                    for keyword in keywords
                )
            )
        except Exception as exc:
            return StepAttempt(
                StepVerdict.UNAVAILABLE,
                f"Hacker News 采集不可用：{type(exc).__name__}: {exc}",
                {"exception": type(exc).__name__, "message": str(exc)},
            )

        selected: list[dict[str, Any]] = []
        seen_permalinks: set[str] = set()
        longest_batch = max((len(batch) for batch in batches), default=0)
        for row_index in range(longest_batch):
            for batch in batches:
                if row_index >= len(batch):
                    continue
                item = batch[row_index]
                permalink = item.get("permalink")
                if not permalink or permalink in seen_permalinks:
                    continue
                seen_permalinks.add(permalink)
                selected.append(item)
                if len(selected) == MAX_CITATIONS:
                    break
            if len(selected) == MAX_CITATIONS:
                break
        if len(selected) < MIN_EVIDENCE:
            return StepAttempt(
                StepVerdict.FAIL,
                f"Hacker News 证据不足：至少 {MIN_EVIDENCE} 条，实际 {len(selected)} 条",
                {
                    "keywords": keywords,
                    "minimum": MIN_EVIDENCE,
                    "actual": len(selected),
                },
            )

        exported = []
        try:
            for citation_no, item in enumerate(selected, 1):
                evidence_id = "ev-" + hashlib.sha256(
                    f"{self.research_id}\0{item['permalink']}".encode("utf-8")
                ).hexdigest()[:20]
                self.store.add_evidence(
                    id=evidence_id,
                    report_id=self.research_id,
                    goal_id=GOAL_ID,
                    agent_name="hn-collector",
                    platform=item["platform"],
                    source_type=item.get("source_type", "post"),
                    platform_item_id=item.get("platform_item_id"),
                    permalink=item["permalink"],
                    title=item.get("title"),
                    content_excerpt=item.get("content_excerpt"),
                    author_name=item.get("author_name"),
                    source_keyword=item.get("source_keyword"),
                    fetch_method=item.get("fetch_method", "official_api"),
                    published_at=item.get("published_at"),
                    fetched_at=item["fetched_at"],
                    raw_metrics=item.get("raw_metrics", {}),
                    score_authority=item.get("score_authority"),
                    score_freshness=item.get("score_freshness"),
                    score_crossref=item.get("score_crossref"),
                    score_completeness=item.get("score_completeness"),
                    score_independence=item.get("score_independence"),
                    rated_by=item.get("rated_by", "baseline"),
                    citation_no=citation_no,
                    extra={},
                )
                exported.append(
                    {
                        **item,
                        "evidence_id": evidence_id,
                        "citation_no": citation_no,
                        "citation_id": f"S{citation_no:02d}",
                    }
                )
            self.evidence_path.write_text(
                json.dumps(exported, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            return StepAttempt(
                StepVerdict.UNAVAILABLE,
                f"证据入库或导出不可用：{type(exc).__name__}: {exc}",
                {"exception": type(exc).__name__, "message": str(exc)},
            )
        return StepAttempt(StepVerdict.PASS, f"已去重入库 {len(exported)} 条 HN 证据")

    async def _report_attempt(self, attempt_number: int) -> StepAttempt:
        del attempt_number
        task = ClaudeTask(
            body=(
                f"用户调研需求：{self.query}\n\n"
                f"唯一可读信息源是 {self.evidence_path}。禁止出网，禁止重新采集，"
                "禁止凭训练知识补事实。\n"
                f"把 Markdown 报告写入：{self.report_path}\n\n"
                "M0 本步的完成标准是忠实总结 evidence.json 中现有的 HN 社区证据，"
                "不要求补齐国内平台、价格、市占率、SLA 或 evidence.json 未覆盖的竞品。"
                "证据范围的局限写入『假设与不确定性』，不列入 owli-result.unmet。"
                "只要下列结构与角标验收全部满足且文件已落盘，owli-result.status 必须填写 done，"
                "unmet 必须填写空数组。\n"
                "报告必须包含标题为『结论』和『信息源』的两个非空章节。"
                "『结论』中的每条结论必须写成 Markdown 列表项，"
                "每个叶子列表项至少带一个 [Sxx] 角标。"
                "『信息源』只列正文实际引用的条目，每条格式必须是 "
                "- [Sxx] [标题](permalink)，permalink 必须从 evidence.json 原样复制并可点击。\n"
                "双向角标判定：citation_marks_resolvable 要求正文每个 [Sxx] 都能在信息源章节找到；"
                "no_orphan_citation 要求信息源章节每个 [Sxx] 都至少被正文引用一次。"
                "两组角标集合必须完全一致。\n"
                "最终 owli-result 结论块的 summary 固定填写『报告已写入并完成自检』，"
                "不要添加其他文字。"
            ),
            output_path=self.report_path,
            output_format="markdown",
            research_id=self.research_id,
            goal_id=GOAL_ID,
            agent_id="report-writer",
            validators=REPORT_VALIDATORS,
            tools=frozenset({"Read", "Write"}),
        )
        result = await self._run_engine_task(task)
        if result.verdict is not StepVerdict.PASS:
            return result
        final_report = validation.validate(
            self._ctx(self.report_path, "markdown", "report-writer"),
            REPORT_VALIDATORS,
        )
        if final_report.verdict is validation.Verdict.UNAVAILABLE:
            return StepAttempt(
                StepVerdict.UNAVAILABLE,
                "最终产物校验不可用",
                [item.detail for item in final_report.unavailable],
            )
        if final_report.verdict is validation.Verdict.FAIL:
            return StepAttempt(
                StepVerdict.FAIL,
                "；".join(item.message for item in final_report.failures),
                [item.detail for item in final_report.failures],
            )
        return StepAttempt(StepVerdict.PASS, "报告与双向角标校验通过")

    async def _finalize_attempt(self, attempt_number: int) -> StepAttempt:
        del attempt_number
        final_report = validation.validate(
            self._ctx(self.report_path, "markdown", "report-writer"),
            REPORT_VALIDATORS,
        )
        if final_report.verdict is validation.Verdict.UNAVAILABLE:
            return StepAttempt(
                StepVerdict.UNAVAILABLE,
                "最终产物校验不可用",
                [
                    {
                        "name": item.name,
                        "message": item.message,
                        "detail": item.detail,
                    }
                    for item in final_report.unavailable
                ],
            )
        if final_report.verdict is validation.Verdict.FAIL:
            return StepAttempt(
                StepVerdict.FAIL,
                "；".join(item.message for item in final_report.failures),
                [
                    {
                        "name": item.name,
                        "message": item.message,
                        "detail": item.detail,
                    }
                    for item in final_report.failures
                ],
            )

        contract_path = f"runs/{self.research_id}/goals/{GOAL_ID}/report.md"
        try:
            self.store.finish_report(
                self.research_id,
                status="completed",
                completed_at=_utc_now(),
                summary="M0 Hacker News 证据报告已生成并通过产物校验",
                summary_line=self.query,
                report_path=contract_path,
            )
        except Exception as exc:
            return StepAttempt(
                StepVerdict.UNAVAILABLE,
                f"报告收尾入库不可用：{type(exc).__name__}: {exc}",
                {"exception": type(exc).__name__, "message": str(exc)},
            )
        return StepAttempt(StepVerdict.PASS, "产物校验通过并完成报告入库")

    async def _execute_step(
        self,
        agent_id: str,
        start_summary: str,
        operation: Callable[[int], Awaitable[StepAttempt]],
        artifact: Path | None,
        *,
        phase: str | None = None,
    ) -> bool:
        await self._start_step(agent_id, start_summary, phase=phase)
        result = await run_step_with_retry(
            operation,
            lambda next_attempt, failure: self._retry_step(
                agent_id, next_attempt, failure, phase=phase
            ),
        )
        if result.attempt.verdict is StepVerdict.PASS:
            await self._finish_step(
                agent_id, result.attempt.message, artifact, phase=phase
            )
            return True
        await self._terminal(agent_id, result.attempt, phase=phase)
        return False

    async def run(self) -> None:
        """执行真实 M0 链路；内部异常归一成不可用终态。"""
        try:
            self.goal_root.mkdir(parents=True, exist_ok=True)
            self.store.create_report(
                id=self.research_id,
                title=self.query,
                research_question=self.query,
                created_at=_utc_now(),
                status="running",
                plan_snapshot={"goals": self.state["goals"]},
                extra={"m0_goal_id": GOAL_ID},
            )
            self._report_created = True
        except Exception as exc:
            await self._terminal(
                "keyword-extractor",
                StepAttempt(
                    StepVerdict.UNAVAILABLE,
                    f"初始化报告存储不可用：{type(exc).__name__}: {exc}",
                    {"exception": type(exc).__name__, "message": str(exc)},
                ),
            )
            return

        if not await self._execute_step(
            "keyword-extractor", "正在提取英文检索词", self._keywords_attempt, self.keywords_path
        ):
            return
        if not await self._execute_step(
            "hn-collector", "正在采集 Hacker News 近 90 天证据", self._collect_attempt, self.evidence_path
        ):
            return
        if not await self._execute_step(
            "report-writer", "正在基于已入库证据撰写报告", self._report_attempt, self.report_path
        ):
            return
        if not await self._execute_step(
            "report-writer",
            "正在执行最终产物校验与报告入库",
            self._finalize_attempt,
            None,
            phase="validation",
        ):
            return

        contract_path = f"runs/{self.research_id}/goals/{GOAL_ID}/report.md"
        self.state["status"] = "completed"
        self.state["status_label"] = "已完成"
        self.state["actions"] = []
        self.goal["status"] = "done"
        self.goal["summary"] = "4 个步骤全部完成 · 产物 report.md"
        self.state["progress"]["summary"] = "报告已生成并通过全部校验"
        await self.events.publish(
            self.research_id,
            {
                "type": "research_update",
                "data": {
                    "status": "completed",
                    "status_label": "已完成",
                    "actions": [],
                    "goals": self.state["goals"],
                    "report_path": contract_path,
                    "summary": "调研完成，报告可读",
                },
            },
        )
