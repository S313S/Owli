#!/usr/bin/env python3
"""§RATE-5 货 2（一次性）：用当前代码的评级写回，把一份研究的评级章产物重新落库。

为什么要这一步：§RATE-5 之前的写回把帖子入库带的平台基线分（`baseline:*`）当成
「已有分」，评级章给帖子的真评分整块丢了。产物文件还在
（`runs/<id>/goals/<goal>/reliability-audit*.json`），**不需要再跑模型**，拿新码
的 `RuntimeCoordinator._persist_rating_chapter` 原样再落一遍就行。

⛔ 不走 `scripts/backfill-evidence-ratings.py` / `backfill_report`：那条会改写工作稿
并重排 `citation_no`，角标错位是 09-08 实测事故。这里只经 `upsert_evidence_batch`
按 permalink 贴评分列，`citation_no` 取自库行原值。

runs 只读：本脚本只读评级章产物与 plan_snapshot，不写 runs 下任何文件；事件不进
库的 events 表，打印并可落到 `--events-out`。

    ../Owli/.venv/bin/python scripts/rate5_repersist.py \
        --database var/rate5-copy.db --runs ../Owli-wx1/var/runs \
        --research r-20271e8a5028 --events-out var/logs/rate5-copy-events.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.orchestrator.runtime import RuntimeCoordinator  # noqa: E402
from app.plan.model import Plan  # noqa: E402
from app.store.dao import Store  # noqa: E402


async def repersist(
    store: Store, runs: Path, research_id: str, only: set[str] | None = None,
) -> list[dict[str, Any]]:
    report = store.get_report(research_id)
    if report is None or not isinstance(report.get("plan_snapshot"), dict):
        raise SystemExit(f"{research_id}：库里没有这份研究或没有 plan_snapshot")
    plan = Plan.from_dict(report["plan_snapshot"])
    events: list[dict[str, Any]] = []

    async def publish(_research_id: str, payload: dict[str, Any]) -> None:
        events.append(payload)

    coordinator = RuntimeCoordinator(
        store=store, event_buffer=SimpleNamespace(publish=publish), researches={},
        cards={}, runs_root=runs,
        routing_utc_clock=lambda: datetime.now(timezone.utc),
    )
    for goal in plan.goals:
        for agent in goal.agents:
            if only and agent.agent_id not in only:
                continue
            # 非评级章在 `_persist_rating_chapter` 里自己早退，不会写库
            await coordinator._persist_rating_chapter(plan, goal.goal_id, agent)
    return [e["data"] for e in events if e["type"] == "rating_chapter_persisted"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--runs", type=Path, required=True)
    parser.add_argument("--research", required=True)
    parser.add_argument("--events-out", type=Path)
    parser.add_argument("--only", action="append",
                        help="只重落这几个评级章 agent_id（可重复；造红用）")
    args = parser.parse_args()

    store = Store(args.database.resolve())
    data = asyncio.run(repersist(
        store, args.runs.resolve(), args.research, set(args.only or []) or None,
    ))
    total = {"rated": 0, "kept": 0, "filled": 0, "replaced_baseline": 0,
             "unmatched": 0, "invalid": 0}
    failed = []
    for item in data:
        print(json.dumps(item, ensure_ascii=False))
        for key in total:
            total[key] += int(item.get(key) or 0)
        if item.get("failed"):
            failed.append(f"{item['goal_id']}/{item['agent_id']}: {item['failed']}")
    print("TOTAL", json.dumps(total, ensure_ascii=False))
    if args.events_out:
        args.events_out.parent.mkdir(parents=True, exist_ok=True)
        args.events_out.write_text(
            json.dumps({"events": data, "total": total}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    if failed:
        print("FAILED", *failed, sep="\n  ")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
