"""§D-068 判据尺子：采集卡被删光的 goal，最终验收条里还剩多少「要本 goal 采集产物」的条。

    ../Owli/.venv/bin/python scripts/acceptance/d068/d068_hollow_ruler.py --code-root <代码树> [--out 读数.json]

⛔ 尺子**不调** `normalize` 里的任何判定函数（`_asks_backfill` / `_repair_acceptance` / `deleted_cards`）：
WX-1 判据⑦就是拿生产 `_asks_backfill` 当尺子，与被测闸同盲、报了假绿。这里只调 `normalize_plan`
得到最终计划（被测对象本身），被删卡、空心 goal、命中词全部由本脚本从「草稿 vs 最终计划」自己算：

- 被删卡：草稿（`_build_plan` 之后、`normalize_plan` 之前）的采集章 agent_id 不在最终计划里；
- 空心 goal：草稿里有采集章、最终计划里一张不剩（且 goal 本身还在）；
- 命中：最终验收条原文里出现被删卡的 (source_id, entity) 组合（`·` / `,` / `、` 任一分隔，忽略大小写）、
  被删卡的 output.path（全形或 `goal-N/…` 短形）、或「新增…采集 / 采集章 / 采集 JSON / 补采」主语。
  主语匹配故意比生产宽（`新增` 与 `采集` 之间允许 6 个非分隔字符），不做任何语境豁免——豁免条另列对照。

读数同时给不退化那一半：各夹具 `normalize_plan` 后的修正说明 sha 与去 prompt 计划 sha。
`--code-root` 指向要量的代码树（旧码 = main 9801396 的 ../Owli，新码 = 本 worktree），夹具一律从本脚本所在树读。
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[3]
FIXTURES = HERE / "tests" / "fixtures"
TARGET = "d068/wx1-run-0914-1425"

SUBJECT = re.compile(r"新增[^，,；;。]{0,6}采集|采集章|采集\s*JSON|补采", re.IGNORECASE)
SEP = r"\s*[·・,，、/]\s*"


def _without_prompts(value):
    if isinstance(value, dict):
        return {k: _without_prompts(v) for k, v in value.items() if k != "prompt"}
    if isinstance(value, list):
        return [_without_prompts(v) for v in value]
    return value


def _sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _build(folder: Path, scale: str, entity_limit: int | None):
    from app.plan.generate import _build_plan

    skeleton = json.loads((folder / "skeleton.json").read_text(encoding="utf-8"))
    paths = sorted(folder.glob("entity-*.json"), key=lambda p: int(p.stem.split("-")[1]))
    if entity_limit is not None:
        paths = [p for p in paths if int(p.stem.split("-")[1]) <= entity_limit]
    cards = []
    for path in paths:
        card = json.loads(path.read_text(encoding="utf-8"))
        card.setdefault("id", card["canonical"])
        cards.append(card)
    return _build_plan(
        json.loads((folder / "assembled.json").read_text(encoding="utf-8")),
        query="国内大家对豆包的看法",
        research_id=f"r-{folder.name}",
        timestamp="2026-09-14T00:00:00+00:00",
        scale=scale,
        market_profile=skeleton["market_profile"],
        market_profile_justification=skeleton["market_profile_justification"],
        subjects=skeleton["subjects"],
        subjects_justification=skeleton["subjects_justification"],
        entities=cards,
        repairs=[],
    )


def _collectors(plan) -> dict[str, dict]:
    """草稿里的采集章：profile=web-collector 且带实体（与生产 `_is_collector` 分开写）。"""
    result = {}
    for goal in plan.goals:
        for agent in goal.agents:
            if agent.capability.get("profile") != "web-collector" or not str(agent.entity or "").strip():
                continue
            result[agent.agent_id] = {
                "goal_id": goal.goal_id,
                "name": agent.display_name,
                "source_id": str((agent.capability.get("sources") or [""])[0]),
                "entity": str(agent.entity).strip(),
                "path": str((agent.output or {}).get("path", "")),
            }
    return result


def _hits(text: str, dead: list[dict]) -> list[str]:
    hits = []
    for card in dead:
        combo = re.compile(re.escape(card["source_id"]) + SEP + re.escape(card["entity"]), re.IGNORECASE)
        if combo.search(text):
            hits.append(f"组合 {card['source_id']}·{card['entity']}")
        full = card["path"]
        short = full.removeprefix("goals/")
        if full and (full in text or re.search(r"(?<![\w/])" + re.escape(short), text)):
            hits.append(f"路径 {full}")
    hits += [f"主语 {m.group(0)}" for m in SUBJECT.finditer(text)]
    return hits


def measure_target() -> dict:
    from app.plan.normalize import normalize_plan

    folder = FIXTURES / TARGET
    plan = _build(folder, "standard", None)
    draft_acceptance = {goal.goal_id: list(goal.acceptance) for goal in plan.goals}
    before = _collectors(plan)
    notes = normalize_plan(plan, collection_plan=json.loads((folder / "allocation.json").read_text(encoding="utf-8")))
    after = _collectors(plan)
    goals = {goal.goal_id: goal for goal in plan.goals}
    dead = [card for agent_id, card in before.items() if agent_id not in after]
    hollow = sorted(
        {c["goal_id"] for c in before.values()} - {c["goal_id"] for c in after.values()},
        key=lambda g: int(g.split("-")[1]),
    )
    hollow = [g for g in hollow if g in goals]
    reading: dict = {"hollow_goals": hollow, "goals": {}}
    for goal_id, goal in goals.items():
        mine = [c for c in dead if c["goal_id"] == goal_id]
        items = []
        for index, text in enumerate(goal.acceptance):
            items.append({"index_final": index, "text": text, "hits": _hits(text, mine) if goal_id in hollow else []})
        removed = []
        for index, text in enumerate(draft_acceptance[goal_id]):
            if text not in goal.acceptance:
                marks = [n for n in notes if n.startswith(f"[修正4] {goal_id}.acceptance[{index}] 已摘：")]
                removed.append({
                    "index_draft": index,
                    "hits": _hits(text, mine) if goal_id in hollow else [],
                    "note_has_original": bool(marks) and marks[0].endswith(f"——原文「{text}」"),
                })
        reading["goals"][goal_id] = {
            "hollow": goal_id in hollow,
            "acceptance_sha": _sha(goal.acceptance),
            "remaining_hit_items": sum(1 for item in items if item["hits"]),
            "remaining": items,
            "removed": removed,
        }
    reading["hollow_remaining_hit_items"] = sum(reading["goals"][g]["remaining_hit_items"] for g in hollow)
    reading["notes_sha"] = _sha(notes)
    reading["plan_sha"] = _sha(_without_prompts(plan.to_dict()))
    return reading


def measure_regression() -> dict:
    from app.plan.model import Plan
    from app.plan.normalize import normalize_plan

    result = {}
    for name in ("d061", "alloc2", "d062fu", "d062fu-run-a"):
        folder = FIXTURES / name
        plan = _build(folder, "fast", 4)
        notes = normalize_plan(plan, collection_plan=json.loads((folder / "allocation.json").read_text()), per_goal_capacity=2)
        result[f"{name}/fast"] = {"notes_sha": _sha(notes), "plan_sha": _sha(_without_prompts(plan.to_dict()))}
    plan = Plan.from_dict(json.loads((FIXTURES / "d065" / "plan.json").read_text()))
    notes = normalize_plan(plan, collection_plan=json.loads((FIXTURES / "d065" / "plan-segments" / "allocation.json").read_text()))
    result["d065/standard"] = {"notes_sha": _sha(notes), "plan_sha": _sha(_without_prompts(plan.to_dict()))}
    for name in ("d067/alloc3-run-b", "d067/wx1-run-0914-1239"):
        folder = FIXTURES / name
        plan = _build(folder, "standard", None)
        notes = normalize_plan(plan, collection_plan=json.loads((folder / "allocation.json").read_text()))
        result[f"{name}/standard"] = {"notes_sha": _sha(notes), "plan_sha": _sha(_without_prompts(plan.to_dict()))}
        if name.endswith("1239"):
            goal_4 = next(g for g in plan.goals if g.goal_id == "goal-4")
            result[f"{name}/standard"]["goal4_removed"] = [
                n.split(" 已摘")[0] for n in notes if n.startswith("[修正4] goal-4")
            ]
            result[f"{name}/standard"]["goal4_acceptance_len"] = len(goal_4.acceptance)
    folder = FIXTURES / "d067/wx1-run-0914-1239"
    assembled = json.loads((folder / "assembled.json").read_text())
    assembled["goals"][3]["depends_on"] = []
    tmp = copy.deepcopy(assembled)
    from app.plan.generate import _build_plan  # noqa: F401  (同 _build，只换 assembled)

    skeleton = json.loads((folder / "skeleton.json").read_text())
    cards = []
    for path in sorted(folder.glob("entity-*.json"), key=lambda p: int(p.stem.split("-")[1])):
        card = json.loads(path.read_text()); card.setdefault("id", card["canonical"]); cards.append(card)
    plan = _build_plan(tmp, query="国内大家对豆包的看法", research_id="r-wx1-run-0914-1239",
                       timestamp="2026-09-14T00:00:00+00:00", scale="standard",
                       market_profile=skeleton["market_profile"],
                       market_profile_justification=skeleton["market_profile_justification"],
                       subjects=skeleton["subjects"], subjects_justification=skeleton["subjects_justification"],
                       entities=cards, repairs=[])
    notes = normalize_plan(plan, collection_plan=json.loads((folder / "allocation.json").read_text()))
    result["d067/wx1-run-0914-1239-no-upstream/standard"] = {
        "notes_sha": _sha(notes), "plan_sha": _sha(_without_prompts(plan.to_dict()))}
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--code-root", required=True)
    parser.add_argument("--out")
    args = parser.parse_args()
    sys.path.insert(0, str(Path(args.code_root).resolve()))
    import app.plan.normalize as normalize

    assert Path(normalize.__file__).resolve().is_relative_to(Path(args.code_root).resolve()), normalize.__file__
    reading = {"code_root": str(Path(args.code_root).resolve()), "target": measure_target(), "regression": measure_regression()}
    text = json.dumps(reading, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    target = reading["target"]
    print("空心 goal:", target["hollow_goals"], "剩余命中条数:", target["hollow_remaining_hit_items"])
    for goal_id in target["hollow_goals"]:
        g = target["goals"][goal_id]
        for item in g["remaining"]:
            if item["hits"]:
                print(f"  剩 {goal_id} {item['hits']} 「{item['text'][:40]}…」")
        for item in g["removed"]:
            print(f"  摘 {goal_id}[{item['index_draft']}] {item['hits']} 留痕带原文={item['note_has_original']}")
    for key, value in reading["regression"].items():
        print(key, value["notes_sha"][:12], value["plan_sha"][:12], value.get("goal4_removed", ""))


if __name__ == "__main__":
    main()
