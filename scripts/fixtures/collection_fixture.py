"""采集模块夹具：手工构造只含采集章的 fast 计划，走真实源适配器 + 真实引擎。

用法：
  cd Owli-m3h
  ../Owli/.venv/bin/python scripts/fixtures/collection_fixture.py <输出目录> [--case main|inject-engine-error]

不经规划期：3 个竞品 × (web_search, x) = 6 个采集章，三 goal 各两章，全并行。
断言见 __main__ 末尾 _assert_* 系列；结论落 <输出目录>/summary.json 与终端表格。
已知坑（沿用 report_sectioning_fixture）：capability.profile 必须在闭集内；
event_buffer.publish 必须是 async。
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

from app.adapters.events import ItemKind, NormalizedEvent
from app.config import load_research_scale_config
from app.orchestrator.runtime import RuntimeCoordinator
from app.plan.generate import _agent_prompt, _capability, _output
from app.plan.model import DEFAULT_RETRY_POLICY, Plan
from app.plan.store import save_plan
from app.store.dao import Store


RID = "r-collect-fixture"
QUERY = "豆包语音输入法的竞品分析"
COMPETITORS = ["讯飞输入法", "搜狗输入法", "百度输入法"]
SOURCES = [("web_search", "网页搜索数据抓取"), ("x", "X 数据抓取")]
HN_SOURCE = [("hacker_news", "HN 数据抓取")]
TERMINAL = {"done", "missing", "deferred"}
REASONS = {
    "empty_result", "tool_unavailable", "quota_exhausted", "retry_exhausted",
    "timeout",
}
SCALE = "fast"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _agent(goal_id: str, index: int, serial: int, competitor: str, source_id: str,
           display: str, scale_profile: Any) -> dict[str, Any]:
    # agent_id 必须全局唯一：Scheduler 的 _agents/agent_statuses 以 agent_id 为键，
    # 跨 goal 重名会让后面的 goal 整段不被派活（本夹具第一轮实测）。
    agent_id = "data-collection" if serial == 1 else f"data-collection-{serial}"
    task = (
        f"采集 {competitor} 在语音输入/语音转写方向的公开资料与用户反馈，"
        f"每条保留 permalink 与 fetched_at。"
    )
    output = _output("data_collection", goal_id, agent_id, "array")
    return {
        "agent_id": agent_id,
        "display_name": display,
        "task": task[:200],
        "depends_on": [],
        "inputs": [],
        "engine": "codex",
        "model": None,
        "capability": _capability("web-collector", goal_id, [], source_id=source_id),
        "prompt": {
            "preamble_ref": "common/v1",
            "body": _agent_prompt(
                f"{competitor} 语音输入",
                task,
                output,
                "data_collection",
                source_id=source_id,
                source_item_limit=scale_profile.source_item_limits.get(source_id),
                scale=SCALE,
            ),
            "assumptions_policy": "assume_and_declare",
        },
        "output": output,
        "chapter": {
            "chapter_id": f"ch-{index}",
            "chapter_type": "collection",
            "plan_path": f"goals/{goal_id}/ch-{index}.md",
            "opening": {
                "inputs": [],
                "task": task,
                "acceptance": [
                    "顶层为 JSON 数组且至少 1 条",
                    "每条带 permalink 与 fetched_at",
                ],
            },
            "closing": {
                "output": {"path": output["path"]},
                "entities": [competitor],
                "expected_count": None,
                "notes": {"source": source_id},
            },
        },
        "extra_quota_credits": None,
        "origin": {"_node": "generated"},
        "status": "queued",
    }


def _goal(number: int, competitor: str, scale_profile: Any,
          sources: list[tuple[str, str]]) -> dict[str, Any]:
    goal_id = f"goal-{number}"
    agents = [
        _agent(goal_id, index, (number - 1) * len(sources) + index,
               competitor, source_id, display, scale_profile)
        for index, (source_id, display) in enumerate(sources, start=1)
    ]
    retry_policy = dict(DEFAULT_RETRY_POLICY)
    if scale_profile.chapter_wall_clock_seconds is not None:
        retry_policy["chapter_deadline_seconds"] = scale_profile.chapter_wall_clock_seconds
    return {
        "goal_id": goal_id,
        "title": f"{competitor}采集"[:24],
        "objective": f"采集 {competitor} 语音输入相关公开资料，供后续对标消费。",
        "depends_on": [],
        "deliverable": {
            "format": "json",
            "shape": "array",
            "path": agents[0]["output"]["path"],
            "description": f"{competitor} 语音输入公开资料条目。",
        },
        "acceptance": ["产物为 JSON 数组且每条带 permalink 与 fetched_at"],
        "intervention": {"on_complete": True, "prompt": f"请核对《{competitor}采集》产物，是否继续？"},
        "retry_policy": retry_policy,
        "on_upstream_failure": "skip",
        "agents": agents,
        "status": "pending",
    }


def build_plan(case: str) -> Plan:
    profile = load_research_scale_config().profile(SCALE)
    if case in {
        "inject-429", "inject-429-twice", "inject-engine-error", "inject-timeout",
    }:
        goals = [_goal(1, COMPETITORS[0], profile, SOURCES[:1])]
    elif case == "empty-hn":
        # 真实零命中：英文社区源对中文输入法题目全空（m3-h §七 C9 同款），
        # 用来真实触发 empty_result，不做任何注入。
        goals = [_goal(1, COMPETITORS[0], profile, HN_SOURCE)]
    else:
        goals = [
            _goal(number, competitor, profile, SOURCES)
            for number, competitor in enumerate(COMPETITORS, start=1)
        ]
    return Plan.from_dict({
        "research_id": RID,
        "plan_rev": 1,
        "title": QUERY,
        "research_question": QUERY,
        "use_case": "product_competitor",
        "market_profile": "cn_product",
        "market_profile_justification": "题目与竞品均为中文市场输入法产品。",
        "scale": SCALE,
        "status": "approved",
        "approved_at": _now(),
        "decision_balance": [{
            "q_id": "q-1",
            "question": "本次优先服务哪类判断？",
            "options": ["产品路线与功能取舍", "市场话术"],
            "input_type": "single",
            "answer": "产品路线与功能取舍",
            "affects": ["goal-1"],
            "answered_at": _now(),
        }],
        "expert_panel": None,
        "goals": goals,
        "change_log": [],
        "baseline": None,
        "baseline_source": "generated",
        "created_at": _now(),
        "updated_at": _now(),
    })


# --------------------------------------------------------------------------
# 运行时装配
# --------------------------------------------------------------------------

def make_store(root: Path) -> Store:
    database = root / "owli.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(Path("app/store/schema.sql").read_text(encoding="utf-8"))
    store = Store(database)
    store.create_report(
        id=RID, title=QUERY, research_question=QUERY,
        created_at=_now(), use_case="product_competitor",
        extra={"scale": SCALE},
    )
    return store


class Recorder:
    """把每次 adapter.run 的真实判定原样留档，供死因取证。"""

    def __init__(self, started_at: float) -> None:
        self.started_at = started_at
        self.attempts: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []

    def elapsed(self) -> float:
        return time.time() - self.started_at

    def wrap(
        self,
        adapter: Any,
        inject_rate_limit: int,
        *,
        inject_engine_error: bool = False,
        inject_timeout: bool = False,
    ) -> None:
        """在 adapter.run 边界注入确定性失败；注入分支不启动真实引擎。"""

        original = adapter.run
        injected: dict[str, int] = {}

        async def run(task: Any, ctx: Any, on_event: Any = None) -> Any:
            key = f"{task.goal_id}/{task.agent_id}"
            begin = self.elapsed()
            if inject_engine_error or inject_timeout:
                engine_error = (
                    "夹具注入 engine_error：确定性启动失败"
                    if inject_engine_error
                    else "夹具注入 engine_error：引擎调用超时"
                )
                result = SimpleNamespace(
                    conclusion=None,
                    conclusion_error="夹具注入：结构化结论未生成",
                    validation=SimpleNamespace(verdict=None, results=()),
                    events=[],
                    permission_denials=[],
                    engine_error=engine_error,
                    succeeded=False,
                )
                self._record(
                    key,
                    begin,
                    engine=task.user_override,
                    note=(
                        "注入 engine_error（未起引擎）"
                        if inject_engine_error
                        else "注入 timeout（未起引擎）"
                    ),
                    result=result,
                )
                return result
            if injected.get(key, 0) < inject_rate_limit:
                injected[key] = injected.get(key, 0) + 1
                event = NormalizedEvent(
                    engine=task.user_override or "codex", thread_id=None, turn_id=None,
                    item_kind=ItemKind.ERROR,
                    text="[注入] HTTP 429 rate limit（夹具注入，未调用真实引擎）",
                    is_error=True, raw={"http_status": 429}, cause="rate_limit",
                )
                if on_event is not None:
                    await on_event(event)
                self._record(key, begin, engine=task.user_override,
                             note="注入 429（未起引擎）", result=None)
                return SimpleNamespace(
                    conclusion=None, conclusion_error=None,
                    validation=SimpleNamespace(verdict=None, results=()),
                    events=[event], permission_denials=[],
                    engine_error=None, succeeded=False,
                )
            result = await original(task, ctx, on_event=on_event)
            self._record(key, begin, engine=task.user_override, note=None, result=result)
            return result

        adapter.run = run

    def _record(self, key: str, begin: float, *, engine: Any, note: str | None,
                result: Any) -> None:
        conclusion = getattr(result, "conclusion", None)
        validation_report = getattr(result, "validation", None)
        failures = [
            f"{getattr(item, 'name', '')}: {getattr(item, 'message', '')}"
            for item in (getattr(validation_report, "results", ()) or ())
            if str(getattr(getattr(item, "verdict", ""), "value", "")).lower() not in {"pass", ""}
        ]
        entry = {
            "chapter": key,
            "engine": engine,
            "started_s": round(begin, 1),
            "ended_s": round(self.elapsed(), 1),
            "duration_s": round(self.elapsed() - begin, 1),
            "note": note,
            "succeeded": bool(getattr(result, "succeeded", False)) if result is not None else False,
            "engine_error": getattr(result, "engine_error", None),
            "conclusion_error": getattr(result, "conclusion_error", None),
            "conclusion_status": getattr(conclusion, "status", None),
            "conclusion_reason": getattr(conclusion, "reason", None),
            "conclusion_unmet": list(getattr(conclusion, "unmet", None) or []),
            "permission_denials": list(getattr(result, "permission_denials", None) or []),
            "validation_verdict": str(getattr(getattr(validation_report, "verdict", None), "value", "")),
            "validation_failures": failures[:4],
        }
        self.attempts.append(entry)
        print(
            f"[{entry['ended_s']:7.1f}s] 章 {key} 引擎={entry['engine']} "
            f"耗时={entry['duration_s']}s succeeded={entry['succeeded']} "
            f"status={entry['conclusion_status']} reason={entry['conclusion_reason']} "
            f"engine_error={str(entry['engine_error'])[:120]!r} "
            f"conclusion_error={str(entry['conclusion_error'])[:120]!r} "
            f"denials={entry['permission_denials']} "
            f"validation={entry['validation_verdict']} {entry['validation_failures']}"
            + (f" 备注={note}" if note else ""),
            flush=True,
        )


async def run_case(root: Path, case: str, budget_seconds: float) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    store = make_store(root)
    plan = build_plan(case)
    save_plan(store, plan, expected_rev=0)

    # D3 回归：产物根刻意注入到调用方的 /tmp 输出目录，不依赖仓内默认 runs/。
    runs_root = root / "runs"
    published: list[dict[str, Any]] = []
    started_at = time.time()
    recorder = Recorder(started_at)

    async def publish(research_id: str, payload: Any) -> None:
        event = payload if isinstance(payload, dict) else {"type": str(payload)}
        published.append(event)
        kind = event.get("type")
        if kind in {"chapter_update", "goal_gate", "route_update", "agent_error"}:
            print(f"[{recorder.elapsed():7.1f}s] «{kind}» "
                  f"{json.dumps(event.get('data'), ensure_ascii=False)[:260]}", flush=True)
        elif kind == "normalized_event" and event.get("data", {}).get("is_error"):
            print(f"[{recorder.elapsed():7.1f}s] «错误事件» "
                  f"{str(event['data'].get('text'))[:200]}", flush=True)

    coordinator = RuntimeCoordinator(
        store=store,
        event_buffer=SimpleNamespace(publish=publish),
        researches={},
        cards={},
        runs_root=runs_root,
        auto_confirm=True,
        routing_utc_clock=lambda: datetime.now(timezone.utc),
    )
    coordinator.researches[RID] = coordinator._state_from_plan(plan)
    adapter = coordinator.adapter_factory()
    coordinator._adapters[RID] = adapter
    recorder.wrap(adapter, inject_rate_limit={
        "inject-429": 1, "inject-429-twice": 2,
    }.get(case, 0), inject_engine_error=case == "inject-engine-error",
        inject_timeout=case == "inject-timeout")

    timed_out = False
    try:
        await asyncio.wait_for(coordinator.start_research(plan), timeout=budget_seconds)
    except asyncio.TimeoutError:
        timed_out = True
        print(f"\n!! 超过夹具预算 {budget_seconds:.0f}s，停止调度并按当前账本判定", flush=True)
        scheduler = coordinator.scheduler_for(RID)
        if scheduler is not None:
            await scheduler.stop()
    # 产品侧已在 start_research 返回前排干；这里保留双保险，防止夹具漏取尾事件。
    for _ in range(20):
        pending = [item for item in coordinator._auto_tasks if not item.done()]
        if not pending:
            break
        await asyncio.wait(pending, timeout=10)
    total_seconds = time.time() - started_at

    scheduler = coordinator.scheduler_for(RID)
    ledger = store.list_chapters(RID)
    return {
        "case": case,
        "timed_out": timed_out,
        "total_seconds": round(total_seconds, 1),
        "scheduler_status": None if scheduler is None else scheduler.status,
        "goal_statuses": {} if scheduler is None else dict(scheduler.goal_statuses),
        "agent_statuses": {} if scheduler is None else dict(scheduler.agent_statuses),
        "ledger": ledger,
        "attempts": recorder.attempts,
        "chapter_updates": [
            event["data"] for event in published if event.get("type") == "chapter_update"
        ],
        "normalized_events": [
            event["data"] for event in published
            if event.get("type") == "normalized_event"
        ],
        "runs_root": str(runs_root),
    }


# --------------------------------------------------------------------------
# 断言
# --------------------------------------------------------------------------

def _check(name: str, passed: bool | None, detail: str) -> dict[str, Any]:
    mark = {True: "通过", False: "未过", None: "未触发"}[passed]
    print(f"  [{mark}] {name} —— {detail}")
    return {"name": name, "passed": passed, "detail": detail}


def assess_main(outcome: dict[str, Any]) -> list[dict[str, Any]]:
    ledger = outcome["ledger"]
    by_key = {f"{row['goal_id']}/{row['chapter_id']}": row for row in ledger}
    x_keys = [key for key in by_key if key.endswith("ch-2")]
    checks: list[dict[str, Any]] = []

    bad_status = [k for k, r in by_key.items() if r["status"] not in TERMINAL]
    bad_reason = [
        f"{k}:{r['reason']}" for k, r in by_key.items()
        if (r["status"] == "done" and r["reason"] is not None)
        or (r["status"] in {"missing", "deferred"} and r["reason"] not in REASONS)
    ]
    checks.append(_check(
        "断言1 每章终态∈{done,missing,deferred} 且 reason 闭集",
        not bad_status and not bad_reason,
        f"非终态章={bad_status or '无'}；越界 reason={bad_reason or '无'}；共 {len(ledger)} 章",
    ))

    x_rows = [(k, by_key[k]) for k in sorted(x_keys)]
    x_ok = bool(x_rows) and all(
        row["status"] == "missing" and row["reason"] == "tool_unavailable"
        and row["attempts"] == 1
        for _, row in x_rows
    )
    checks.append(_check(
        "断言2 X 不可用→立即 tool_unavailable 不烧重试",
        x_ok,
        "；".join(
            f"{k} status={row['status']} reason={row['reason']} attempts={row['attempts']}"
            for k, row in x_rows
        ) or "无 X 章",
    ))

    empty = [k for k, r in by_key.items() if r["reason"] == "empty_result"]
    checks.append(_check(
        "断言3 空结果→empty_result",
        True if empty else None,
        f"命中章={empty}" if empty else "本轮真实源无零命中，未自然触发（见结论章取证）",
    ))

    deferred = [
        k for k, r in by_key.items()
        if r["reason"] == "quota_exhausted" or r["status"] == "deferred"
    ]
    checks.append(_check(
        "断言4 429→deferred 后补一轮",
        True if deferred else None,
        f"命中章={deferred}" if deferred else "本轮未撞真实 429，未自然触发（另见 inject-429 子例）",
    ))

    missing_meta = [
        k for k, r in by_key.items()
        if not r["attempts"] or r["engine"] not in {"claude", "codex"}
    ]
    checks.append(_check(
        "断言5 每章 attempts/engine 可查",
        not missing_meta,
        f"缺失章={missing_meta or '无'}；"
        + "，".join(f"{k}={r['attempts']}次/{r['engine']}" for k, r in sorted(by_key.items())),
    ))

    checks.append(_check(
        "断言6 全部 6 章 ≤ 10 min",
        (not outcome["timed_out"]) and outcome["total_seconds"] <= 600,
        f"实测 {outcome['total_seconds']}s（预算 600s）"
        + ("；已超预算被夹具停止" if outcome["timed_out"] else ""),
    ))
    contract_rejections = [
        item["chapter"] for item in outcome["attempts"]
        if "CodexTask 与校验上下文不一致" in str(item["conclusion_error"])
    ]
    checks.append(_check(
        "D3 注入 runs_root 不触发 CodexTask 契约批量拒绝",
        not contract_rejections and "/tmp/" in outcome["runs_root"],
        f"runs_root={outcome['runs_root']}；拒绝章={contract_rejections or '无'}",
    ))
    checks.append(_check(
        "D4 start_research 返回即研究终态",
        outcome["scheduler_status"] == "completed"
        and bool(outcome["goal_statuses"])
        and set(outcome["goal_statuses"].values()) == {"done"},
        f"scheduler={outcome['scheduler_status']}；goals={outcome['goal_statuses']}",
    ))
    error_events = [
        event for event in outcome.get("normalized_events", [])
        if event.get("is_error") is True
    ]
    real_causes = [
        attempt for attempt in outcome["attempts"]
        if attempt.get("succeeded") is False
    ]
    shortened_errors = [
        event for event in error_events
        if "Skill descriptions were shortened" in str(event.get("text", ""))
    ]
    checks.append(_check(
        "E4 非致命 Codex 启动告警不以错误事件进入 SSE",
        len(error_events) == len(real_causes) and not shortened_errors,
        f"is_error=True 事件={len(error_events)}；真实死因={len(real_causes)}；"
        f"shortened 假错误={len(shortened_errors)}",
    ))
    return checks


def assess_inject(outcome: dict[str, Any]) -> list[dict[str, Any]]:
    ledger = outcome["ledger"]
    updates = outcome["chapter_updates"]
    sequence = [
        (item.get("status"), item.get("reason"), item.get("attempts"))
        for item in updates
    ]
    saw_deferred = any(item.get("status") == "deferred" for item in updates)
    saw_quota = any(item.get("reason") == "quota_exhausted" for item in updates)
    attempts = ledger[0]["attempts"] if ledger else 0
    terminal = ledger[0]["status"] if ledger else None
    expected_terminal = (
        "missing" if outcome["case"] == "inject-429-twice" else "done"
    )
    return [_check(
        f"注入子例 {outcome['case']}：429→deferred→补一轮→{expected_terminal}",
        saw_deferred and saw_quota and attempts >= 2 and terminal == expected_terminal,
        f"账本更新序列={sequence}；终态="
        f"{[(r['status'], r['reason'], r['attempts']) for r in ledger]}",
    )]


def assess_empty(outcome: dict[str, Any]) -> list[dict[str, Any]]:
    ledger = outcome["ledger"]
    row = ledger[0] if ledger else {}
    return [_check(
        "真实零命中→empty_result",
        row.get("status") == "missing" and row.get("reason") == "empty_result",
        f"终态={row.get('status')} reason={row.get('reason')} "
        f"attempts={row.get('attempts')} engine={row.get('engine')} "
        f"耗时={outcome['total_seconds']}s",
    )]


def assess_engine_error(outcome: dict[str, Any]) -> list[dict[str, Any]]:
    ledger = outcome["ledger"]
    row = ledger[0] if ledger else {}
    starts = [item["started_s"] for item in outcome["attempts"]]
    gaps = [round(right - left, 1) for left, right in zip(starts, starts[1:])]
    return [
        _check(
            "D1 注入 engine_error 原文进入终态账本",
            "夹具注入 engine_error" in str(row.get("engine_error")),
            f"engine_error={row.get('engine_error')!r}",
        ),
        _check(
            "D2 同因失败恒等于 3 次且总耗时小于 60 秒",
            row.get("attempts") == 3 and outcome["total_seconds"] < 60,
            f"attempts={row.get('attempts')}；耗时={outcome['total_seconds']}s",
        ),
        _check(
            "D2b 相邻两次 start_chapter 间隔至少 5 秒",
            len(gaps) == 2 and all(gap >= 5 for gap in gaps),
            f"间隔={gaps}",
        ),
    ]


def assess_timeout(outcome: dict[str, Any]) -> list[dict[str, Any]]:
    checks = assess_engine_error(outcome)
    row = outcome["ledger"][0] if outcome["ledger"] else {}
    checks[0] = _check(
        "E1 注入超时原文进入账本且 reason=timeout",
        "超时" in str(row.get("engine_error")) and row.get("reason") == "timeout",
        f"reason={row.get('reason')!r}；engine_error={row.get('engine_error')!r}",
    )
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description="Owli 采集模块夹具")
    parser.add_argument("outdir")
    parser.add_argument(
        "--case", default="main",
        choices=[
            "main",
            "inject-429",
            "inject-429-twice",
            "inject-engine-error",
            "inject-timeout",
            "empty-hn",
        ])
    parser.add_argument("--budget-seconds", type=float, default=900.0)
    args = parser.parse_args()

    root = Path(args.outdir)
    print(f"=== 采集模块夹具 case={args.case} 输出目录={root}\n", flush=True)
    outcome = asyncio.run(run_case(root, args.case, args.budget_seconds))

    print("\n=== 账本终态")
    for row in outcome["ledger"]:
        print("  " + json.dumps(row, ensure_ascii=False))
    print(f"\n=== 调度器 status={outcome['scheduler_status']} "
          f"goals={outcome['goal_statuses']}")
    print(f"=== agents={outcome['agent_statuses']}")

    print("\n=== 断言")
    checks = {
        "main": assess_main,
        "inject-429": assess_inject,
        "inject-429-twice": assess_inject,
        "inject-engine-error": assess_engine_error,
        "inject-timeout": assess_timeout,
        "empty-hn": assess_empty,
    }[args.case](outcome)
    outcome["checks"] = checks
    (root / "summary.json").write_text(
        json.dumps(outcome, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    print(f"\n=== 明细已落盘 {root / 'summary.json'}")
    failed = [item["name"] for item in checks if item["passed"] is False]
    print("=== 结论：" + ("全部通过" if not failed else f"未过 {failed}"))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
