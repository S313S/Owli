"""接缝夹具：确定性复现 6b 整跑暴露的四条「模块交接」缺陷（A/B/C/D），不起真实引擎。

用法：
  cd Owli-m3h
  ../Owli/.venv/bin/python scripts/fixtures/seam_fixture.py <输出目录> --case <name>

case 一览（对当前代码各自必红；括号里是「防修过头」的绿断言）：
  finalize-json-report  缺陷 D：报告章 output.format=json → 收尾兜底到不存在的 report.md，
                        写空占位，三 goal 全 done 的研究被判 failed（guard：goal/章全 done）
  partial-done          缺陷 A：partial + unmet + reason=empty_result + validation PASS 的章
                        被 reason 短路判成 missing，_record_unmet 永不执行（guard：产物真空仍 missing）
  ratelimit-regex       缺陷 C：「非限流错误」原文被 `限流` 正则归成 quota_exhausted
                        （guard：api_error_status=429 仍归 quota_exhausted；超时仍归 timeout）
  stop-resume           缺陷 B：走 API，/stop 之后 /resume 是 no-op 但回报 running，研究永不到终态
                        （guard：/resume 必须在 N 秒内返回，不阻塞到整轮结束）
  section-transport-jitter  缺陷 E：节级传输断连一次即 missing/retry_exhausted（attempts=1，没重试），
                        章级重试又跳过 missing 节（guard：真耗尽仍 missing；conclusion_invalid
                        不被重试；真 429 仍 quota_exhausted）

所有章产物都由假适配器构造；结论落 <输出目录>/summary.json（每条断言 name/expected/actual/passed）。
退出码只给调度脚本用（全过 0，否则 1），判定以 summary.json 为准。
已知坑（沿用 report_sectioning_fixture）：capability.profile 必须在闭集内；
event_buffer.publish 必须是 async；agent_id 全局唯一。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, ".")

from app.adapters import validation
from app.adapters.contracts import EngineRunResult, OwliResult
from app.orchestrator.chapter_failure import chapter_failure_reason
from app.orchestrator.runtime import RuntimeCoordinator
from app.plan.model import DEFAULT_RETRY_POLICY, Plan
from app.plan.store import save_plan
from app.store.dao import Store
from tests.plan_factory import make_plan_dict


QUERY = "豆包语音输入法的竞品分析"
SCALE = "fast"
PLACEHOLDER_TEXT = "本次运行未生成完整结论"
SCHEMA = Path("app/store/schema.sql")

# 缺陷 C 的真实原文（worklog §十，run-2 goal-3/ch-3/sec-2 账本 engine_error 逐字）
NON_RATELIMIT_ENGINE_ERROR = {
    "is_error": True,
    "api_error_status": None,
    "subtype": "error_during_execution",
    "result": "非限流错误：error_during_execution",
    "errors": [
        "AxiosError: timeout of 5000ms exceeded",
        "Error: The socket connection was closed unexpectedly",
        "TelemetrySafeError: Output does not match required schema: "
        "/summary: must NOT have more than 200 characters",
    ],
}
NON_RATELIMIT_CONCLUSION_ERROR = "owli-result.summary 必须是 200 字以内字符串"
REAL_RATELIMIT_ENGINE_ERROR = {
    "is_error": True,
    "api_error_status": 429,
    "subtype": "error_during_execution",
    "result": "HTTP 429 rate limit",
    "errors": ["Error: 429 Too Many Requests"],
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Checks:
    """断言表：name / expected / actual / passed。"""

    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def add(self, name: str, *, expected: Any, actual: Any, passed: bool,
            guard: bool = False) -> None:
        entry = {
            "name": name,
            "expected": expected,
            "actual": actual,
            "passed": bool(passed),
            "kind": "guard(防修过头)" if guard else "defect(对当前代码应红)",
        }
        self.items.append(entry)
        mark = "通过" if passed else "未过"
        print(f"  [{mark}] {name}\n         期望={expected!r}\n         实际={actual!r}",
              flush=True)

    def failed_names(self) -> list[str]:
        return [item["name"] for item in self.items if not item["passed"]]


# --------------------------------------------------------------------------
# 计划构造（手工最小计划，不经规划期）
# --------------------------------------------------------------------------

def _chapter(cid: str, ctype: str, path: str, entities: list[str],
             inputs: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "chapter_id": cid, "chapter_type": ctype, "plan_path": f"x/{cid}.md",
        "opening": {"inputs": [{"path": p} for p in inputs], "task": "见 task",
                    "acceptance": ["产物按声明路径落盘"]},
        "closing": {"output": {"path": path}, "entities": entities,
                    "expected_count": None, "notes": {}},
    }


def _agent(goal_id: str, agent_id: str, *, display: str, kind_profile: str,
           engine: str, fmt: str, path: str, validators: list[str], cid: str,
           ctype: str, inputs: tuple[str, ...] = ()) -> dict[str, Any]:
    profile_caps = {
        "web-collector": {"tools": ["source.web_search", "fs.write", "db.write"],
                          "sources": ["web_search"], "network": "allowlist"},
        "report-writer": {"tools": ["fs.read", "fs.write"], "sources": [], "network": "none"},
        "readonly-analyst": {"tools": ["fs.read", "fs.write"], "sources": [], "network": "none"},
    }[kind_profile]
    return {
        "agent_id": agent_id, "display_name": display, "entity": None,
        "task": f"{display}（接缝夹具假适配器执行）", "depends_on": [],
        "inputs": [{"path": p} for p in inputs], "engine": engine, "model": None,
        "capability": {
            "profile": kind_profile, **profile_caps,
            "fs": {"read": ["goals/**"], "write": [f"goals/{goal_id}/**"]},
            "shell": "none",
        },
        "prompt": {"preamble_ref": "common/v1", "body": "接缝夹具：产物由假适配器直接落盘。",
                   "assumptions_policy": "assume_and_declare"},
        "output": {"format": fmt, "shape": "array" if fmt == "json" else "object",
                   "path": path, "validators": validators},
        "chapter": _chapter(cid, ctype, path, ["讯飞输入法"], inputs),
        "extra_quota_credits": None, "origin": {"_node": "generated"}, "status": "queued",
    }


def _goal(goal_id: str, title: str, agents: list[dict[str, Any]],
          depends_on: list[str]) -> dict[str, Any]:
    first = agents[0]["output"]
    return {
        "goal_id": goal_id, "title": title[:24], "objective": f"{title}，供下游消费。",
        "depends_on": depends_on,
        "deliverable": {"format": first["format"], "shape": first["shape"],
                        "path": first["path"], "description": f"{title}产物。"},
        "acceptance": ["产物按声明路径落盘"],
        "intervention": {"on_complete": True, "prompt": f"请核对《{title}》产物，是否继续？"},
        "retry_policy": dict(DEFAULT_RETRY_POLICY),
        "on_upstream_failure": "skip", "agents": agents, "status": "pending",
    }


def collection_agent(goal_id: str, serial: int) -> dict[str, Any]:
    agent_id = "data-collection" if serial == 1 else f"data-collection-{serial}"
    return _agent(goal_id, agent_id, display=f"数据采集·web_search#{serial}",
                  kind_profile="web-collector", engine="codex", fmt="json",
                  path=f"goals/{goal_id}/{agent_id}.json",
                  validators=["file_exists", "json_array_min_items:1"],
                  cid="ch-1", ctype="collection")


def build_plan(rid: str, goals: list[dict[str, Any]]) -> Plan:
    src = make_plan_dict()
    src.update({
        "research_id": rid, "title": QUERY, "research_question": QUERY,
        "scale": SCALE, "status": "approved", "approved_at": _now(),
        "market_profile": "cn_product",
        "market_profile_justification": "题目与竞品均为中文市场输入法产品。",
        # subjects 留空：否则规则 25 会要求每个 goal 都有该实体的采集 agent，手工最小计划过不了 /approve
        "subjects": [], "subjects_justification": "接缝夹具最小计划不声明研究实体。",
        "baseline": None, "goals": goals, "created_at": _now(), "updated_at": _now(),
    })
    return Plan.from_dict(src)


def plan_finalize_json_report(rid: str) -> Plan:
    """run-2 同构：采集章 done + 两个 report-writer 章都把产物声明成 json。"""
    g1 = _goal("goal-1", "竞品基准信息采集", [collection_agent("goal-1", 1)], [])
    cmp_path = "goals/goal-2/competitor-voice-input-comparison.json"
    g2 = _goal("goal-2", "竞品语音输入对比", [_agent(
        "goal-2", "report-writing", display="对比章（report-writer）",
        kind_profile="report-writer", engine="claude", fmt="json", path=cmp_path,
        validators=["file_exists"], cid="ch-1", ctype="comparison",
        inputs=("goals/goal-1/data-collection.json",))], ["goal-1"])
    g3 = _goal("goal-3", "竞品分析报告", [_agent(
        "goal-3", "report-writing-2", display="报告章（report-writer）",
        kind_profile="report-writer", engine="claude", fmt="json",
        path="goals/goal-3/comparative-analysis.json", validators=["file_exists"],
        cid="ch-1", ctype="report", inputs=(cmp_path,))], ["goal-2"])
    return build_plan(rid, [g1, g2, g3])


def plan_partial(rid: str, *, agent_id: str = "consistency-check") -> Plan:
    """run-1 goal-1/ch-3 同构：cross_validation 章，markdown，非节化。"""
    g1 = _goal("goal-1", "一致性检查", [_agent(
        "goal-1", agent_id, display="一致性检查（claude）",
        kind_profile="readonly-analyst", engine="claude", fmt="markdown",
        path=f"goals/goal-1/{agent_id}.md",
        validators=["file_exists", "sections_exist:结论"], cid="ch-3",
        ctype="cross_validation")], [])
    return build_plan(rid, [g1])


def plan_empty_collection(rid: str) -> Plan:
    return build_plan(rid, [_goal("goal-1", "真空采集", [collection_agent("goal-1", 1)], [])])


def plan_stop_resume(rid: str) -> Plan:
    g1 = _goal("goal-1", "采集一", [collection_agent("goal-1", 1)], [])
    g2 = _goal("goal-2", "采集二", [collection_agent("goal-2", 2)], ["goal-1"])
    return build_plan(rid, [g1, g2])


# --------------------------------------------------------------------------
# 运行时装配：Store / 假适配器 / RuntimeCoordinator
# --------------------------------------------------------------------------

def make_store(root: Path, rid: str) -> Store:
    database = root / "owli.db"
    if not database.exists():
        with sqlite3.connect(database) as connection:
            connection.executescript(SCHEMA.read_text(encoding="utf-8"))
    store = Store(database)
    store.create_report(id=rid, title=QUERY, research_question=QUERY, created_at=_now(),
                        use_case="product_competitor", status="running",
                        extra={"scale": SCALE, "fixture": "seam"})
    return store


def _pass_report(task: Any, ctx: validation.Ctx) -> validation.ValidationReport:
    """用真实校验器跑一遍声明的 validators，确保「validation PASS」不是夹具口头宣称。"""
    return validation.validate(ctx, list(task.validators))


def _done(task: Any, ctx: validation.Ctx, *, count: int = 1) -> EngineRunResult:
    return EngineRunResult(
        conclusion=OwliResult("done", str(task.output_path), "夹具产物已落盘", [], [], []),
        conclusion_error=None, validation=_pass_report(task, ctx), events=[],
        permission_denials=[],
    )


class FakeAdapter:
    """不起引擎的适配器替身：按 agent_kind / agent_id 直接落产物并返回结构化结论。

    behaviors: {agent_id 或 agent_kind: async fn(task, ctx) -> EngineRunResult}
    未命中的任务走默认：json → 一条带 permalink 的数组；markdown → 带角标的结论/信息源。
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.behaviors: dict[str, Any] = {}
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.block_agent_id: str | None = None

    async def run(self, task: Any, ctx: Any, on_event: Any = None) -> Any:
        self.calls.append({"goal_id": task.goal_id, "agent_id": task.agent_id,
                           "kind": task.agent_kind, "format": task.output_format,
                           "path": str(task.output_path), "at": _now()})
        if task.agent_id == self.block_agent_id:
            self.started.set()
            await self.release.wait()
        handler = self.behaviors.get(task.agent_id) or self.behaviors.get(task.agent_kind)
        if handler is not None:
            return await handler(task, ctx)
        task.output_path.parent.mkdir(parents=True, exist_ok=True)
        if task.output_format == "json":
            task.output_path.write_text(json.dumps([{
                "competitor": "讯飞输入法",
                "title": "讯飞输入法语音识别准确率实测 98%，支持 23 种方言",
                "permalink": "https://example.com/iflytek-dialect",
                "fetched_at": "2026-08-20T02:00:00Z",
                "snippet": "方言免切换识别；离线包可用",
            }], ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            task.output_path.write_text(
                "## 结论\n\n- 讯飞输入法方言覆盖领先豆包 [S01]\n\n"
                "## 信息源\n\n- [S01] [讯飞方言实测](https://example.com/iflytek-dialect)\n",
                encoding="utf-8")
        return _done(task, ctx)


def make_coordinator(store: Store, root: Path, rid: str, plan: Plan,
                     adapter: FakeAdapter, published: list[dict[str, Any]]) -> RuntimeCoordinator:
    async def publish(research_id: str, payload: Any) -> None:
        event = payload if isinstance(payload, dict) else {"type": str(payload)}
        published.append(event)
        kind = event.get("type")
        if kind in {"chapter_update", "goal_gate", "agent_error", "report_validation"}:
            print(f"    «{kind}» {json.dumps(event.get('data'), ensure_ascii=False)[:300]}",
                  flush=True)

    coordinator = RuntimeCoordinator(
        store=store, event_buffer=SimpleNamespace(publish=publish), researches={}, cards={},
        runs_root=root / "runs", auto_confirm=True,
        routing_utc_clock=lambda: datetime.now(timezone.utc),
    )
    save_plan(store, plan, expected_rev=0)
    coordinator.researches[rid] = coordinator._state_from_plan(plan)
    coordinator._adapters[rid] = adapter
    return coordinator


async def run_to_terminal(coordinator: RuntimeCoordinator, plan: Plan,
                          budget_seconds: float) -> bool:
    try:
        await asyncio.wait_for(coordinator.start_research(plan), timeout=budget_seconds)
    except asyncio.TimeoutError:
        scheduler = coordinator.scheduler_for(plan.research_id)
        if scheduler is not None:
            await scheduler.stop()
        return True
    return False


# --------------------------------------------------------------------------
# case finalize-json-report（缺陷 D）
# --------------------------------------------------------------------------

async def case_finalize_json_report(root: Path, checks: Checks, budget: float) -> dict[str, Any]:
    rid = "r-seam-finalize"
    store = make_store(root, rid)
    plan = plan_finalize_json_report(rid)
    adapter = FakeAdapter()
    published: list[dict[str, Any]] = []
    coordinator = make_coordinator(store, root, rid, plan, adapter, published)

    # 收尾前快照：_report_path() 指向的文件在 _append_decision_notes 写占位之前是否存在。
    probe: dict[str, Any] = {}
    original_append = coordinator._append_decision_notes

    def probing_append(path: Path, plan_: Plan, scheduler: Any) -> None:
        probe["report_path"] = str(path)
        probe["existed_before_finalize"] = path.is_file()
        original_append(path, plan_, scheduler)

    coordinator._append_decision_notes = probing_append  # 仅夹具实例，不改产品代码

    timed_out = await run_to_terminal(coordinator, plan, budget)
    scheduler = coordinator.scheduler_for(rid)
    ledger = store.list_chapters(rid)
    report_row = store.get_report(rid) or {}
    state = coordinator.researches[rid]
    report_path = Path(probe.get("report_path") or coordinator._report_path(plan))
    report_text = report_path.read_text(encoding="utf-8") if report_path.is_file() else ""
    declared_report_outputs = {
        str(root / "runs" / rid / agent.output["path"])
        for goal in plan.goals for agent in goal.agents
        if str(agent.capability.get("profile")) == "report-writer"
    }
    artifacts = {
        path: ((root / "runs" / rid / path).stat().st_size
               if (root / "runs" / rid / path).is_file() else None)
        for path in [a.output["path"] for g in plan.goals for a in g.agents]
    }
    validation_events = [e["data"] for e in published if e.get("type") == "report_validation"]

    goal_statuses = {} if scheduler is None else dict(scheduler.goal_statuses)
    chapter_statuses = {f"{r['goal_id']}/{r['chapter_id']}": r["status"] for r in ledger
                        if "/" not in r["chapter_id"]}
    checks.add("guard: 三 goal 全 done 且调度器 completed", guard=True,
               expected={"scheduler": "completed", "goals": "全 done"},
               actual={"scheduler": None if scheduler is None else scheduler.status,
                       "goals": goal_statuses, "timed_out": timed_out},
               passed=(scheduler is not None and scheduler.status == "completed"
                       and set(goal_statuses.values()) == {"done"} and not timed_out))
    checks.add("guard: 三章账本全 done 且 report-writer 的 json 产物真实存在", guard=True,
               expected="每章 status=done；两份 json 产物非空",
               actual={"chapters": chapter_statuses, "artifacts_bytes": artifacts},
               passed=(bool(chapter_statuses) and set(chapter_statuses.values()) == {"done"}
                       and all(size for size in artifacts.values())))
    checks.add("D1 研究终态 status == completed（reports 表与 API 状态一致）",
               expected="completed",
               actual={"reports.status": report_row.get("status"),
                       "state.status": state.get("status"),
                       "summary_line": report_row.get("summary_line"),
                       "report_validation": validation_events[-1:] },
               passed=(report_row.get("status") == "completed"
                       and state.get("status") == "completed"))
    checks.add("D2 _report_path() 指向收尾前已真实存在的报告文件（且是某个 report-writer 章的产物）",
               expected={"existed_before_finalize": True, "is_declared_report_output": True},
               actual={"report_path": str(report_path),
                       "existed_before_finalize": probe.get("existed_before_finalize"),
                       "is_declared_report_output": str(report_path) in declared_report_outputs},
               passed=(probe.get("existed_before_finalize") is True
                       and str(report_path) in declared_report_outputs))
    checks.add("D3 收尾没有写入空占位正文",
               expected=f"报告正文不含「{PLACEHOLDER_TEXT}」",
               actual={"contains_placeholder": PLACEHOLDER_TEXT in report_text,
                       "report_bytes": len(report_text.encode("utf-8")),
                       "head": report_text[:160]},
               passed=PLACEHOLDER_TEXT not in report_text and bool(report_text))
    return {"research_id": rid, "ledger": ledger, "report": report_row, "state_status": state.get("status"),
            "report_path": str(report_path), "probe": probe, "artifacts": artifacts,
            "report_validation": validation_events, "adapter_calls": adapter.calls}


# --------------------------------------------------------------------------
# case partial-done（缺陷 A）
# --------------------------------------------------------------------------

PARTIAL_UNMET = ["「两份采集同时给证据」因 DC2 为空无法物理满足"]


async def _partial_behavior(task: Any, ctx: Any) -> EngineRunResult:
    task.output_path.parent.mkdir(parents=True, exist_ok=True)
    body = ("# 一致性检查\n\n## alignment_table\n\n| 维度 | 讯飞 | 搜狗 |\n|---|---|---|\n| 方言 | 23 种 | 未知 |\n\n"
            "## conflicts\n\n- 准确率口径不一致\n\n## gaps\n\n- DC2 为空\n\n"
            "## 结论\n\n- 仅能基于 DC1 单边对齐 [S01]\n\n## 信息源\n\n- [S01] https://example.com/iflytek-dialect\n")
    task.output_path.write_text(body * 90, encoding="utf-8")  # ≈ 26 KB，对齐 run-1 的 25 961 B
    return EngineRunResult(
        conclusion=OwliResult("partial", str(task.output_path), "部分完成：DC2 为空", [],
                              list(PARTIAL_UNMET), [], "empty_result"),
        conclusion_error=None, validation=_pass_report(task, ctx), events=[],
        permission_denials=[],
    )


async def _empty_collection_behavior(task: Any, ctx: Any) -> EngineRunResult:
    task.output_path.parent.mkdir(parents=True, exist_ok=True)
    task.output_path.write_text("[]", encoding="utf-8")
    empty_ctx = validation.Ctx(**{**vars(ctx), "missing_reason": "empty_result"})
    return EngineRunResult(
        conclusion=OwliResult("partial", str(task.output_path), "零命中", [], [], [], "empty_result"),
        conclusion_error=None, validation=_pass_report(task, empty_ctx), events=[],
        permission_denials=[],
    )


async def case_partial_done(root: Path, checks: Checks, budget: float) -> dict[str, Any]:
    # 子例 1：合法 partial（产物 26 KB + validators 全 PASS + unmet 非空 + reason=empty_result）
    rid = "r-seam-partial"
    store = make_store(root, rid)
    plan = plan_partial(rid)
    adapter = FakeAdapter()
    adapter.behaviors["consistency-check"] = _partial_behavior
    published: list[dict[str, Any]] = []
    coordinator = make_coordinator(store, root, rid, plan, adapter, published)
    timed_out = await run_to_terminal(coordinator, plan, budget)
    row = next(r for r in store.list_chapters(rid) if r["chapter_id"] == "ch-3")
    artifact = root / "runs" / rid / "goals/goal-1/consistency-check.md"
    unmet_file = root / "runs" / rid / "goals/goal-1/.owli-unmet-consistency-check.json"
    unmet_items = coordinator._unmet_items(rid)
    artifact_report = validation.validate(validation.Ctx(
        output_path=artifact, output_format="markdown", research_id=rid, goal_id="goal-1",
        agent_id="consistency-check", read_text=lambda: artifact.read_text(encoding="utf-8"),
        read_json=lambda: None, store=store, source_domains=frozenset(), runs_root=root / "runs",
    ), ["file_exists", "sections_exist:结论"]) if artifact.is_file() else None

    checks.add("guard: 产物真实落盘且声明的 validators 全 PASS（succeeded 的前提成立）", guard=True,
               expected={"artifact_exists": True, "verdict": "pass"},
               actual={"artifact_bytes": artifact.stat().st_size if artifact.is_file() else None,
                       "verdict": None if artifact_report is None else artifact_report.verdict.value,
                       "timed_out": timed_out},
               passed=(artifact_report is not None
                       and artifact_report.verdict is validation.Verdict.PASS and not timed_out))
    checks.add("A1 合法 partial 的章账本 chapter_progress.status == done",
               expected={"status": "done", "reason": None},
               actual={"status": row["status"], "reason": row["reason"], "attempts": row["attempts"],
                       "engine_error": row["engine_error"], "conclusion_error": row["conclusion_error"]},
               passed=(row["status"] == "done" and row["reason"] is None))
    checks.add("A2 unmet 已被 _record_unmet() 记录（.owli-unmet-*.json 可查且条目一致）",
               expected={"unmet_file_exists": True, "unmet": PARTIAL_UNMET},
               actual={"unmet_file_exists": unmet_file.is_file(),
                       "unmet_items": unmet_items},
               passed=(unmet_file.is_file() and any(
                   item.get("unmet") == PARTIAL_UNMET for item in unmet_items)))

    # 子例 2（guard，硬约束 4a）：产物真是空数组 + reason=empty_result → 仍 missing
    rid2 = "r-seam-partial-empty"
    store2 = make_store(root, rid2)
    plan2 = plan_empty_collection(rid2)
    adapter2 = FakeAdapter()
    adapter2.behaviors["data-collection"] = _empty_collection_behavior
    coordinator2 = make_coordinator(store2, root, rid2, plan2, adapter2, [])
    await run_to_terminal(coordinator2, plan2, budget)
    row2 = store2.list_chapters(rid2)[0]
    checks.add("guard: 产物为空数组 + reason=empty_result → 章仍 missing/empty_result（4a 不被放宽）",
               guard=True, expected={"status": "missing", "reason": "empty_result"},
               actual={"status": row2["status"], "reason": row2["reason"],
                       "actual_count": row2["actual_count"], "attempts": row2["attempts"]},
               passed=(row2["status"] == "missing" and row2["reason"] == "empty_result"))
    return {"research_id": rid, "ledger": store.list_chapters(rid), "unmet_items": unmet_items,
            "empty_case_ledger": store2.list_chapters(rid2), "adapter_calls": adapter.calls}


# --------------------------------------------------------------------------
# case ratelimit-regex（缺陷 C）
# --------------------------------------------------------------------------

def _failed_result(engine_error: dict[str, Any] | str | None,
                   conclusion_error: str | None) -> SimpleNamespace:
    """对齐账本里 engine_error 的真实形态：适配器把结构化 JSON 序列化后整段落账本。"""
    return SimpleNamespace(
        conclusion=None, conclusion_error=conclusion_error,
        engine_error=json.dumps(engine_error, ensure_ascii=False)
        if isinstance(engine_error, dict) else engine_error,
        permission_denials=[], reason=None, events=[],
        validation=validation.ValidationReport(validation.Verdict.PASS, []),
        api_error_status=(engine_error or {}).get("api_error_status")
        if isinstance(engine_error, dict) else None,
    )


async def case_ratelimit_regex(root: Path, checks: Checks, budget: float) -> dict[str, Any]:
    del budget
    section_path = root / "runs" / "r-seam-regex" / "goals/goal-3/consistency-check/sec-2.md"
    section_path.parent.mkdir(parents=True, exist_ok=True)
    section_path.write_text("## 结论\n\n- 草稿 [S01]\n\n## 信息源\n\n- [S01] https://example.com/x\n",
                            encoding="utf-8")

    non_ratelimit = _failed_result(NON_RATELIMIT_ENGINE_ERROR, NON_RATELIMIT_CONCLUSION_ERROR)
    reason_non_ratelimit = chapter_failure_reason(non_ratelimit, section_path)
    checks.add("C1 「非限流错误」原文（api_error_status=null）不得归 quota_exhausted",
               expected="!= quota_exhausted（按结构化字段应为 timeout / retry_exhausted / conclusion_invalid）",
               actual={"reason": reason_non_ratelimit,
                       "api_error_status": non_ratelimit.api_error_status,
                       "engine_error_result": NON_RATELIMIT_ENGINE_ERROR["result"]},
               passed=reason_non_ratelimit != "quota_exhausted")

    real_ratelimit = _failed_result(REAL_RATELIMIT_ENGINE_ERROR, None)
    reason_real = chapter_failure_reason(real_ratelimit, section_path)
    checks.add("guard: 真限流（api_error_status=429）仍归 quota_exhausted", guard=True,
               expected="quota_exhausted", actual={"reason": reason_real, "api_error_status": 429},
               passed=reason_real == "quota_exhausted")

    timeout_only = _failed_result("夹具注入 engine_error：引擎调用超时", "夹具注入：结构化结论未生成")
    reason_timeout = chapter_failure_reason(timeout_only, section_path)
    checks.add("guard: 纯超时原文仍归 timeout（采集夹具 E1 口径不回退）", guard=True,
               expected="timeout", actual={"reason": reason_timeout},
               passed=reason_timeout == "timeout")
    return {"non_ratelimit": {"engine_error": NON_RATELIMIT_ENGINE_ERROR,
                              "conclusion_error": NON_RATELIMIT_CONCLUSION_ERROR,
                              "reason": reason_non_ratelimit},
            "real_ratelimit": {"engine_error": REAL_RATELIMIT_ENGINE_ERROR, "reason": reason_real},
            "timeout": {"reason": reason_timeout}}


# --------------------------------------------------------------------------
# case stop-resume（缺陷 B）：走 API
# --------------------------------------------------------------------------

RESUME_RETURN_BUDGET_SECONDS = 10.0
RESUME_TERMINAL_BUDGET_SECONDS = 30.0


async def _wait_until(predicate: Any, timeout: float, step: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(step)
    return bool(predicate())


async def case_stop_resume(root: Path, checks: Checks, budget: float) -> dict[str, Any]:
    import httpx
    from app.api.main import create_app

    rid = "r-seam-stopresume"
    adapter = FakeAdapter()
    adapter.block_agent_id = "data-collection"  # goal-1/ch-1 执行中挂起，等夹具放行
    app = create_app(root / "owli.db", SCHEMA, engine_probe=lambda: {},
                     adapter_factory=lambda: adapter, runs_root=root / "runs",
                     auto_confirm=True)
    timeline: list[dict[str, Any]] = []
    t0 = time.monotonic()

    def mark(step: str, **extra: Any) -> None:
        timeline.append({"t": round(time.monotonic() - t0, 3), "step": step, **extra})
        print(f"  [{timeline[-1]['t']:6.2f}s] {step} {json.dumps(extra, ensure_ascii=False)[:200]}",
              flush=True)

    async with app.router.lifespan_context(app):
        store: Store = app.state.store
        runtime: RuntimeCoordinator = app.state.runtime
        plan = plan_stop_resume(rid)
        plan.status, plan.approved_at = "awaiting_review", None
        store.create_report(id=rid, title=QUERY, research_question=QUERY, created_at=_now(),
                            use_case="product_competitor", status="running",
                            extra={"scale": SCALE, "fixture": "seam"})
        save_plan(store, plan, expected_rev=0)
        app.state.researches[rid] = runtime._state_from_plan(plan)
        runtime._adapters[rid] = adapter
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://seam",
                                     timeout=budget) as client:
            headers = lambda tag: {"X-Request-ID": f"seam-{tag}"}  # noqa: E731
            approved = await client.post(f"/api/researches/{rid}/plan/approve", headers=headers("approve"))
            mark("POST /plan/approve", status_code=approved.status_code, body=approved.text[:300])
            start_path = "api:/plan/approve"
            if approved.status_code != 200:
                # lint 拦住手工计划时退回直接起调度（仍由 API 做 stop/resume，不影响缺陷 B 的复现面）
                start_path = "runtime.start_research(background)"
                approved_plan = Plan.from_dict({**plan.to_dict(), "status": "approved",
                                                "approved_at": _now()})
                app.state.background_tasks.add(asyncio.create_task(
                    runtime.start_research(approved_plan)))
            started = await asyncio.wait_for(adapter.started.wait(), timeout=budget)
            del started
            mark("goal-1/ch-1 进行中（假适配器已挂起）")
            scheduler = runtime.scheduler_for(rid)

            stopped = await client.post(f"/api/researches/{rid}/stop", headers=headers("stop"))
            stop_body = stopped.json()
            scheduler_status_after_stop = None if scheduler is None else scheduler.status
            mark("POST /stop", status_code=stopped.status_code,
                 api_status=stop_body.get("data", {}).get("status"),
                 scheduler_status=scheduler_status_after_stop)
            adapter.release.set()  # 放行挂起的章；stop 之后调度循环应退出
            drained = await _wait_until(lambda: scheduler is not None and not scheduler._tasks, 10.0)
            mark("在跑章已返回，驱动循环退出", drained=drained,
                 scheduler_status=None if scheduler is None else scheduler.status)

            resume_t0 = time.monotonic()
            resumed = await client.post(f"/api/researches/{rid}/resume", headers=headers("resume"))
            resume_seconds = time.monotonic() - resume_t0
            resume_body = resumed.json()
            api_status_after_resume = resume_body.get("data", {}).get("status")
            scheduler_status_after_resume = None if scheduler is None else scheduler.status
            mark("POST /resume 返回", status_code=resumed.status_code, seconds=round(resume_seconds, 3),
                 api_status=api_status_after_resume, scheduler_status=scheduler_status_after_resume)

            reached_terminal = await _wait_until(
                lambda: scheduler is not None and scheduler.status == "completed",
                RESUME_TERMINAL_BUDGET_SECONDS)
            snapshot = (await client.get(f"/api/researches/{rid}")).json()["data"]
            ledger = store.list_chapters(rid)
            scheduler_status_final = None if scheduler is None else scheduler.status
            mark("等待终态结束", reached_terminal=reached_terminal,
                 scheduler_status=scheduler_status_final,
                 api_status=snapshot.get("status"),
                 ledger={f"{r['goal_id']}/{r['chapter_id']}": r["status"] for r in ledger})
            if scheduler is not None and scheduler.status != "completed":
                await scheduler.stop()
        for task in list(app.state.background_tasks):
            if not task.done():
                task.cancel()
        await asyncio.gather(*app.state.background_tasks, return_exceptions=True)

    checks.add("guard: /stop 返回 200 且 API 与调度器同时为 stopped", guard=True,
               expected={"status_code": 200, "api": "stopped", "scheduler": "stopped"},
               actual={"status_code": stopped.status_code,
                       "api": stop_body.get("data", {}).get("status"),
                       "scheduler": scheduler_status_after_stop},
               passed=(stopped.status_code == 200 and stop_body.get("data", {}).get("status") == "stopped"
                       and scheduler_status_after_stop == "stopped"))
    checks.add(f"guard: /resume 在 {RESUME_RETURN_BUDGET_SECONDS:.0f}s 内返回（不阻塞到整轮结束）",
               guard=True, expected=f"<= {RESUME_RETURN_BUDGET_SECONDS}s 且 200",
               actual={"seconds": round(resume_seconds, 3), "status_code": resumed.status_code},
               passed=(resume_seconds <= RESUME_RETURN_BUDGET_SECONDS and resumed.status_code == 200))
    checks.add("B1 /resume 回报的状态与调度器实际状态一致",
               expected="api.status == scheduler.status（running↔running / stopped↔stopped）",
               actual={"api": api_status_after_resume, "scheduler": scheduler_status_after_resume},
               passed=(api_status_after_resume == scheduler_status_after_resume))
    checks.add(f"B2 /resume 后研究继续并在 {RESUME_TERMINAL_BUDGET_SECONDS:.0f}s 内到终态（scheduler completed，两章 done）",
               expected={"scheduler": "completed", "ledger": {"goal-1/ch-1": "done", "goal-2/ch-1": "done"}},
               actual={"reached_terminal": reached_terminal,
                       "scheduler": scheduler_status_final,
                       "ledger": {f"{r['goal_id']}/{r['chapter_id']}": r["status"] for r in ledger},
                       "api": snapshot.get("status")},
               passed=(reached_terminal and all(r["status"] == "done" for r in ledger)))
    checks.add("B3 终态后 API 状态与调度器一致（不得 API running / 调度器 stopped）",
               expected="scheduler completed ⇒ api ∈ {completed, failed}；否则 api == scheduler",
               actual={"api": snapshot.get("status"), "scheduler": scheduler_status_final},
               passed=((scheduler_status_final == "completed"
                        and snapshot.get("status") in {"completed", "failed"})
                       or snapshot.get("status") == scheduler_status_final))
    return {"research_id": rid, "start_path": start_path, "timeline": timeline,
            "resume_seconds": round(resume_seconds, 3), "resume_response": resume_body,
            "final_snapshot_status": snapshot.get("status"), "ledger": ledger,
            "adapter_calls": adapter.calls}


# --------------------------------------------------------------------------
# case section-transport-jitter（缺陷 E，D-007）
# --------------------------------------------------------------------------

# 6b 整跑账本 engine_error 逐字（runs/6b-final/ledger-final.txt，7 处 retry_exhausted 全同此文案）
TRANSPORT_ENGINE_ERROR = {
    "is_error": True,
    "api_error_status": None,
    "subtype": "error_during_execution",
    "result": "API Error: The socket connection was closed unexpectedly",
    "errors": ["API Error: The socket connection was closed unexpectedly"],
}
SECTION_AGENT = "report-writing"
SECTION_2 = f"{SECTION_AGENT}-sec-2"
SECTION_2_CONCLUSION_RETRY = f"{SECTION_2}-conclusion-retry"
SECTION_2_HEADING = "竞品对比"
JITTER_PER_ROUND = 3


def _transport_failure(task: Any, ctx: Any, engine_error: dict[str, Any]) -> EngineRunResult:
    """传输断连：无产物、无结论、engine_error=结构化 JSON 整段落账本（对齐 claude 适配器）。"""
    del task, ctx
    return EngineRunResult(
        conclusion=None, conclusion_error=None,
        validation=validation.ValidationReport(validation.Verdict.PASS, []), events=[],
        permission_denials=[], engine_error=json.dumps(engine_error, ensure_ascii=False),
    )


def _write_section_2(task: Any) -> None:
    task.output_path.parent.mkdir(parents=True, exist_ok=True)
    task.output_path.write_text(
        f"## {SECTION_2_HEADING}\n\n- 讯飞 23 种方言 vs 豆包 8 种 [S01]\n\n"
        "## 结论\n\n- 讯飞输入法方言覆盖领先豆包 [S01]\n\n"
        "## 信息源\n\n- [S01] [讯飞方言实测](https://example.com/iflytek-dialect)\n",
        encoding="utf-8")


def plan_section_jitter(rid: str, *, per_round: int, max_rounds: int,
                        report_validators: list[str]) -> Plan:
    """两 goal：goal-1 采集 done；goal-2 节化报告章（sec-1←goal-1，sec-2←goal-2）。"""
    g1 = _goal("goal-1", "竞品基准信息采集", [collection_agent("goal-1", 1)], [])
    g2 = _goal("goal-2", "竞品分析报告", [_agent(
        "goal-2", SECTION_AGENT, display="报告章（report-writer，节化）",
        kind_profile="report-writer", engine="claude", fmt="markdown",
        path="goals/goal-2/report.md", validators=report_validators,
        cid="ch-1", ctype="report", inputs=("goals/goal-1/data-collection.json",))], ["goal-1"])
    g2["retry_policy"] = {**DEFAULT_RETRY_POLICY, "max_attempts_per_round": per_round,
                          "max_rounds": max_rounds}
    return build_plan(rid, [g1, g2])


def _section_rows(store: Store, rid: str) -> dict[str, dict[str, Any]]:
    return {r["chapter_id"]: dict(r) for r in store.list_chapters(rid) if r["goal_id"] == "goal-2"}


def _row_digest(row: dict[str, Any] | None) -> dict[str, Any]:
    if row is None:
        return {"status": None}
    return {k: row.get(k) for k in ("status", "reason", "attempts", "engine")}


async def _run_jitter(root: Path, rid: str, budget: float, *, per_round: int = JITTER_PER_ROUND,
                      max_rounds: int = 1, report_validators: list[str] | None = None,
                      sec2: Any) -> dict[str, Any]:
    """起一次不真跑引擎的整研究；sec2(task, ctx, chapter_attempt) 决定 sec-2 每次派活的结果。"""
    store = make_store(root, rid)
    plan = plan_section_jitter(rid, per_round=per_round, max_rounds=max_rounds,
                               report_validators=report_validators
                               or ["file_exists", "sections_exist:结论,信息源"])
    adapter = FakeAdapter()
    published: list[dict[str, Any]] = []
    coordinator = make_coordinator(store, root, rid, plan, adapter, published)
    chapter_attempt = {"current": 0}
    original_run_task = coordinator._run_task

    async def tracking_run_task(plan_: Plan, agent: Any, context: Any) -> Any:
        if agent.agent_id == SECTION_AGENT:
            chapter_attempt["current"] = int(getattr(context, "attempt", 0) or 0)
        return await original_run_task(plan_, agent, context)

    coordinator._run_task = tracking_run_task  # 仅夹具实例，不改产品代码
    sec2_calls: list[dict[str, Any]] = []

    async def sec2_behavior(task: Any, ctx: Any) -> EngineRunResult:
        serial = len(sec2_calls) + 1
        sec2_calls.append({"serial": serial, "agent_id": task.agent_id,
                           "chapter_attempt": chapter_attempt["current"], "at": _now()})
        return await sec2(task, ctx, serial, chapter_attempt["current"])

    adapter.behaviors[SECTION_2] = sec2_behavior
    adapter.behaviors[SECTION_2_CONCLUSION_RETRY] = sec2_behavior
    timed_out = await run_to_terminal(coordinator, plan, budget)
    scheduler = coordinator.scheduler_for(rid)
    rows = _section_rows(store, rid)
    return {
        "research_id": rid, "timed_out": timed_out,
        "scheduler": None if scheduler is None else scheduler.status,
        "goals": {} if scheduler is None else dict(scheduler.goal_statuses),
        "chapter": rows.get("ch-1"), "sec1": rows.get("ch-1/sec-1"), "sec2": rows.get("ch-1/sec-2"),
        "sec2_calls": sec2_calls, "adapter_calls": adapter.calls,
        "section_errors": [e["data"] for e in published if e.get("type") == "section_error"],
    }


async def _sec2_fail_then_ok(task: Any, ctx: Any, serial: int, chapter_attempt: int) -> EngineRunResult:
    del chapter_attempt
    if serial == 1:
        return _transport_failure(task, ctx, TRANSPORT_ENGINE_ERROR)
    _write_section_2(task)
    return _done(task, ctx)


async def _sec2_always_fail(task: Any, ctx: Any, serial: int, chapter_attempt: int) -> EngineRunResult:
    del serial, chapter_attempt
    return _transport_failure(task, ctx, TRANSPORT_ENGINE_ERROR)


async def _sec2_fail_in_round_1(task: Any, ctx: Any, serial: int, chapter_attempt: int) -> EngineRunResult:
    """章级第 1 次尝试内无论节级重试几次都断连；第 2 次（第二轮）派活即成功。"""
    del serial
    if chapter_attempt <= 1:
        return _transport_failure(task, ctx, TRANSPORT_ENGINE_ERROR)
    _write_section_2(task)
    return _done(task, ctx)


async def _sec2_conclusion_invalid(task: Any, ctx: Any, serial: int, chapter_attempt: int) -> EngineRunResult:
    """产物已写好但 owli-result 不合法：非传输类失败，只走既有「重发结论块」一次，不得退避重试。"""
    del serial, chapter_attempt
    _write_section_2(task)
    return EngineRunResult(
        conclusion=None, conclusion_error=NON_RATELIMIT_CONCLUSION_ERROR,
        validation=_pass_report(task, ctx), events=[], permission_denials=[],
    )


async def _sec2_real_429(task: Any, ctx: Any, serial: int, chapter_attempt: int) -> EngineRunResult:
    del serial, chapter_attempt
    return _transport_failure(task, ctx, REAL_RATELIMIT_ENGINE_ERROR)


async def case_section_transport_jitter(root: Path, checks: Checks, budget: float) -> dict[str, Any]:
    # 子例 1（defect）：第 1 次断连、第 2 次成功 → 节 done、attempts=2、引擎不变
    one = await _run_jitter(root, "r-seam-jitter-1", budget, sec2=_sec2_fail_then_ok)
    sec2 = one["sec2"] or {}
    checks.add("guard: 子例 1 研究到终态且 sec-1 done（断连只影响 sec-2）", guard=True,
               expected={"scheduler": "completed", "sec1": "done"},
               actual={"scheduler": one["scheduler"], "goals": one["goals"],
                       "sec1": _row_digest(one["sec1"]), "timed_out": one["timed_out"]},
               passed=(one["scheduler"] == "completed" and (one["sec1"] or {}).get("status") == "done"
                       and not one["timed_out"]))
    checks.add("E1 一次传输断连后节级重试一次即 done（attempts=2，engine 不变）",
               expected={"status": "done", "reason": None, "attempts": 2, "engine": "claude"},
               actual={**_row_digest(sec2), "sec2_calls": len(one["sec2_calls"]),
                       "engine_error": sec2.get("engine_error")},
               passed=(sec2.get("status") == "done" and sec2.get("reason") is None
                       and sec2.get("attempts") == 2 and sec2.get("engine") == "claude"
                       and len(one["sec2_calls"]) == 2))
    checks.add("guard: 断连注入确实命中 classify_transport_error（不是被归成 timeout/quota）", guard=True,
               expected="首个 section_error.reason ∈ {retry_exhausted}（或修复后无 section_error）",
               actual={"section_errors": [e.get("reason") for e in one["section_errors"]]},
               passed=all(e.get("reason") == "retry_exhausted" for e in one["section_errors"]))

    # 子例 2：连续 N=max_attempts_per_round 次断连 → 真耗尽才 missing/retry_exhausted
    n = JITTER_PER_ROUND
    two = await _run_jitter(root, "r-seam-jitter-2", budget, per_round=n, sec2=_sec2_always_fail)
    sec2b = two["sec2"] or {}
    checks.add(f"guard: 连续断连 → 节 missing/retry_exhausted、engine 不变、attempts≤N={n}、研究仍到终态",
               guard=True,
               expected={"status": "missing", "reason": "retry_exhausted", "engine": "claude",
                         "attempts<=": n, "scheduler": "completed"},
               actual={**_row_digest(sec2b), "sec2_calls": len(two["sec2_calls"]),
                       "scheduler": two["scheduler"], "timed_out": two["timed_out"]},
               passed=(sec2b.get("status") == "missing" and sec2b.get("reason") == "retry_exhausted"
                       and sec2b.get("engine") == "claude" and (sec2b.get("attempts") or 0) <= n
                       and len(two["sec2_calls"]) <= n and two["scheduler"] == "completed"
                       and not two["timed_out"]))
    checks.add(f"E2 retry_exhausted 必须真耗尽：attempts == N={n} 且 sec-2 被派活 N 次",
               expected={"attempts": n, "sec2_calls": n},
               actual={"attempts": sec2b.get("attempts"), "sec2_calls": len(two["sec2_calls"])},
               passed=(sec2b.get("attempts") == n and len(two["sec2_calls"]) == n))

    # 子例 3（defect）：章级第二轮重试要重新派活 missing/retry_exhausted 的节
    # 父章 validators 要求 sec-2 成功内容里的标题，于是第一轮拼装校验失败 → 触发章级重试
    three = await _run_jitter(
        root, "r-seam-jitter-3", budget, per_round=1, max_rounds=2,
        report_validators=["file_exists", f"sections_exist:结论,信息源,{SECTION_2_HEADING}"],
        sec2=_sec2_fail_in_round_1)
    sec2c = three["sec2"] or {}
    chapter = three["chapter"] or {}
    round2_calls = [c for c in three["sec2_calls"] if c["chapter_attempt"] >= 2]
    checks.add("guard: 子例 3 章级确实进入第二轮（父章 attempts>=2）且研究到终态", guard=True,
               expected={"chapter.attempts>=": 2, "scheduler": "completed"},
               actual={"chapter": _row_digest(chapter), "scheduler": three["scheduler"],
                       "timed_out": three["timed_out"]},
               passed=((chapter.get("attempts") or 0) >= 2 and three["scheduler"] == "completed"
                       and not three["timed_out"]))
    checks.add("E3 章级第二轮重新派活 missing/retry_exhausted 的 sec-2 并 done（父章 done）",
               expected={"sec2": "done", "sec2_round2_calls>=": 1, "chapter": "done"},
               actual={"sec2": _row_digest(sec2c), "chapter": _row_digest(chapter),
                       "sec2_calls": three["sec2_calls"]},
               passed=(sec2c.get("status") == "done" and len(round2_calls) >= 1
                       and chapter.get("status") == "done"))

    # guard：非传输类失败（conclusion_invalid）不被退避重试，行为不变（只走既有结论块重发一次）
    four = await _run_jitter(root, "r-seam-jitter-4", budget, sec2=_sec2_conclusion_invalid)
    sec2d = four["sec2"] or {}
    call_ids = [c["agent_id"] for c in four["sec2_calls"]]
    checks.add("guard: conclusion_invalid 不走传输重试：attempts=1、只多一次结论块重发、reason=conclusion_invalid",
               guard=True,
               expected={"status": "missing", "reason": "conclusion_invalid", "attempts": 1,
                         "calls": [SECTION_2, SECTION_2_CONCLUSION_RETRY]},
               actual={**_row_digest(sec2d), "calls": call_ids, "scheduler": four["scheduler"]},
               passed=(sec2d.get("status") == "missing" and sec2d.get("reason") == "conclusion_invalid"
                       and sec2d.get("attempts") == 1
                       and call_ids == [SECTION_2, SECTION_2_CONCLUSION_RETRY]))

    # guard：真 429（api_error_status=429）仍归 quota_exhausted，不得归 retry_exhausted
    five = await _run_jitter(root, "r-seam-jitter-5", budget, sec2=_sec2_real_429)
    sec2e = five["sec2"] or {}
    checks.add("guard: 真限流 429 的节仍 quota_exhausted（不归 retry_exhausted），engine 不变", guard=True,
               expected={"status": "missing", "reason": "quota_exhausted", "engine": "claude"},
               actual={**_row_digest(sec2e), "sec2_calls": len(five["sec2_calls"]),
                       "scheduler": five["scheduler"]},
               passed=(sec2e.get("status") == "missing" and sec2e.get("reason") == "quota_exhausted"
                       and sec2e.get("engine") == "claude"))
    return {"sub1": one, "sub2": two, "sub3": three, "guard_conclusion_invalid": four,
            "guard_429": five, "transport_engine_error": TRANSPORT_ENGINE_ERROR}


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------

CASES = {
    "finalize-json-report": case_finalize_json_report,
    "partial-done": case_partial_done,
    "ratelimit-regex": case_ratelimit_regex,
    "stop-resume": case_stop_resume,
    "section-transport-jitter": case_section_transport_jitter,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Owli 接缝夹具（A/B/C/D 造红）")
    parser.add_argument("outdir")
    parser.add_argument("--case", required=True, choices=sorted(CASES))
    parser.add_argument("--budget-seconds", type=float, default=120.0)
    args = parser.parse_args()

    root = Path(args.outdir)
    root.mkdir(parents=True, exist_ok=True)
    print(f"=== 接缝夹具 case={args.case} 输出目录={root}\n", flush=True)
    checks = Checks()
    started_at = time.time()
    outcome = asyncio.run(CASES[args.case](root, checks, args.budget_seconds))
    total_seconds = round(time.time() - started_at, 2)

    summary = {
        "case": args.case,
        "total_seconds": total_seconds,
        "checks": checks.items,
        "failed": checks.failed_names(),
        "outcome": outcome,
    }
    (root / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    print(f"\n=== 耗时 {total_seconds}s；明细已落盘 {root / 'summary.json'}")
    print("=== 结论：" + ("全部通过" if not summary["failed"] else f"未过 {summary['failed']}"))
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
