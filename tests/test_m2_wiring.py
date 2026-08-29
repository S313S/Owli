from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator

import httpx

from app.store.evidence_artifacts import load_evidence_payloads
from tests.plan_factory import chapter_slots


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "app" / "store" / "schema.sql"


def async_test(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return wrapper


def _skeleton() -> dict[str, Any]:
    return {
        "market_profile": "global_product",
        "market_profile_justification": "产品面向全球市场。",
        "subjects": ["飞书"],
        "subjects_justification": "研究主体为飞书。",
        "goals": [
            {
                "title": "采集竞品证据",
                "objective": "形成可复核的竞品证据集合。",
                "depends_on": [],
                "deliverable": {
                    "format": "json",
                    "shape": "array",
                    "path": "evidence.json",
                    "description": "带永久链接的证据数组。",
                },
                "acceptance": ["文件存在且至少包含 1 条 permalink 记录"],
                "agents": [{"name": "HN 数据抓取·飞书", "task": "采集 Hacker News 证据",
                            "output": {"shape": "array"}}],
            },
            {
                "title": "审计证据可靠度",
                "objective": "形成可复核的证据评级结果。",
                "depends_on": ["goal-1"],
                "deliverable": {
                    "format": "json",
                    "shape": "array",
                    "path": "audit.json",
                    "description": "包含评级结论的结构化数据。",
                },
                "acceptance": ["文件存在且每条记录包含 5 个评分字段"],
                "agents": [{"name": "可靠度审计", "task": "审计证据可靠度",
                            "output": {"shape": "array"}}],
            },
            {
                "title": "撰写调研报告",
                "objective": "形成带证据角标和决策注释的报告。",
                "depends_on": ["goal-2"],
                "deliverable": {
                    "format": "markdown",
                    "shape": "object",
                    "path": "report.md",
                    "description": "包含结论与信息源双章的 Markdown 报告。",
                },
                "acceptance": ["文件存在且包含结论、信息源 2 个章节"],
                "agents": [{"name": "报告撰写", "task": "撰写最终 Markdown 报告",
                            "output": {"shape": "object"}}],
            },
        ]
    }


class RecordingEngine:
    def __init__(self, *, skeleton: dict[str, Any] | None = None) -> None:
        self.tasks: list[Any] = []
        self.skeleton = skeleton or _skeleton()
        self.block_kind: str | None = None
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.backoff_first_collection = False
        self._backoff_sent = False
        self.fail_kind: str | None = None
        self.chapter_outputs: list[str] = []

    async def run(self, task, ctx, on_event=None):
        del ctx
        self.tasks.append(task)
        if task.agent_kind == "planning":
            task.output_path.parent.mkdir(parents=True, exist_ok=True)
            if task.output_path.name == "skeleton.json":
                payload = {
                    "market_profile": self.skeleton["market_profile"],
                    "market_profile_justification": self.skeleton[
                        "market_profile_justification"
                    ],
                    "subjects": self.skeleton["subjects"],
                    "subjects_justification": self.skeleton[
                        "subjects_justification"
                    ],
                    "goals": [
                        {
                            "title": goal["title"],
                            "objective": goal["objective"],
                            "depends_on": goal["depends_on"],
                        }
                        for goal in self.skeleton["goals"]
                    ]
                }
            elif "-ch-" not in task.output_path.stem:
                number = int(task.output_path.stem.removeprefix("goal-"))
                goal = self.skeleton["goals"][number - 1]
                payload = {
                    "deliverable": goal["deliverable"],
                    "acceptance": goal["acceptance"],
                    "agents": goal["agents"],
                }
            else:
                goal_text, chapter_text = task.output_path.stem.split("-ch-", 1)
                goal_number = int(goal_text.removeprefix("goal-"))
                raw_agent = chapter_slots(
                    self.skeleton["goals"][goal_number - 1]["agents"]
                )[int(chapter_text) - 1]
                name = str(raw_agent["name"])
                chapter_type = (
                    "collection" if "数据抓取" in name
                    else "report" if "报告" in name
                    else "audit"
                )
                output_tail = task.body.partition("系统声明 output.path=")[2]
                output_path = json.JSONDecoder().raw_decode(output_tail)[0]
                payload = {
                    "chapter_type": chapter_type,
                    "opening": {
                        "inputs": [], "task": raw_agent["task"],
                        "acceptance": ["产物按声明路径落盘"],
                    },
                    "closing": {
                        "output": {"path": output_path},
                        "entities": ["飞书"] if chapter_type == "collection" else [],
                        "expected_count": 1, "notes": {},
                    },
                }
                if chapter_type == "collection":
                    self.chapter_outputs.append(output_path)
            task.output_path.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
        else:
            if task.agent_kind == self.block_kind:
                self.started.set()
                await self.release.wait()
            if self.backoff_first_collection and not self._backoff_sent and task.agent_kind == "data_collection":
                from app.adapters.events import ItemKind, NormalizedEvent

                self._backoff_sent = True
                if on_event is not None:
                    await on_event(NormalizedEvent(
                        engine="Codex",
                        thread_id="fake-thread",
                        turn_id="fake-turn",
                        item_kind=ItemKind.ERROR,
                        text="429 测试样本",
                        is_error=True,
                        raw={
                            "api_error_status": 429,
                            "resets_at": (datetime.now(timezone.utc) + timedelta(milliseconds=50)).isoformat(),
                        },
                        route_state="BACKOFF",
                        suspend_new_tasks=True,
                    ))
        if task.agent_kind == self.fail_kind:
            return SimpleNamespace(succeeded=False)
        if task.agent_kind != "planning" and task.output_format == "json":
            task.output_path.parent.mkdir(parents=True, exist_ok=True)
            task.output_path.write_text(
                json.dumps(
                    [{
                        "platform": "hacker_news",
                        "platform_item_id": "1",
                        "permalink": "https://news.ycombinator.com/item?id=1",
                        "title": "Hacker News",
                        "fetched_at": "2026-08-19T00:00:00Z",
                        "raw_metrics": {"points": 42},
                        "normalized_score": None,
                        "norm_method": "none",
                        "norm_context": {
                            "scope": "batch",
                            "platform": "hacker_news",
                            "metric": "points",
                            "n": 1,
                            "formula": "none",
                            "stats": {},
                            "computed_at": "2026-08-19T00:00:00Z",
                            "reason": "insufficient_sample",
                        },
                        "score_authority": 2,
                        "score_freshness": 2,
                        "score_crossref": 1,
                        "score_completeness": 2,
                        "score_independence": 2,
                        "rating_notes": (
                            "权威2:平台原帖 · 时效2:时间窗内 · 交叉1:弱交叉 · "
                            "完整2:字段齐全 · 无关2:无利益关系"
                        ),
                        "rated_by": "baseline:hacker_news@v1",
                    }],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        elif task.agent_kind != "planning":
            task.output_path.parent.mkdir(parents=True, exist_ok=True)
            task.output_path.write_text(
                json.dumps({
                    "markdown": (
                        "# 结论\n\n- 飞书在协作集成上有优势 [S01]\n\n"
                        "# 信息源\n\n- [S01] [Hacker News]"
                        "(https://news.ycombinator.com/item?id=1)\n"
                    ),
                    "claims": [],
                }, ensure_ascii=False),
                encoding="utf-8",
            )
        return SimpleNamespace(succeeded=True)


@asynccontextmanager
async def api_client(
    tmp_path: Path,
    *,
    auto_confirm: bool = False,
    engine: RecordingEngine | None = None,
) -> AsyncIterator[tuple[Any, httpx.AsyncClient, RecordingEngine]]:
    from app.adapters.routing import RoutedAdapter
    from app.api.main import create_app

    engine = engine or RecordingEngine()
    adapter = RoutedAdapter(
        utc_clock=lambda: datetime.now(timezone.utc),
        adapters={"claude": engine, "codex": engine},
    )
    application = create_app(
        tmp_path / "owli.db",
        SCHEMA_PATH,
        engine_probe=lambda: {},
        adapter_factory=lambda: adapter,
        runs_root=tmp_path / "runs",
        auto_confirm=auto_confirm,
    )
    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield application, client, engine


async def wait_for_status(
    client: httpx.AsyncClient, research_id: str, expected: str
) -> dict[str, Any]:
    # §RATE-1 货 2：每个采集章多带一个评级章，同一 event loop 里要多让出几次。
    for _ in range(400):
        response = await client.get(f"/api/researches/{research_id}")
        if response.status_code == 200 and response.json()["data"]["status"] == expected:
            return response.json()["data"]
        await asyncio.sleep(0)
    raise AssertionError(
        f"research 未进入 {expected}；实际={response.json()['data']['status']}"
    )


async def wait_for_goal_status(
    client: httpx.AsyncClient,
    research_id: str,
    goal_id: str,
    expected: str,
) -> dict[str, Any]:
    for _ in range(100):
        response = await client.get(f"/api/researches/{research_id}")
        state = response.json()["data"]
        goal = next(item for item in state["goals"] if item["id"] == goal_id)
        if goal["status"] == expected:
            return state
        await asyncio.sleep(0)
    raise AssertionError(f"{goal_id} 未进入 {expected}")


@async_test
async def test_POST_先生成计划停_awaiting_review_批准前不执行_agent(tmp_path: Path):
    async with api_client(tmp_path) as (application, client, engine):
        created = await client.post(
            "/api/researches",
            json={"query": "飞书竞品优缺点"},
            headers={"X-Request-ID": "create-m2-wiring"},
        )
        assert created.status_code == 200, created.text
        research_id = created.json()["data"]["research_id"]
        state = await wait_for_status(client, research_id, "awaiting_review")
        plan = await client.get(f"/api/researches/{research_id}/plan")

    assert plan.status_code == 200, plan.text
    assert plan.json()["data"]["status"] == "awaiting_review"
    assert state["progress"]["done"] == 0
    assert state["progress"]["total"] == 3
    assert [task.agent_kind for task in engine.tasks] == ["planning"] * 7
    assert application.state.runtime.scheduler_for(research_id) is None
    assert state["cards"]
    assert {card["card_type"] for card in state["cards"]} == {"QUESTION"}
    assert all(card["status"] == "pending" for card in state["cards"])


@async_test
async def test_计划编辑器回答追问后_QUESTION_卡同步归档(tmp_path: Path):
    async with api_client(tmp_path) as (_, client, _):
        created = await client.post(
            "/api/researches",
            json={"query": "飞书竞品优缺点"},
            headers={"X-Request-ID": "create-question-sync"},
        )
        research_id = created.json()["data"]["research_id"]
        await wait_for_status(client, research_id, "awaiting_review")
        plan_response = await client.get(f"/api/researches/{research_id}/plan")
        plan = plan_response.json()["data"]
        plan["decision_balance"][0]["answer"] = plan["decision_balance"][0]["options"][0]
        plan["decision_balance"][0]["answered_at"] = "2026-08-19T00:00:00Z"
        saved = await client.put(f"/api/researches/{research_id}/plan", json=plan)
        assert saved.status_code == 200, saved.text
        state = (await client.get(f"/api/researches/{research_id}")).json()["data"]

    question = next(item for item in state["cards"] if item["card_type"] == "QUESTION")
    assert question["status"] == "answered"
    assert question["result"]["action"] == "plan_edit"


@async_test
async def test_自动确认仍经审核批准干预状态并由_DAG_生成_C1_报告(tmp_path: Path):
    async with api_client(tmp_path, auto_confirm=True) as (application, client, engine):
        created = await client.post(
            "/api/researches",
            json={"query": "飞书竞品优缺点"},
            headers={"X-Request-ID": "create-m2-auto"},
        )
        research_id = created.json()["data"]["research_id"]
        completed = await wait_for_status(client, research_id, "completed")
        plan_response = await client.get(f"/api/researches/{research_id}/plan")
        plan = plan_response.json()["data"]
        report = application.state.store.get_report(research_id)
        report_path = ROOT / str(report["report_path"])
        text = report_path.read_text(encoding="utf-8")
        chapters_response = await client.get(
            f"/api/researches/{research_id}/chapters"
        )
        chapters = chapters_response.json()["data"]["chapters"]
        replay = await application.state.event_buffer.replay_after(research_id, None)
        with sqlite3.connect(tmp_path / "owli.db") as connection:
            stored_citations = connection.execute(
                "SELECT platform, citation_no FROM evidence WHERE report_id = ?",
                (research_id,),
            ).fetchall()

    # §X-1 货 1：收尾另起 reliability-auditor 回填任务，不属计划序列，剔除后比对。
    planned = [task for task in engine.tasks if task.agent_id != "reliability-auditor"]
    assert [task.agent_kind for task in planned] == [
        "planning", "planning", "planning", "planning", "planning", "planning", "planning",
        # §RATE-1 货 2：采集章后自动跟一个评级章（写作前评级），再是 goal-2 的审计章
        "data_collection", "reliability_audit", "reliability_audit",
        "report_writing", "report_writing", "report_writing",
    ]
    assert chapters_response.status_code == 200
    assert chapters and all(
        {"status", "attempts", "engine", "reason"} <= set(item)
        for item in chapters
    )
    assert completed["progress"]["done"] == 3
    assert plan["status"] == "approved" and plan["approved_at"]
    assert plan["decision_balance"][0]["answer"] == plan["decision_balance"][0]["options"][0]
    assert "# 结论" in text and "# 信息源" in text
    assert "[^q-1]" in text
    assert plan["decision_balance"][0]["question"] in text
    assert str(plan["decision_balance"][0]["answer"]) in text
    assert stored_citations == [("hacker_news", 1)]
    card_updates = [
        event.payload["data"]["card"]
        for event in replay.events
        if event.payload.get("type") == "card_update"
    ]
    interventions = [card for card in card_updates if card["card_type"] == "INTERVENE"]
    question_cards = [card for card in card_updates if card["card_type"] == "QUESTION"]
    assert question_cards[-1]["status"] == "answered"
    assert question_cards[-1]["result"]["auto"] is True
    assert interventions
    assert any(card["status"] == "pending" for card in interventions)
    assert all(
        card["result"].get("auto") is True
        for card in interventions
        if card["status"] == "answered"
    )
    report_validations = [
        event.payload["data"]
        for event in replay.events
        if event.payload.get("type") == "report_validation"
    ]
    assert report_validations == [{
        "verdict": "pass",
        "validators": [
            "file_exists",
            "sections_exist:结论,信息源,缺失清单",
            "citation_marks_resolvable",
            "no_orphan_citation",
            "chapter_missing_items_reported",
        ],
        "failures": [],
    }]
    research_statuses = [
        event.payload["data"]["status"]
        for event in replay.events
        if event.payload.get("type") == "research_update"
        and isinstance(event.payload.get("data"), dict)
        and "status" in event.payload["data"]
    ]
    for expected in ("awaiting_review", "approved", "running", "completed"):
        assert expected in research_statuses


@async_test
async def test_执行任务正文包含绝对产物路径供引擎落盘(tmp_path: Path):
    async with api_client(tmp_path, auto_confirm=True) as (_, client, engine):
        created = await client.post(
            "/api/researches",
            json={"query": "飞书竞品优缺点"},
            headers={"X-Request-ID": "create-m2-abs-path"},
        )
        research_id = created.json()["data"]["research_id"]
        await wait_for_status(client, research_id, "completed")

    executed = [task for task in engine.tasks if task.agent_kind != "planning"]
    assert executed
    for task in executed:
        assert str(task.output_path) in task.body, (
            f"{task.agent_id} 的任务正文缺少绝对产物路径，"
            "引擎无从得知落盘位置（cwd 不是 research 根时相对路径必然越界）"
        )


@async_test
async def test_pause_让在跑_agent_完成但新_agent_等_resume(tmp_path: Path):
    engine = RecordingEngine()
    engine.block_kind = "data_collection"
    async with api_client(tmp_path, auto_confirm=True, engine=engine) as (_, client, _):
        created = await client.post(
            "/api/researches",
            json={"query": "飞书竞品优缺点"},
            headers={"X-Request-ID": "create-pause"},
        )
        research_id = created.json()["data"]["research_id"]
        await engine.started.wait()
        paused = await client.post(
            f"/api/researches/{research_id}/pause",
            headers={"X-Request-ID": "pause-running"},
        )
        assert paused.status_code == 200, paused.text
        engine.release.set()
        for _ in range(20):
            await asyncio.sleep(0)
        assert [task.agent_kind for task in engine.tasks] == [
            "planning", "planning", "planning", "planning", "planning", "planning", "planning", "data_collection"
        ]
        resumed = await client.post(
            f"/api/researches/{research_id}/resume",
            headers={"X-Request-ID": "resume-running"},
        )
        assert resumed.status_code == 200, resumed.text
        completed = await wait_for_status(client, research_id, "completed")

    assert completed["progress"]["done"] == 3
    planned = [task for task in engine.tasks if task.agent_id != "reliability-auditor"]
    assert [task.agent_kind for task in planned][-5:] == [
        "reliability_audit", "reliability_audit",
        "report_writing", "report_writing", "report_writing",
    ]


@async_test
async def test_BACKOFF_样本挂起同引擎后续_agent_并沿_SSE_恢复(tmp_path: Path):
    skeleton = _skeleton()
    skeleton["goals"][0]["agents"].append(
        {"name": "HN 数据抓取", "task": "第二批采集，必须等待限流恢复",
         "output": {"shape": "array"}}
    )
    engine = RecordingEngine(skeleton=skeleton)
    engine.backoff_first_collection = True
    async with api_client(tmp_path, auto_confirm=True, engine=engine) as (application, client, _):
        created = await client.post(
            "/api/researches",
            json={"query": "飞书竞品优缺点"},
            headers={"X-Request-ID": "create-backoff"},
        )
        research_id = created.json()["data"]["research_id"]
        for _ in range(50):
            replay = await application.state.event_buffer.replay_after(research_id, None)
            states = [
                event.payload.get("data", {}).get("state")
                for event in replay.events
                if event.payload.get("type") == "route_update"
            ]
            if "BACKOFF" in states:
                break
            await asyncio.sleep(0)
        assert [task.agent_id for task in engine.tasks].count("data-collection-2") == 0
        await asyncio.sleep(0.08)
        completed = await wait_for_status(client, research_id, "completed")
        replay = await application.state.event_buffer.replay_after(research_id, None)

    route_updates = [
        event.payload["data"]
        for event in replay.events
        if event.payload.get("type") == "route_update"
    ]
    route_states = [item["state"] for item in route_updates]
    # D-023：退避开始多发一条带时长的 BACKOFF（「睡多久」要在事件里可读）
    assert route_states[:3] == ["BACKOFF", "BACKOFF", "CONTINUE"]
    assert "秒后重试" in route_updates[1]["reason"]
    assert [task.agent_id for task in engine.tasks].count("data-collection-2") == 1
    assert completed["status"] == "completed"


@async_test
async def test_批准后改_goal_删除已完成产物并双写_feedback_json(tmp_path: Path):
    async with api_client(tmp_path, auto_confirm=True) as (application, client, _):
        created = await client.post(
            "/api/researches",
            json={"query": "飞书竞品优缺点"},
            headers={"X-Request-ID": "create-runtime-edit"},
        )
        research_id = created.json()["data"]["research_id"]
        await wait_for_status(client, research_id, "completed")
        artifact = tmp_path / "runs" / research_id / "goals" / "goal-1" / "evidence.json"
        assert artifact.is_file()

        plan_response = await client.get(f"/api/researches/{research_id}/plan")
        plan = plan_response.json()["data"]
        plan["goals"][1]["objective"] = "按用户调整后的口径重新审计下游证据。"
        edited = await client.put(f"/api/researches/{research_id}/plan", json=plan)
        assert edited.status_code == 200, edited.text
        change = edited.json()["data"]["change_log"][-1]
        with sqlite3.connect(tmp_path / "owli.db") as connection:
            feedback = connection.execute(
                "SELECT kind, json_valid(before_value), json_valid(after_value), extra "
                "FROM feedback WHERE report_id = ? ORDER BY created_at DESC LIMIT 1",
                (research_id,),
            ).fetchone()

    assert not artifact.exists()
    assert change["phase"] == "runtime_intervention"
    assert change["artifact_discarded"]["goal_id"] == "goal-1"
    assert change["feedback_id"].startswith("fb-")
    assert feedback[:3] == ("goal_change", 1, 1)
    assert json.loads(feedback[3])["artifact_discarded"]["path"].endswith("evidence.json")


@async_test
async def test_必失败_goal_下游_skipped_独立_goal_完成且报告如实标注(
    tmp_path: Path, monkeypatch
):
    from app.orchestrator import scheduler as scheduler_module

    monkeypatch.setitem(
        scheduler_module.CHAPTER_RETRY_INTERVAL_SECONDS, "standard", 0.0
    )
    skeleton = _skeleton()
    skeleton["goals"].append({
        "title": "独立输出失败说明",
        "objective": "不依赖失败链路，独立形成可读报告。",
        "depends_on": [],
        "deliverable": {
            "format": "markdown",
            "shape": "object",
            "path": "independent-report.md",
            "description": "如实列出成功与失败阶段的独立报告。",
        },
        "acceptance": ["文件存在且包含结论、信息源 2 个章节"],
        "agents": [{"name": "报告撰写", "task": "独立撰写失败说明报告",
                    "output": {"shape": "object"}}],
    })
    engine = RecordingEngine(skeleton=skeleton)
    engine.fail_kind = "data_collection"
    async with api_client(tmp_path, auto_confirm=True, engine=engine) as (application, client, _):
        created = await client.post(
            "/api/researches",
            json={"query": "飞书竞品优缺点"},
            headers={"X-Request-ID": "create-failure"},
        )
        research_id = created.json()["data"]["research_id"]
        completed = await wait_for_status(client, research_id, "completed")
        report = application.state.store.get_report(research_id)
        report_path = Path(str(report["report_path"]))
        if not report_path.is_absolute():
            report_path = ROOT / report_path
        text = report_path.read_text(encoding="utf-8")

    statuses = {goal["id"]: goal["status"] for goal in completed["goals"]}
    assert statuses == {
        "goal-1": "done",
        "goal-2": "done",
        "goal-3": "done",
        "goal-4": "done",
    }
    assert len([task for task in engine.tasks if task.agent_kind == "data_collection"]) == 3
    assert any(task.agent_kind == "reliability_audit" for task in engine.tasks)
    assert any(task.agent_id.startswith("report-writing-2-sec-") for task in engine.tasks)
    assert "retry_exhausted" in text
    # W-1：采集全失败导致证据池为空时，撰写章必须如实判红并落缺失，
    # 不得从 done 产物 URL 回退拼出一份伪引用报告。
    assert "conclusion_invalid" in text
    assert "# 结论" not in text and "# 信息源" not in text
    assert "[^q-1]" in text


@async_test
async def test_stop_接_scheduler_终止且在跑结果不再启动后续(tmp_path: Path):
    engine = RecordingEngine()
    engine.block_kind = "data_collection"
    async with api_client(tmp_path, auto_confirm=True, engine=engine) as (_, client, _):
        created = await client.post(
            "/api/researches",
            json={"query": "飞书竞品优缺点"},
            headers={"X-Request-ID": "create-stop"},
        )
        research_id = created.json()["data"]["research_id"]
        await engine.started.wait()
        stopped = await client.post(
            f"/api/researches/{research_id}/stop",
            headers={"X-Request-ID": "stop-running"},
        )
        assert stopped.status_code == 200, stopped.text
        engine.release.set()
        for _ in range(20):
            await asyncio.sleep(0)
        snapshot = await client.get(f"/api/researches/{research_id}")

    assert snapshot.json()["data"]["status"] == "stopped"
    assert [task.agent_kind for task in engine.tasks] == [
        "planning", "planning", "planning", "planning", "planning", "planning", "planning", "data_collection"
    ]


@async_test
async def test_stop后resume真续跑_状态取自调度器且不阻塞(tmp_path: Path):
    """D-003 缺陷 B：/stop 之后 /resume 曾是 no-op，API 却无条件回报 running。"""
    engine = RecordingEngine()
    engine.block_kind = "data_collection"
    async with api_client(tmp_path, auto_confirm=True, engine=engine) as (application, client, _):
        created = await client.post(
            "/api/researches",
            json={"query": "飞书竞品优缺点"},
            headers={"X-Request-ID": "create-stop-resume"},
        )
        research_id = created.json()["data"]["research_id"]
        await engine.started.wait()
        stopped = await client.post(
            f"/api/researches/{research_id}/stop",
            headers={"X-Request-ID": "stop-before-resume"},
        )
        assert stopped.status_code == 200, stopped.text
        assert [action["id"] for action in stopped.json()["data"]["actions"]] == ["resume"]
        engine.release.set()
        for _ in range(20):
            await asyncio.sleep(0)
        scheduler = application.state.runtime.scheduler_for(research_id)
        assert scheduler.status == "stopped"
        # 停下时在跑的章不得留 running 幽灵
        store = application.state.store
        assert all(
            row["status"] != "running" for row in store.list_chapters(research_id)
        ), store.list_chapters(research_id)

        started_at = time.monotonic()
        resumed = await client.post(
            f"/api/researches/{research_id}/resume",
            headers={"X-Request-ID": "resume-after-stop"},
        )
        elapsed = time.monotonic() - started_at
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["data"]["status"] == scheduler.status == "running"
        assert elapsed < 5.0, f"/resume 阻塞了 {elapsed:.1f}s"
        completed = await wait_for_status(client, research_id, "completed")

    assert completed["progress"]["done"] == 3
    assert scheduler.status == "completed"
    assert {row["status"] for row in store.list_chapters(research_id)} == {"done"}


@async_test
async def test_调整后继续_停在干预点_编辑下游后再由继续卡推进(tmp_path: Path):
    async with api_client(tmp_path) as (_, client, engine):
        created = await client.post(
            "/api/researches",
            json={"query": "飞书竞品优缺点"},
            headers={"X-Request-ID": "create-adjust"},
        )
        research_id = created.json()["data"]["research_id"]
        reviewed = await wait_for_status(client, research_id, "awaiting_review")
        for card in [item for item in reviewed["cards"] if item["card_type"] == "QUESTION"]:
            action = card["actions"][0]
            answered = await client.post(
                f"/api/cards/{card['card_id']}/respond",
                json={"action": action["id"], "payload": {"choice": action["value"]}},
                headers={"X-Request-ID": f"answer-{card['card_id']}"},
            )
            assert answered.status_code == 200, answered.text
        await client.post(
            f"/api/researches/{research_id}/plan/approve",
            headers={"X-Request-ID": "approve-adjust"},
        )
        intervened = await wait_for_goal_status(
            client, research_id, "goal-1", "awaiting_intervention"
        )
        first_card = next(
            item for item in intervened["cards"]
            if item["card_type"] == "INTERVENE" and item["status"] == "pending"
        )
        adjusted = await client.post(
            f"/api/cards/{first_card['card_id']}/respond",
            json={"action": "adjust", "payload": {"choice": "adjust"}},
            headers={"X-Request-ID": "adjust-goal-1"},
        )
        assert adjusted.status_code == 200, adjusted.text
        still_waiting = await wait_for_goal_status(
            client, research_id, "goal-1", "awaiting_intervention"
        )
        # §RATE-1 货 2：goal-1 自己的评级章跟采集章一起跑完了；这里看的是 goal-2 的审计章
        assert not any(
            task.agent_kind == "reliability_audit" and task.goal_id == "goal-2"
            for task in engine.tasks
        )

        plan_response = await client.get(f"/api/researches/{research_id}/plan")
        plan = plan_response.json()["data"]
        plan["goals"][1]["objective"] = "依据干预点反馈调整审计口径。"
        edited = await client.put(f"/api/researches/{research_id}/plan", json=plan)
        assert edited.status_code == 200, edited.text
        pending = [
            item for item in (await client.get(f"/api/researches/{research_id}")).json()["data"]["cards"]
            if item["card_type"] == "INTERVENE" and item["status"] == "pending"
        ]
        assert len(pending) == 1
        assert pending[0]["card_id"] != first_card["card_id"]
        continued = await client.post(
            f"/api/cards/{pending[0]['card_id']}/respond",
            json={"action": "continue", "payload": {"choice": "continue"}},
            headers={"X-Request-ID": "continue-after-adjust"},
        )
        assert continued.status_code == 200, continued.text
        await wait_for_goal_status(client, research_id, "goal-2", "awaiting_intervention")
        audit_task = next(
            task for task in engine.tasks
            if task.agent_kind == "reliability_audit" and task.goal_id == "goal-2"
        )

    assert still_waiting["status"] == "running"
    assert "依据干预点反馈调整审计口径" in audit_task.body


def _six_goal_evidence_skeleton() -> dict[str, Any]:
    goals = []
    for number in range(1, 6):
        agent = (
            {
                "name": "HN 数据抓取·飞书",
                "task": "采集飞书相关证据",
                "output": {"shape": "array"},
            }
            if number == 1
            else {
                "name": "数据清洗",
                "task": "清洗并规范证据字段",
                "output": {"shape": "array"},
            }
        )
        goals.append({
            "title": f"证据处理阶段 {number}",
            "objective": f"形成阶段 {number} 可复核产物。",
            "depends_on": [] if number == 1 else [f"goal-{number - 1}"],
            "deliverable": {
                "format": "json",
                "shape": "array",
                "path": f"evidence-{number}.json",
                "description": "标准 evidence 数组。",
            },
            "acceptance": ["文件存在且为非空 JSON"],
            "agents": [agent],
        })
    goals.append({
        "title": "故意失败的下游报告",
        "objective": "验证失败 run 保留上游证据。",
        "depends_on": ["goal-5"],
        "deliverable": {
            "format": "markdown",
            "shape": "object",
            "path": "report.md",
            "description": "最终报告。",
        },
        "acceptance": ["报告包含结论与信息源章节"],
        "agents": [{
            "name": "报告撰写",
            "task": "本测试注入失败",
            "output": {"shape": "object"},
        }],
    })
    return {
        "market_profile": "global_product",
        "market_profile_justification": "测试对象面向全球市场。",
        "subjects": ["飞书"],
        "subjects_justification": "飞书是本测试的研究主体。",
        "goals": goals,
    }


def _persistable_evidence(platform: str, item_id: str) -> dict[str, Any]:
    metric = {
        "hacker_news": "points",
        "product_hunt": "votes_count",
        "x": "like_count",
    }[platform]
    context = {
        "scope": "batch",
        "platform": platform,
        "metric": metric,
        "n": 1,
        "formula": "none",
        "stats": {},
        "computed_at": "2026-08-21T04:00:00+00:00",
        "reason": "insufficient_sample",
    }
    if platform == "x":
        context["sampling"] = "post_filtered_local"
    return {
        "platform": platform,
        "platform_item_id": item_id,
        "permalink": f"https://example.com/{platform}/{item_id}",
        "fetched_at": "2026-08-21T04:00:00+00:00",
        "raw_metrics": {metric: 1},
        "normalized_score": None,
        "norm_method": "none",
        "norm_context": context,
        "score_authority": 1,
        "score_freshness": 2,
        "score_crossref": 0,
        "score_completeness": 1,
        "score_independence": 2,
        "rating_notes": (
            "权威1:平台基线 · 时效2:时间窗内 · 交叉0:单一来源 · "
            "完整1:摘要可追溯 · 无关2:无利益关系"
        ),
        "rated_by": f"baseline:{platform}@v1",
        "citation_no": 7,
    }


def test_原始采集证据可用章节能力补平台并投影入库字段(tmp_path: Path):
    artifact = tmp_path / "web-search.json"
    artifact.write_text(
        json.dumps([{
            "permalink": "https://example.com/product/?utm_source=test",
            "fetched_at": "2026-08-25T04:00:00+00:00",
            "source_type": "product_page",
            "title": "OpenAI product",
            "summary": "原始采集摘要",
        }], ensure_ascii=False),
        encoding="utf-8",
    )

    payloads = load_evidence_payloads(
        artifact,
        report_id="r-raw",
        goal_id="goal-1",
        agent_name="data-collection",
        platform_hint="web_search",
    )

    assert len(payloads) == 1
    assert payloads[0]["platform"] == "web_search"
    assert payloads[0]["permalink"] == "https://example.com/product?utm_source=test"
    assert payloads[0]["source_type"] == "other"
    assert payloads[0]["extra"]["artifact_source_type"] == "product_page"
    assert payloads[0]["extra"]["summary"] == "原始采集摘要"


class GoalFiveEvidenceEngine(RecordingEngine):
    def __init__(self) -> None:
        super().__init__(skeleton=_six_goal_evidence_skeleton())
        self.fail_kind = "report_writing"

    async def run(self, task, ctx, on_event=None):
        result = await super().run(task, ctx, on_event=on_event)
        if task.goal_id == "goal-5" and result.succeeded:
            task.output_path.write_text(
                json.dumps([
                    _persistable_evidence("hacker_news", "1"),
                    _persistable_evidence("product_hunt", "ph-1"),
                    _persistable_evidence("x", "x-1"),
                ], ensure_ascii=False),
                encoding="utf-8",
            )
        return result


@async_test
async def test_goal5完成后下游run缺失_已入库三平台证据仍保留(tmp_path: Path):
    engine = GoalFiveEvidenceEngine()
    async with api_client(
        tmp_path, auto_confirm=True, engine=engine
    ) as (application, client, _):
        created = await client.post(
            "/api/researches",
            json={"query": "失败 run 证据保留"},
            headers={"X-Request-ID": "create-evidence-survives-failure"},
        )
        research_id = created.json()["data"]["research_id"]
        await wait_for_status(client, research_id, "completed")
        report = application.state.store.get_report(research_id)
        with sqlite3.connect(tmp_path / "owli.db") as connection:
            rows = connection.execute(
                "SELECT platform, citation_no FROM evidence "
                "WHERE report_id = ? ORDER BY platform",
                (research_id,),
            ).fetchall()

    assert report["status"] == "completed"
    assert rows == [
        ("hacker_news", None),
        ("product_hunt", None),
        ("x", None),
    ]


def test_t_m2_脚本输出两_goal_依赖_必失败与状态迁移序列() -> None:
    script = ROOT / "scripts" / "t-m2-orchestrator.py"
    assert script.is_file(), "scripts/t-m2-orchestrator.py 尚未创建"
    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=ROOT,
        env={**os.environ, "OWLI_AUTO_CONFIRM": "1"},
        capture_output=True,
        text=True,
    )
    output = result.stdout + result.stderr

    assert "状态迁移序列" in output
    assert "goal-1:running" in output
    assert "goal-1:awaiting_intervention" in output
    assert "goal-1:done" in output
    assert "goal-2:failed" in output
    assert "必失败注入尝试次数=2" in output
    assert "结构化验收: PASS" in output
