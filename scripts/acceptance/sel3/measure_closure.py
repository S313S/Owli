"""§SEL-3 只读读数尺：量 `goal-6/ch-4` 的 declared inputs 命中率与各节 `cross_goal_count`。

尺子只用**消费侧**的生产函数（`_artifact_key` / `_merge_declared_done_inputs` /
`_allowed_evidence_goal_ids` / `_evidence_index`）——被测的是导入侧的路径 rebase，
两边不是同一段代码（记忆 `verification-ruler-needs-verifying`：可复用生产解析，
但被测函数本身绝不能当尺子）。

零引擎：只读库 + 纯函数重算一遍组池，不起任何 adapter、不写任何库。

    python scripts/acceptance/sel3/measure_closure.py \
        --database var/sel3-base.db --research-id r-3b3482ca7f8b \
        --runs-root ../Owli-src5/var/runs --chapter goal-6/ch-4
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from app.orchestrator.sectioning import (  # noqa: E402
    _allowed_evidence_goal_ids,
    _artifact_key,
    _evidence_index,
    _ledger_inputs,
    _merge_declared_done_inputs,
    _section_specs,
)
from app.plan.model import Plan, agent_kind_of  # noqa: E402
from app.reliability.relevance import rows_naming_entities  # noqa: E402
from app.store.dao import Store  # noqa: E402


def _find_agent(plan: Plan, goal_id: str, chapter_id: str) -> Any:
    for goal in plan.goals:
        if str(goal.goal_id) != goal_id:
            continue
        for agent in goal.agents:
            chapter = agent.chapter if isinstance(agent.chapter, Mapping) else {}
            if str(chapter.get("chapter_id") or agent.agent_id) == chapter_id:
                return agent
    raise SystemExit(f"计划里没有 {goal_id}/{chapter_id}")


def measure(database: Path, research_id: str, runs_root: Path, chapter: str) -> dict[str, Any]:
    goal_id, chapter_id = chapter.split("/", 1)
    store = Store(database)
    report = store.get_report(research_id)
    if report is None:
        raise SystemExit(f"{database} 里没有 {research_id}")
    plan = Plan.from_dict(report["plan_snapshot"])
    research_root = Path(runs_root).resolve() / research_id
    input_rows = store.list_chapters(research_id)
    evidence_rows = store.list_evidence(research_id)
    evidence_rows = rows_naming_entities(evidence_rows, plan) or evidence_rows

    agent = _find_agent(plan, goal_id, chapter_id)
    chapter_map = agent.chapter if isinstance(agent.chapter, Mapping) else {}
    opening = chapter_map.get("opening", {})
    declared_inputs = opening.get("inputs", []) if isinstance(opening, Mapping) else []
    kind = agent_kind_of(agent.agent_id, (agent.capability or {}).get("profile"))

    done_rows_by_path: dict[str, dict[str, Any]] = {}
    for row in input_rows:
        if row["status"] != "done":
            continue
        key = _artifact_key(row.get("actual_output_path"), research_root)
        if key is not None:
            done_rows_by_path.setdefault(key, row)

    declared: list[dict[str, Any]] = []
    for index, item in enumerate(declared_inputs, start=1):
        path = item.get("path") if isinstance(item, Mapping) else None
        key = _artifact_key(path, research_root)
        row = done_rows_by_path.get(key) if key is not None else None
        declared.append({
            "no": index,
            "declared_path": str(path),
            "hit": row is not None,
            "row": (
                f"{row['goal_id']}/{row['chapter_id']}" if row is not None else None
            ),
            "ledger_path": row["actual_output_path"] if row is not None else None,
        })

    sections: list[dict[str, Any]] = []
    for section in _section_specs(plan, agent):
        inputs = _ledger_inputs(input_rows, section["goal_id"])
        inputs = _merge_declared_done_inputs(
            inputs, input_rows, declared_inputs, research_root=research_root,
        )
        allowed = _allowed_evidence_goal_ids(
            plan, input_rows, inputs, str(section["goal_id"]),
            research_root=research_root,
        )
        pool, _ = _evidence_index(
            evidence_rows, allowed,
            section_goal_id=str(section["goal_id"]), chapter_kind=kind,
        )
        items = pool["items"]
        own = sum(
            1 for item in items
            if str(item.get("goal_id")) == str(section["goal_id"])
        )
        sections.append({
            "section_id": section["section_id"],
            "goal_id": section["goal_id"],
            "allowed_goal_ids": sorted(allowed),
            "pool_size": len(items),
            "own_goal_count": own,
            "cross_goal_count": len(items) - own,
        })

    return {
        "database": str(database),
        "research_id": research_id,
        "research_root": str(research_root),
        "chapter": chapter,
        "agent_kind": kind,
        "evidence_rows": len(evidence_rows),
        "declared_inputs_total": len(declared),
        "declared_inputs_hit": sum(1 for item in declared if item["hit"]),
        "declared_inputs": declared,
        "sections": sections,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--research-id", required=True)
    parser.add_argument("--runs-root", required=True, type=Path)
    parser.add_argument("--chapter", default="goal-6/ch-4")
    args = parser.parse_args()
    result = measure(args.database, args.research_id, args.runs_root, args.chapter)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
