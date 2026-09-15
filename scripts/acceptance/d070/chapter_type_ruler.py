"""§D-070 尺子：离线重放规划章阶段，量「撰写章被标 collection」与规则 22。零引擎费。

    ../Owli/.venv/bin/python scripts/acceptance/d070/chapter_type_ruler.py dump <out.json>
    ../Owli/.venv/bin/python scripts/acceptance/d070/chapter_type_ruler.py check [<out.json>]
    ../Owli/.venv/bin/python scripts/acceptance/d070/chapter_type_ruler.py compare <old.json> <new.json>

重放走**生产路径**，与 `generate.generate_plan` 章阶段同形：`_build_plan(assembled)` →
`normalize_plan` → `generate_chapter_specs`（假适配器按段名回放夹具里的 `goal-N-ch-M.json`
原文）→ `normalize_plan` → `lint`。夹具是 WX-1 六轮规划产物的只读拷贝（`tests/fixtures/d070/`），
题面 / 时间戳 / 档位来自各轮库的 reports 行（`meta.json`）。

⛔ 期望值一律**人工写死**：「r-82ac9ebbd0f0 的 goal-3/ch-5 是撰写章」由读 `goal-3.json` 与
`goals/goal-3/ch-5.md` 原文（display_name=报告撰写、agent_id=report-writing-3、产物即 goal-3
交付物）得出，不调被测的章类型校正函数推；引擎期望按 `goal-2/ch-5`、`goal-5/ch-5` 同位撰写章的
读数写死为 claude。

- `dump`：所有夹具逐章读数（章类型 / 引擎 / 开头结尾 sha / 校正记录）+ lint 错误原文。
- `check`：只判 r-82ac9ebbd0f0 判据 1，逐条 PASS/FAIL，退出码 = FAIL 数。
- `compare`：两份 dump 除 r-82ac9ebbd0f0 外逐字比较（判据 2 不退化），退出码 = 不同条数。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from app.adapters.contracts import PlanningSegmentResult  # noqa: E402
from app.config import (  # noqa: E402
    ChapterEngineConfig, ResilienceConfig, load_research_scale_config,
)
from app.plan.allocation import per_goal_capacity  # noqa: E402
from app.plan.chapters import generate_chapter_specs  # noqa: E402
from app.plan.generate import _build_plan  # noqa: E402
from app.plan.lint import lint  # noqa: E402
from app.plan.normalize import normalize_plan  # noqa: E402
from app.plan.segments import PlanSegmentWorkspace  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "d070"
RED_CASE = "r-82ac9ebbd0f0"

# ---- 写死的期望（人工读原文得出，不调被测函数）----
WRITER = "goal-3/ch-5"
WRITER_AGENT_ID = "report-writing-3"
WRITER_OUTPUT = "goals/goal-3/goal-3-kimi-reputation.json"
PEERS = ("goal-2/ch-5", "goal-5/ch-5")  # 同卷同位撰写章，原本就标对了
EXPECT_TYPE = "report"
EXPECT_ENGINE = "claude"


def _sha(value) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()[:16]


class ReplayAdapter:
    """按段名回放夹具章段原文；缺段就记下来并报错（不编造）。"""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.missing: list[str] = []
        self.calls: list[str] = []

    async def run_planning_segment(self, request, on_text=None):
        name = request.segment_name
        self.calls.append(name)
        path = self.folder / f"{name}.json"
        if not path.is_file():
            self.missing.append(name)
            return PlanningSegmentResult("", False, error=f"夹具缺段 {name}")
        text = path.read_text(encoding="utf-8")
        if on_text is not None:
            await on_text(text)
        return PlanningSegmentResult(text, True)


def _build(folder: Path):
    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    skeleton = json.loads((folder / "skeleton.json").read_text(encoding="utf-8"))
    cards = []
    for path in sorted(folder.glob("entity-*.json"), key=lambda p: int(p.stem.split("-")[1])):
        card = json.loads(path.read_text(encoding="utf-8"))
        card.setdefault("id", card["canonical"])
        cards.append(card)
    scale_config = load_research_scale_config()
    plan = _build_plan(
        json.loads((folder / "assembled.json").read_text(encoding="utf-8")),
        query=meta["query"],
        research_id=meta["research_id"],
        timestamp=meta["timestamp"],
        scale=meta["scale"],
        scale_config=scale_config,
        market_profile=skeleton["market_profile"],
        market_profile_justification=skeleton["market_profile_justification"],
        subjects=skeleton["subjects"],
        subjects_justification=skeleton["subjects_justification"],
        entities=cards,
        repairs=[],
    )
    collection_plan = json.loads((folder / "allocation.json").read_text(encoding="utf-8"))
    capacity = per_goal_capacity(scale_config.profile(meta["scale"]))
    profile = scale_config.profile(meta["scale"])
    return plan, collection_plan, capacity, profile.max_chapters_per_goal


def measure(folder: Path) -> dict:
    plan, collection_plan, capacity, max_chapters = _build(folder)
    normalize_plan(plan, collection_plan=collection_plan, per_goal_capacity=capacity)
    adapter = ReplayAdapter(folder)
    with tempfile.TemporaryDirectory() as tmp:
        workspace = PlanSegmentWorkspace(
            Path(tmp) / "runs" / plan.research_id, ResilienceConfig(3, 60, 900),
        )
        error = ""
        try:
            asyncio.run(generate_chapter_specs(
                plan, workspace, adapter, ChapterEngineConfig(),
            ))
        except Exception as exc:  # noqa: BLE001 — 读数要记下异常原文
            error = f"{type(exc).__name__}: {exc}"
    notes = normalize_plan(plan, collection_plan=collection_plan, per_goal_capacity=capacity)
    errors = lint(plan, max_chapters_per_goal=max_chapters, collection_plan=collection_plan)["errors"] if not error else []
    chapters = {}
    for goal in plan.goals:
        for index, agent in enumerate(goal.agents, start=1):
            chapter = agent.chapter or {}
            closing = dict(chapter.get("closing") or {})
            notes_obj = dict(closing.get("notes") or {})
            correction = notes_obj.pop("chapter_type_correction", None)
            closing["notes"] = notes_obj
            chapters[f"{goal.goal_id}/ch-{index}"] = {
                "agent_id": agent.agent_id,
                "profile": (agent.capability or {}).get("profile"),
                "chapter_type": chapter.get("chapter_type"),
                "engine": agent.engine,
                "output_path": str((agent.output or {}).get("path", "")),
                "opening_sha": _sha(chapter.get("opening")),
                "closing_sha_without_correction": _sha(closing),
                "correction": correction,
            }
    return {
        "generate_error": error,
        "missing_segments": adapter.missing,
        "engine_calls": len(adapter.calls),
        "chapters": chapters,
        "normalize_notes_sha": _sha(notes),
        "lint_errors": errors,
        "rule22": [item for item in errors if item.startswith("[规则22]")],
    }


def measure_saved(folder: Path) -> dict:
    """当轮通过 lint 的已存计划：逐章按生产校验重走一遍章规格，比章类型 / 引擎 / 整卷 lint。

    0913-1355 / 0914-1239 两轮在现码下重建出的计划形状已与当时不同（normalize 后续改过），
    章段回放对不上号；已存计划是这两轮唯一保真的对照物。
    """
    from app.plan.chapters import validate_chapter_value
    from app.plan.model import Plan

    raw = json.loads((folder / "plan.json").read_text(encoding="utf-8"))
    plan = Plan.from_dict(raw)
    engines = ChapterEngineConfig()
    chapters = {}
    for goal in plan.goals:
        for index, agent in enumerate(goal.agents, start=1):
            saved = agent.chapter or {}
            key = f"{goal.goal_id}/ch-{index}"
            try:
                value = validate_chapter_value(
                    {k: saved[k] for k in ("chapter_type", "opening", "closing")}, agent,
                )
            except Exception as exc:  # noqa: BLE001
                chapters[key] = {"error": f"{type(exc).__name__}: {exc}"}
                continue
            closing = dict(value["closing"])
            notes_obj = dict(closing.get("notes") or {})
            correction = notes_obj.pop("chapter_type_correction", None)
            closing["notes"] = notes_obj
            shell = str((agent.capability or {}).get("shell", "none"))
            chapters[key] = {
                "agent_id": agent.agent_id,
                "saved_type": saved.get("chapter_type"),
                "saved_engine": agent.engine,
                "chapter_type": value["chapter_type"],
                "engine": "codex" if shell != "none" else engines.engine_for(value["chapter_type"]),
                "opening_sha": _sha(value["opening"]),
                "closing_sha_without_correction": _sha(closing),
                "correction": correction,
            }
    return {"chapters": chapters, "lint_errors": lint(plan)["errors"]}


def dump(out: Path) -> dict:
    result = {folder.name: measure(folder) for folder in sorted(FIXTURES.iterdir()) if folder.is_dir()}
    for folder in sorted(FIXTURES.iterdir()):
        if (folder / "plan.json").is_file():
            result[f"{folder.name}/saved-plan"] = measure_saved(folder)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def check(reading: dict) -> int:
    red = reading[RED_CASE]
    chapters = red["chapters"]
    rows = [
        ("章阶段无异常、无缺段", not red["generate_error"] and not red["missing_segments"],
         f"error={red['generate_error']!r} missing={red['missing_segments']}"),
        ("goal-3/ch-5 是 report-writing-3、产物即 goal-3 交付物",
         chapters[WRITER]["agent_id"] == WRITER_AGENT_ID and chapters[WRITER]["output_path"] == WRITER_OUTPUT,
         f"{chapters[WRITER]['agent_id']} {chapters[WRITER]['output_path']}"),
        ("规则 22 = 0 条", len(red["rule22"]) == 0, f"{len(red['rule22'])} 条"),
        (f"goal-3/ch-5 章类型 = {EXPECT_TYPE}", chapters[WRITER]["chapter_type"] == EXPECT_TYPE,
         chapters[WRITER]["chapter_type"]),
        (f"goal-3/ch-5 引擎 = {EXPECT_ENGINE}", chapters[WRITER]["engine"] == EXPECT_ENGINE,
         chapters[WRITER]["engine"]),
    ]
    for peer in PEERS:
        rows.append((
            f"{WRITER} 引擎与同位 {peer} 同",
            chapters[WRITER]["engine"] == chapters[peer]["engine"]
            and chapters[peer]["chapter_type"] == EXPECT_TYPE,
            f"{peer}={chapters[peer]['chapter_type']}/{chapters[peer]['engine']}",
        ))
    fails = 0
    for label, ok, detail in rows:
        fails += 0 if ok else 1
        print(("PASS " if ok else "FAIL ") + label + f"  [{detail}]")
    print(f"lint errors 共 {len(red['lint_errors'])} 条；规则 22 原文：")
    for item in red["rule22"]:
        print("  " + item)
    return fails


def compare(old: dict, new: dict) -> int:
    diffs = 0
    for name in sorted(set(old) | set(new)):
        if name == RED_CASE:
            continue
        a, b = old.get(name), new.get(name)
        if a == b:
            corrections = [k for k, v in (b or {}).get("chapters", {}).items() if v["correction"]]
            print(
                f"SAME {name}  章 {len(b['chapters'])}  lint {len(b['lint_errors'])}  "
                f"校正 {len(corrections)}  异常/缺段 {bool(b.get('generate_error'))}"
            )
            continue
        diffs += 1
        print(f"DIFF {name}")
        for key in sorted(set(a or {}) | set(b or {})):
            if (a or {}).get(key) != (b or {}).get(key):
                print(f"  {key}: old={json.dumps((a or {}).get(key), ensure_ascii=False)[:400]}")
                print(f"  {' ' * len(key)}  new={json.dumps((b or {}).get(key), ensure_ascii=False)[:400]}")
    return diffs


def main() -> None:
    command = sys.argv[1]
    if command == "dump":
        dump(Path(sys.argv[2]))
    elif command == "check":
        reading = (
            json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
            if len(sys.argv) > 2 else {RED_CASE: measure(FIXTURES / RED_CASE)}
        )
        raise SystemExit(check(reading))
    elif command == "compare":
        old = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
        new = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))
        raise SystemExit(compare(old, new))
    else:
        raise SystemExit(f"未知命令 {command}")


if __name__ == "__main__":
    main()
