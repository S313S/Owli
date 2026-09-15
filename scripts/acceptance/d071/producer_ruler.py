"""§D-071 尺子：离线重放规划期到 normalize + lint，量「交付物有没有章产出」。零引擎费。

    <python> scripts/acceptance/d071/producer_ruler.py dump <code_root> <out.json>
    <python> scripts/acceptance/d071/producer_ruler.py check <old.json> <new.json>
    <python> scripts/acceptance/d071/producer_ruler.py gate <old.json>
    <python> scripts/acceptance/d071/producer_ruler.py compare <old.json> <new.json>

`code_root` 是被测代码树（旧码 711c86b 副本 / 新码 ../Owli-d071）；夹具一律读**本脚本所在树**
的 `tests/fixtures/d071/`、`tests/fixtures/d070/`（两棵树 d070 夹具逐字同）。
构建沿用 `scripts/acceptance/d070/chapter_type_ruler.py` 的 `_build` / `ReplayAdapter`，从被测
代码树导入——即走那棵树自己的 `_build_plan` → `normalize_plan` →（章段回放）→ `normalize_plan` → `lint`。

⛔ 期望值**人工写死**，不调被测函数推：读 r-d9c69fb6132a 规划段 `goal-3.json` 原文（数据清洗 /
交叉验证 / 报告撰写三章，交付物 kimi-profile.json）、旧码 normalize 前的 agent 清单（这三章的
agent_id）与存盘计划 `plan.json` 里 goal-6 各章 `inputs`（from_goal=goal-3 的两份路径）。

- `dump`：每份夹具两个读数——`segment`（normalize + lint，不走章段）与 `chapters`（再走章段回放）：
  逐 goal 章清单（agent_id / depends_on / inputs / 章类型 / 产物路径）+ lint 错误原文；新码另记规则 34。
- `check`：判据 1，逐条 PASS/FAIL，退出码 = FAIL 数（旧码读数应红、新码读数应绿）。
- `gate`：判据 2，旧码 normalize 后的计划对象喂**本树**新 lint（开规则 34），应报 goal-3 一条。
- `compare`：判据 3，除红例外逐字比较，退出码 = 不同条数。
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[3]
RED_CASE = "r-d9c69fb6132a"
FIXTURES = [HERE / "tests" / "fixtures" / "d071" / RED_CASE] + sorted(
    p for p in (HERE / "tests" / "fixtures" / "d070").iterdir() if p.is_dir()
)

# ---- 写死的期望（人工读原文得出）----
GOAL = "goal-3"
DELIVERABLE = "goals/goal-3/kimi-profile.json"
WRITER = "report-writing-3"
CHAIN = ["data-cleaning-3", "cross-validation-2", "report-writing-3"]
CLEANER = "data-cleaning-3"
CLEANER_UPSTREAM_ANY = {"data-collection-16", "reliability-audit-16"}
GOAL6_READS_FROM_GOAL3 = ["goals/goal-3/kimi-profile.json", "goals/goal-3/data-collection-16.json"]


def _load_base(code_root: Path):
    sys.path.insert(0, str(code_root / "scripts" / "acceptance" / "d070"))
    sys.path.insert(0, str(code_root))
    import chapter_type_ruler as base  # noqa: PLC0415 — 按被测树导入
    assert Path(base.ROOT).resolve() == code_root.resolve(), (base.ROOT, code_root)
    return base


def _snapshot(plan, lint_fn, max_chapters, collection_plan) -> dict:
    goals = {}
    for goal in plan.goals:
        goals[goal.goal_id] = {
            "deliverable": str((goal.deliverable or {}).get("path", "")),
            "depends_on": list(goal.depends_on),
            "agents": [
                {
                    "agent_id": agent.agent_id,
                    "depends_on": list(agent.depends_on),
                    "inputs": [dict(item) for item in agent.inputs],
                    "chapter_type": (agent.chapter or {}).get("chapter_type"),
                    "output_path": str((agent.output or {}).get("path", "")),
                }
                for agent in goal.agents
            ],
        }
    reading = {
        "goals": goals,
        "lint_errors": lint_fn(
            plan, max_chapters_per_goal=max_chapters, collection_plan=collection_plan,
        )["errors"],
    }
    try:
        reading["rule34"] = lint_fn(
            plan, max_chapters_per_goal=max_chapters, collection_plan=collection_plan,
            require_deliverable_producer=True,
        )["errors"]
    except TypeError:  # 旧码 lint 没有这个开关
        reading["rule34"] = None
    return reading


def measure(base, folder: Path) -> dict:
    from app.config import ChapterEngineConfig, ResilienceConfig
    from app.plan.chapters import generate_chapter_specs
    from app.plan.lint import lint
    from app.plan.normalize import normalize_plan
    from app.plan.segments import PlanSegmentWorkspace

    plan, collection_plan, capacity, max_chapters = base._build(folder)
    notes = normalize_plan(plan, collection_plan=collection_plan, per_goal_capacity=capacity)
    segment = _snapshot(plan, lint, max_chapters, collection_plan)
    segment["notes"] = notes
    segment["plan"] = plan.to_dict()
    adapter = base.ReplayAdapter(folder)
    error = ""
    with tempfile.TemporaryDirectory() as tmp:
        workspace = PlanSegmentWorkspace(Path(tmp) / "runs" / plan.research_id, ResilienceConfig(3, 60, 900))
        try:
            asyncio.run(generate_chapter_specs(plan, workspace, adapter, ChapterEngineConfig()))
        except Exception as exc:  # noqa: BLE001 — 读数要记异常原文
            error = f"{type(exc).__name__}: {exc}"
    chapters = {"generate_error": error, "missing_segments": adapter.missing}
    if not error:
        normalize_plan(plan, collection_plan=collection_plan, per_goal_capacity=capacity)
        chapters.update(_snapshot(plan, lint, max_chapters, collection_plan))
    return {"segment": segment, "chapters": chapters}


def dump(code_root: Path, out: Path) -> None:
    base = _load_base(code_root)
    result = {"code_root": str(code_root)}
    for folder in FIXTURES:
        result[folder.name] = measure(base, folder)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _judge(reading: dict) -> list[tuple[str, bool, str]]:
    seg = reading[RED_CASE]["segment"]
    goal = seg["goals"][GOAL]
    ids = [agent["agent_id"] for agent in goal["agents"]]
    by_id = {agent["agent_id"]: agent for agent in goal["agents"]}
    producers = [agent["agent_id"] for agent in goal["agents"] if agent["output_path"] == DELIVERABLE]
    outputs = {a["output_path"] for g in seg["goals"].values() for a in g["agents"]}
    cleaner_up = set(by_id.get(CLEANER, {}).get("depends_on", []))
    return [
        (f"{GOAL} 交付物路径 = {DELIVERABLE}", goal["deliverable"] == DELIVERABLE, goal["deliverable"]),
        (f"{GOAL} 清洗/交叉/撰写三章都在", all(item in ids for item in CHAIN), " ".join(ids)),
        (f"{GOAL} 交付物恰由 {WRITER} 产出", producers == [WRITER], str(producers)),
        (f"{CLEANER} 上游含 {sorted(CLEANER_UPSTREAM_ANY)} 之一", bool(cleaner_up & CLEANER_UPSTREAM_ANY),
         str(sorted(cleaner_up))),
        ("goal-6 读的两份 goal-3 输入都有章产出", all(path in outputs for path in GOAL6_READS_FROM_GOAL3),
         str([path for path in GOAL6_READS_FROM_GOAL3 if path not in outputs]) + " 缺"),
        ("lint 0 条", seg["lint_errors"] == [], f"{len(seg['lint_errors'])} 条"),
    ]


def check(old: dict, new: dict) -> int:
    fails = 0
    for label, reading, want_green in (("旧码", old, False), ("新码", new, True)):
        print(f"== {label} {reading['code_root']}")
        rows = _judge(reading)
        for text, ok, detail in rows:
            print(("PASS " if ok else "FAIL ") + text + f"  [{detail}]")
        red = not all(ok for _, ok, _ in rows[1:5])
        if want_green:
            fails += sum(0 if ok else 1 for _, ok, _ in rows)
        else:
            print(f"旧码应红：{'是' if red else '否'}；lint 条数 {len(reading[RED_CASE]['segment']['lint_errors'])}")
            fails += 0 if red and reading[RED_CASE]["segment"]["lint_errors"] == [] else 1
    return fails


def gate(old: dict) -> int:
    sys.path.insert(0, str(HERE))
    from app.plan.lint import lint
    from app.plan.model import Plan

    seg = old[RED_CASE]["segment"]
    folder = HERE / "tests" / "fixtures" / "d071" / RED_CASE
    collection_plan = json.loads((folder / "allocation.json").read_text(encoding="utf-8"))
    plan = Plan.from_dict(seg["plan"])
    errors = lint(plan, collection_plan=collection_plan, require_deliverable_producer=True)["errors"]
    hits = [item for item in errors if item.startswith("[规则34]")]
    others = [item for item in errors if not item.startswith("[规则34]")]
    for item in errors:
        print("  " + item)
    ok = len(hits) == 1 and hits[0].startswith(f"[规则34] {GOAL} ") and DELIVERABLE in hits[0] and not others
    print(("PASS" if ok else "FAIL") + f" 旧码计划喂新 lint：规则 34 {len(hits)} 条、其余 {len(others)} 条")
    return 0 if ok else 1


def compare(old: dict, new: dict) -> int:
    diffs = 0
    for name in [folder.name for folder in FIXTURES]:
        if name == RED_CASE:
            continue
        for stage in ("segment", "chapters"):
            a = {k: v for k, v in old[name][stage].items() if k not in ("rule34", "plan")}
            b = {k: v for k, v in new[name][stage].items() if k not in ("rule34", "plan")}
            r34 = new[name][stage].get("rule34")
            extra = [item for item in (r34 or []) if item.startswith("[规则34]")]
            if a == b:
                print(f"SAME {name}/{stage}  goal {len(b.get('goals', {}))}  "
                      f"章 {sum(len(g['agents']) for g in b.get('goals', {}).values())}  "
                      f"lint {len(b.get('lint_errors', []))}  规则34 {len(extra)}  "
                      f"异常 {bool(b.get('generate_error'))}")
                if extra:
                    diffs += 1
                    for item in extra:
                        print("  规则34: " + item)
                continue
            diffs += 1
            print(f"DIFF {name}/{stage}")
            for key in sorted(set(a) | set(b)):
                if a.get(key) != b.get(key):
                    print(f"  {key}: old={json.dumps(a.get(key), ensure_ascii=False)[:600]}")
                    print(f"  {' ' * len(key)}  new={json.dumps(b.get(key), ensure_ascii=False)[:600]}")
    return diffs


def main() -> None:
    command = sys.argv[1]
    if command == "dump":
        dump(Path(sys.argv[2]).resolve(), Path(sys.argv[3]))
        return
    load = lambda index: json.loads(Path(sys.argv[index]).read_text(encoding="utf-8"))  # noqa: E731
    if command == "check":
        raise SystemExit(check(load(2), load(3)))
    if command == "gate":
        raise SystemExit(gate(load(2)))
    if command == "compare":
        raise SystemExit(compare(load(2), load(3)))
    raise SystemExit(f"未知命令 {command}")


if __name__ == "__main__":
    main()
