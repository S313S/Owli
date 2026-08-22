"""规划模块夹具（M3-h-fix-2 条 1/2/3 验证用）：真实 Claude 直调 generate_plan。

用法：cd Owli-m3h && ../Owli/.venv/bin/python scripts/fixtures/planning_fixture.py <输出目录> [轮次标签]

题目固定「豆包语音输入法的竞品分析」、scale=fast；不起服务、不进执行期。
只做观测（猴补仅加时间戳与打印，不改判定），产品代码零改动。
每步（骨架 / 段 / 段级 lint / 章 / 全量 lint）打时间戳并落 trace.jsonl，
lint 错误原文全文落 lint-errors.txt，最终计划落 plan.json，汇总落 summary.json。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, ".")

QUERY = "豆包语音输入法的竞品分析"
SCALE = "fast"
HARD_CAP_SECONDS = 30 * 60

ROOT = Path(sys.argv[1]).resolve()
LABEL = sys.argv[2] if len(sys.argv) > 2 else "run-1"
ROOT.mkdir(parents=True, exist_ok=True)
OUT = ROOT / LABEL
OUT.mkdir(parents=True, exist_ok=True)
RID = f"r-plan-{LABEL}"

TRACE_PATH = OUT / "trace.jsonl"
LINT_PATH = OUT / "lint-errors.txt"
TRACE: list[dict] = []
T0 = time.time()


def rec(kind: str, **fields) -> dict:
    ev = {
        "t": round(time.time() - T0, 1),
        "wall": datetime.now().strftime("%H:%M:%S"),
        "kind": kind,
        **fields,
    }
    TRACE.append(ev)
    with TRACE_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(ev, ensure_ascii=False) + "\n")
    tail = " ".join(f"{k}={v!r}" for k, v in fields.items())
    # 终端只印摘要，trace.jsonl 落全文（上一轮 P3 根因就卡在截断上）
    print(f"[{ev['wall']} +{ev['t']:7.1f}s] {kind} {tail[:700]}", flush=True)
    return ev


def lint_dump(header: str, errors: list[str], warnings: list[str]) -> None:
    with LINT_PATH.open("a", encoding="utf-8") as handle:
        handle.write(f"\n===== {header} (+{time.time() - T0:.1f}s) =====\n")
        for item in errors:
            handle.write(f"[error] {item}\n")
        for item in warnings:
            handle.write(f"[warn ] {item}\n")


# ---------- 建库建报告 ----------
db = OUT / "owli.db"
if db.exists():
    db.unlink()
with sqlite3.connect(db) as connection:
    connection.executescript(Path("app/store/schema.sql").read_text(encoding="utf-8"))

from app.store.dao import Store  # noqa: E402

store = Store(db)
created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
store.create_report(
    id=RID,
    title=QUERY,
    research_question=QUERY,
    created_at=created_at,
    extra={"plan_generated_at": created_at},
)
store.runs_root = OUT / "runs"


async def on_plan_event(event) -> None:
    rec(
        "SSE",
        turn=str(getattr(event, "turn_id", "")),
        错误=bool(getattr(event, "is_error", False)),
        文本=str(getattr(event, "text", ""))[:400],
    )


store.on_plan_event = on_plan_event

# ---------- 观测猴补（只加时间戳与打印） ----------
import app.plan.generate as gen_mod  # noqa: E402
from app.plan import segments as seg_mod  # noqa: E402
from app.adapters.routing import RoutedAdapter  # noqa: E402

_orig_seg_generate = seg_mod.PlanSegmentWorkspace.generate
_orig_reset = seg_mod.PlanSegmentWorkspace.reset_attempts
_orig_lint = gen_mod.lint
_orig_affected = gen_mod._affected_goal_indices
_orig_chapters = gen_mod.generate_chapter_specs

SEG_STARTS: dict[str, int] = {}
LINT_ROUNDS: list[dict] = []


async def seg_generate(self, name, prompt, adapter, *, on_retry=None, output_schema=None):
    SEG_STARTS[name] = SEG_STARTS.get(name, 0) + 1
    rec("段·开始", 段=name, 第几次进入=SEG_STARTS[name], 已计attempts=self._attempts.get(name, 0),
        带schema=output_schema is not None)
    started = time.time()

    def wrapped_on_retry(retry, error):
        rec("段·重试回灌", 段=name, 下一次=retry, 原因=str(error))  # 不截断：P3 根因取证需要全文
        return on_retry(retry, error) if on_retry is not None else None

    try:
        value = await _orig_seg_generate(
            self, name, prompt, adapter,
            on_retry=wrapped_on_retry, output_schema=output_schema,
        )
    except BaseException as exc:  # noqa: BLE001 - 夹具要把死因原样记下
        rec("段·失败", 段=name, 耗时秒=round(time.time() - started, 1),
            attempts=self._attempts.get(name, 0), 错误=f"{type(exc).__name__}: {exc}"[:500])
        raise
    rec("段·完成", 段=name, 耗时秒=round(time.time() - started, 1),
        attempts=self._attempts.get(name, 0))
    return value


def reset_attempts(self, name):
    rec("段·预算清零", 段=name, 清零前attempts=self._attempts.get(name, 0))
    return _orig_reset(self, name)


def lint_wrapper(plan, **kwargs):
    result = _orig_lint(plan, **kwargs)
    has_chapter = any(agent.chapter for goal in plan.goals for agent in goal.agents)
    stage = "全量lint" if has_chapter else "段级lint"
    round_no = len([item for item in LINT_ROUNDS if item["stage"] == stage]) + 1
    errors = list(result["errors"])
    warnings = list(result.get("warnings", []))
    LINT_ROUNDS.append({
        "stage": stage, "round": round_no, "t": round(time.time() - T0, 1),
        "errors": errors, "warnings": warnings,
        "chapters_per_goal": [len(goal.agents) for goal in plan.goals],
    })
    rec(f"{stage}·第{round_no}轮", 错误数=len(errors), 告警数=len(warnings),
        每goal_agent数=[len(goal.agents) for goal in plan.goals])
    for index, item in enumerate(errors, start=1):
        rec(f"{stage}·错误{index}", 原文=item)
    lint_dump(f"{stage} 第{round_no}轮", errors, warnings)
    return result


def affected_wrapper(errors, goal_count):
    result = _orig_affected(errors, goal_count)
    rec("受影响goal判定", 结果=result, 参与判定的错误条数=len(errors))
    return result


async def chapters_wrapper(*args, **kwargs):
    rec("章·批量生成开始")
    started = time.time()
    try:
        return await _orig_chapters(*args, **kwargs)
    finally:
        rec("章·批量生成结束", 耗时秒=round(time.time() - started, 1))


seg_mod.PlanSegmentWorkspace.generate = seg_generate
seg_mod.PlanSegmentWorkspace.reset_attempts = reset_attempts
gen_mod.lint = lint_wrapper
gen_mod._affected_goal_indices = affected_wrapper
gen_mod.generate_chapter_specs = chapters_wrapper


# ---------- 跑 ----------
async def main() -> None:
    adapter = RoutedAdapter(utc_clock=lambda: datetime.now(timezone.utc))
    rec("规划·开始", 题目=QUERY, scale=SCALE, research_id=RID)
    try:
        plan = await asyncio.wait_for(
            gen_mod.generate_plan(QUERY, store, adapter, scale=SCALE),
            timeout=HARD_CAP_SECONDS,
        )
    except BaseException as exc:  # noqa: BLE001
        rec("规划·失败", 类型=type(exc).__name__, 错误=str(exc)[:2000],
            总耗时秒=round(time.time() - T0, 1))
        summarize(None)
        raise
    rec("规划·成功", 总耗时秒=round(time.time() - T0, 1), goal数=len(plan.goals))
    (OUT / "plan.json").write_text(
        json.dumps(plan.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    summarize(plan)


def summarize(plan) -> None:
    total = round(time.time() - T0, 1)
    seg_rounds = {name: count for name, count in SEG_STARTS.items()}
    regenerated = sorted(
        name for name, count in SEG_STARTS.items()
        if count > 1 and not name.startswith("skeleton")
    )
    summary: dict = {
        "label": LABEL,
        "research_id": RID,
        "题目": QUERY,
        "scale": SCALE,
        "总耗时秒": total,
        "总耗时分": round(total / 60, 2),
        "段进入次数": seg_rounds,
        "被重生成的段": regenerated,
        "lint轮次": [
            {k: v for k, v in item.items() if k != "warnings"} for item in LINT_ROUNDS
        ],
        "成功": plan is not None,
    }
    if plan is not None:
        goals = []
        hn_ph = []
        shape_conflicts = []
        for goal in plan.goals:
            sources: list[str] = []
            for agent in goal.agents:
                for source in agent.capability.get("sources", []) or []:
                    sources.append(str(source))
                    if str(source) in {"hacker_news", "product_hunt"}:
                        hn_ph.append({
                            "goal_id": goal.goal_id, "agent_id": agent.agent_id,
                            "source_id": str(source),
                            "chapter_type": (agent.chapter or {}).get("chapter_type"),
                        })
            expected = (goal.deliverable or {}).get("shape")
            deliverable_path = str((goal.deliverable or {}).get("path", ""))
            for agent in goal.agents:
                output = agent.output or {}
                if deliverable_path and str(output.get("path", "")) == deliverable_path:
                    if output.get("shape") != expected:
                        shape_conflicts.append({
                            "goal_id": goal.goal_id, "agent_id": agent.agent_id,
                            "deliverable_shape": expected, "output_shape": output.get("shape"),
                        })
            goals.append({
                "goal_id": goal.goal_id,
                "title": goal.title,
                "章数": len(goal.agents),
                "章": [
                    {
                        "agent_id": agent.agent_id,
                        "display_name": agent.display_name,
                        "chapter_id": (agent.chapter or {}).get("chapter_id"),
                        "chapter_type": (agent.chapter or {}).get("chapter_type"),
                        "engine": agent.engine,
                        "sources": list(agent.capability.get("sources", []) or []),
                        "output_shape": (agent.output or {}).get("shape"),
                    }
                    for agent in goal.agents
                ],
                "sources": sorted(set(sources)),
            })
        summary["market_profile"] = plan.market_profile
        summary["market_profile_justification"] = plan.market_profile_justification
        summary["每goal章数"] = {item["goal_id"]: item["章数"] for item in goals}
        summary["goals"] = goals
        summary["HN_PH采集章"] = hn_ph
        summary["shape冲突"] = shape_conflicts
        summary["章数超4的goal"] = [
            item["goal_id"] for item in goals if item["章数"] > 4
        ]
    (OUT / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print("\n===== 汇总 =====", flush=True)
    print(json.dumps(
        {k: v for k, v in summary.items() if k not in {"goals", "lint轮次"}},
        ensure_ascii=False, indent=2,
    ), flush=True)
    print(f"\n产物：{OUT}", flush=True)


asyncio.run(main())
