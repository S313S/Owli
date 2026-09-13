#!/usr/bin/env python3
"""§D-064 单张采集卡重放 + 库层读数。

走 RP-1 的沙盒与 `replay_sections`（整跑同一条 `_run_task` 路径），只多两件事：

1. 重放前在**沙盒库**里清掉本卡本源的旧证据行（底料原件只读、指纹前后比对），
   这样跑后 evidence 表里这张卡的行数就是本次调用的真实产出，不与旧轮次叠加；
2. 跑后直接读沙盒库与本次 transcript，打印判据要的读数：
   耗时、源工具调用次数、帖子/评论行数、系统检索词命中、缺口里有没有「上限 2」。

    ../Owli/.venv/bin/python scripts/d064_card_replay.py \
        --source-db ../Owli-alloc2/var/alloc2-serve.db \
        --source-runs ../Owli-alloc2/var/runs \
        --research r-50600e09f7dd --goal goal-1 --chapter ch-1 \
        --workspace var/replay/d064-goal1-ch1
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.plan.model import Plan  # noqa: E402
from app.replay.sandbox import open_sandbox  # noqa: E402
from app.replay.section import _resolve_agent, replay_sections  # noqa: E402


def _plan(database: Path, research_id: str) -> Plan:
    connection = sqlite3.connect(database)
    try:
        raw = connection.execute(
            "SELECT plan_snapshot FROM reports WHERE id = ?", (research_id,)
        ).fetchone()[0]
    finally:
        connection.close()
    return Plan.from_dict(json.loads(raw))


def _clear(database: Path, research_id: str, goal_id: str, agent_id: str, source: str) -> int:
    connection = sqlite3.connect(database)
    try:
        cursor = connection.execute(
            "DELETE FROM evidence WHERE report_id = ? AND goal_id = ?"
            " AND agent_name = ? AND platform = ?",
            (research_id, goal_id, agent_id, source),
        )
        connection.commit()
        return cursor.rowcount
    finally:
        connection.close()


def _source_calls(transcript: Path, since: float) -> list[dict]:
    calls: list[dict] = []
    if not transcript.is_file():
        return calls
    for line in transcript.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if float(row.get("ts") or 0) < since:
            continue
        event = row.get("event")
        if not isinstance(event, dict) or event.get("type") != "item.started":
            continue
        item = event.get("item") or {}
        if item.get("type") == "mcp_tool_call" and str(item.get("tool", "")).startswith("source."):
            calls.append({
                "at_seconds": round(float(row["ts"]) - since, 1),
                "tool": item.get("tool"),
                "query": (item.get("arguments") or {}).get("query"),
            })
    return calls


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", required=True)
    parser.add_argument("--source-runs", required=True)
    parser.add_argument("--research", required=True)
    parser.add_argument("--goal", required=True)
    parser.add_argument("--chapter", required=True)
    parser.add_argument("--workspace", required=True)
    args = parser.parse_args(argv)

    # 库路径必须是绝对的：源工具子进程的 cwd 是产物目录，相对路径会让它打不开沙盒库
    # （首轮造红实测 OperationalError: unable to open database file，0 行入库）。
    workspace = Path(args.workspace).resolve()
    if workspace.exists() and any(workspace.iterdir()):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        workspace = workspace.parent / f"{workspace.name}-{stamp}"
    sandbox = open_sandbox(
        source_database=Path(args.source_db), source_runs=Path(args.source_runs),
        research_id=args.research, workspace=workspace,
    )
    plan = _plan(sandbox.database, args.research)
    _, agent = _resolve_agent(plan, args.goal, args.chapter)
    sources = list(agent.capability.get("sources", []))
    if len(sources) != 1:
        print(f"{agent.agent_id} 不是单源采集卡：{sources}", file=sys.stderr)
        return 2
    source = sources[0]
    output = sandbox.runs_root / args.research / str(agent.output["path"])
    output.unlink(missing_ok=True)
    cleared = _clear(sandbox.database, args.research, args.goal, agent.agent_id, source)
    print(f"沙盒：{sandbox.workspace}")
    print(f"卡：{args.goal}/{args.chapter} {agent.agent_id} source={source} entity={agent.entity}")
    print(f"沙盒库清掉本卡旧证据行：{cleared}")

    started = time.time()
    result = asyncio.run(replay_sections(
        sandbox=sandbox, research_id=args.research,
        goal_id=args.goal, chapter_id=args.chapter,
    ))
    elapsed = time.time() - started

    transcript = output.with_name(f"{output.stem}.transcript.jsonl")
    calls = _source_calls(transcript, started)
    connection = sqlite3.connect(sandbox.database)
    try:
        rows = connection.execute(
            "SELECT kind, source_type, source_keyword FROM evidence"
            " WHERE report_id = ? AND goal_id = ? AND agent_name = ? AND platform = ?",
            (args.research, args.goal, agent.agent_id, source),
        ).fetchall()
    finally:
        connection.close()
    kinds = Counter(str(kind) for kind, _, _ in rows)
    keywords = Counter(str(keyword) for kind, _, keyword in rows if kind != "comment")
    task = result.task_result  # TaskRunResult：没有 conclusion，自报结论读引擎侧留档
    last_message = output.with_name(f".{output.stem}-codex-last-message.json")
    conclusion: dict = {}
    if last_message.is_file() and last_message.stat().st_mtime >= started:
        try:
            conclusion = json.loads(last_message.read_text(encoding="utf-8"))
        except ValueError:
            conclusion = {}
    unmet = [str(item) for item in conclusion.get("unmet") or []]
    readings = {
        "elapsed_seconds": round(elapsed, 1),
        "succeeded": bool(getattr(task, "succeeded", False)),
        "chapter_status": getattr(task, "chapter_status", None),
        "reason": getattr(task, "reason", None),
        "actual_count": getattr(task, "actual_count", None),
        "conclusion_status": conclusion.get("status"),
        "engine_error": getattr(task, "engine_error", None),
        "artifact_written": output.is_file(),
        "source_calls": len(calls),
        "calls": calls,
        "evidence_rows": len(rows),
        "rows_by_kind": dict(kinds),
        "post_rows_by_keyword": dict(keywords),
        "unmet": unmet,
        "unmet_mentions_cap_2": any("上限 2" in str(item) or "上限2" in str(item) for item in unmet),
        "source_untouched": result.source_untouched,
    }
    print(json.dumps(readings, ensure_ascii=False, indent=2))
    (sandbox.workspace / "d064-readings.json").write_text(
        json.dumps(readings, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return 0 if result.source_untouched else 1


if __name__ == "__main__":
    raise SystemExit(main())
