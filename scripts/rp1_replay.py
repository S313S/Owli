#!/usr/bin/env python3
"""§RP-1 脚本级重放：把某研究的某一节拿旧数据重跑一遍。

**重放不作关账证据**：它拿旧库旧产物、跑当下的代码，两者不同源。
用来迭代与诊断（省下整跑的钱），关账仍需一轮从规划起的干净整跑。

    python3 scripts/rp1_replay.py list \
        --source-db ../Owli-d021/var/owli-d021run-r5ca713c1297e.db \
        --research r-5ca713c1297e

    python3 scripts/rp1_replay.py section \
        --source-db ../Owli-d021/var/owli-d021run-r5ca713c1297e.db \
        --source-runs ../Owli-d021/runs \
        --research r-5ca713c1297e --goal goal-3 --chapter ch-4 --section sec-1 \
        --workspace var/replay/goal3-ch4-sec1
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.plan.model import Plan  # noqa: E402
from app.replay.sandbox import open_sandbox  # noqa: E402
from app.replay.section import replay_sections  # noqa: E402


def _list(args: argparse.Namespace) -> int:
    """只读打开底料库：`list` 一个字都不许往原件里写（`Store` 是读写连接）。"""

    connection = sqlite3.connect(f"file:{Path(args.source_db).resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        report = connection.execute(
            "SELECT id, status, plan_snapshot FROM reports WHERE id = ?",
            (args.research,),
        ).fetchone()
        if report is None:
            print(f"库里没有 {args.research}", file=sys.stderr)
            return 2
        chapters = {
            (row["goal_id"], row["chapter_id"]): dict(row)
            for row in connection.execute(
                "SELECT * FROM chapter_progress WHERE research_id = ?",
                (args.research,),
            )
        }
    finally:
        connection.close()
    plan = Plan.from_dict(json.loads(report["plan_snapshot"]))
    print(f"{report['id']}  status={report['status']}  scale={plan.scale}")
    for goal in plan.goals:
        print(f"\n== {goal.goal_id}  {goal.title}")
        for agent in goal.agents:
            chapter = agent.chapter if isinstance(agent.chapter, dict) else {}
            chapter_id = str(chapter.get("chapter_id") or agent.agent_id)
            row = chapters.get((goal.goal_id, chapter_id), {})
            print(
                f"   {chapter_id:<6} {row.get('status', '-'):<8}"
                f" attempts={row.get('attempts', '-')}"
                f" reason={row.get('reason') or '-':<18} {agent.agent_id}"
            )
            for key, sub in sorted(chapters.items()):
                if key[0] == goal.goal_id and key[1].startswith(f"{chapter_id}/"):
                    print(
                        f"      └ {key[1].rsplit('/', 1)[-1]:<6}"
                        f" {sub['status']:<8} attempts={sub['attempts']}"
                        f" reason={sub['reason'] or '-'}"
                    )
    return 0


def _section(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace)
    if workspace.exists() and any(workspace.iterdir()):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        workspace = workspace.parent / f"{workspace.name}-{stamp}"
    sandbox = open_sandbox(
        source_database=Path(args.source_db),
        source_runs=Path(args.source_runs),
        research_id=args.research,
        workspace=workspace,
    )
    print(f"沙盒：{sandbox.workspace}")
    print(f"底料指纹（跑前）：{json.dumps(sandbox.source_fingerprint.as_dict())}")
    result = asyncio.run(replay_sections(
        sandbox=sandbox,
        research_id=args.research,
        goal_id=args.goal,
        chapter_id=args.chapter,
        sections=list(args.section) or None,
    ))
    before = {(r["chapter_id"]): r for r in result.ledger_before}
    print("\n账本变化（只列变了的行；attempts 变了也算变）：")
    for row in result.ledger_after:
        old = before.get(row["chapter_id"], {})
        same = (old.get("status"), old.get("reason"), old.get("attempts")) == (
            row["status"], row["reason"], row["attempts"]
        )
        if same:
            continue
        print(
            f"   {row['chapter_id']:<14}"
            f" {old.get('status', '-')}/{old.get('reason') or '-'}"
            f"  →  {row['status']}/{row['reason'] or '-'}"
            f"  attempts {old.get('attempts', '-')}→{row['attempts']}"
        )
    print(f"\n产物：{list(result.artifacts) or '（无）'}")
    print(f"task_result：{result.task_result}")
    print(f"底料指纹（跑后）：{json.dumps(result.fingerprint_after.as_dict())}")
    print(f"底料原件零改动：{'是' if result.source_untouched else '否 ← 红'}")
    return 0 if result.source_untouched else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    listing = sub.add_parser("list", help="列出底料的章/节账本，选重放目标用")
    listing.add_argument("--source-db", required=True)
    listing.add_argument("--research", required=True)
    listing.set_defaults(handler=_list)

    section = sub.add_parser("section", help="在沙盒里重跑指定章的指定节")
    section.add_argument("--source-db", required=True)
    section.add_argument("--source-runs", required=True)
    section.add_argument("--research", required=True)
    section.add_argument("--goal", required=True)
    section.add_argument("--chapter", required=True)
    section.add_argument(
        "--section", action="append", default=[],
        help="节名如 sec-1，可重复；不给则重跑该章全部未终态节",
    )
    section.add_argument("--workspace", required=True)
    section.set_defaults(handler=_section)

    args = parser.parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
