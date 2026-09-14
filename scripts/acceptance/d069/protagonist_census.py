"""§D-069 尺子：离线重生分配表 + 原声闸名单，量「题面对比 X 不算主角」。零引擎费。

    ../Owli/.venv/bin/python scripts/acceptance/d069/protagonist_census.py dump  <out.json>
    ../Owli/.venv/bin/python scripts/acceptance/d069/protagonist_census.py check
    ../Owli/.venv/bin/python scripts/acceptance/d069/protagonist_census.py compare <golden.json>

读数走**生产路径**：`generate._protagonists`（分配表拿到的主角）→ `allocation._protagonist_first`
→ `allocate_collections`；原声闸读 `tables.quote_gate_names`。输入是真夹具：骨架 + 实体卡照
`generate_plan` 定稿分配表那一步喂（与 `tests/test_alloc3_standard_goal_nature.py::_run` 同形）。

⛔ **期望值一律人工写死**（主角、竞品、各自叫法、口碑/媒体 goal），按题面与夹具原文逐字抄，
不调 `subject_canonicals` 推——被测函数不能当尺子。

- `dump`：把全部夹具的读数写成 JSON（在 bbcf658 上跑一次即「改前 golden」）。
- `check`：只判 wx1-0914c（WX-1 第四轮 r-wx1-0914-1749）判据 1/2，打印逐条 PASS/FAIL，退出码 = FAIL 数。
- `compare`：除 wx1-0914c 外所有夹具的读数与 golden 逐字比较（判据 3 不退化），退出码 = 不同条数。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from app.config import load_research_scale_config  # noqa: E402
from app.plan.allocation import (  # noqa: E402
    _protagonist_first, allocate_collections, collection_plan_dict, ordered_sources,
)
from app.plan.generate import _protagonists  # noqa: E402
from app.report.polish.tables import quote_gate_names  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
DOUBAO_Q = "国内大家对豆包的看法"

# ---- 写死的期望（wx1-0914c，题面「国内大家对豆包的看法（对比 DeepSeek、Kimi、文心一言、通义千问）」）----
RED_CASE = "wx1-0914c/standard"
EXPECT_PROTAGONISTS = ["豆包"]
EXPECT_RIVALS = ["DeepSeek", "Kimi", "文心一言", "通义千问"]
DOUBAO_NAMES = {"豆包", "Doubao", "豆包AI", "字节豆包"}
RIVAL_NAMES = {
    "DeepSeek", "深度求索", "DeepSeek-V3", "DeepSeek-R1",
    "Kimi", "Kimi智能助手", "Kimi Chat", "kimi", "月之暗面Kimi", "MoonshotAI Kimi",
    "文心一言", "ERNIE Bot", "百度文心", "文心大模型",
    "通义千问", "Qwen", "Tongyi Qianwen",
}
#: goal-3「国内用户与媒体对豆包的评价与口碑」——骨架里唯一一个既是口碑类又是媒体类、且点名豆包的 goal。
WOM_GOAL = "goal-3"
MEDIA_GOAL = "goal-3"
ZH_UGC = ("xhs", "douyin", "weibo")
WECHAT = "wechat_mp"


def _plan_case(name: str) -> dict:
    """tests/fixtures/d069/<name>：plan.json（题面/subjects/实体卡）+ skeleton.json。"""
    base = FIXTURES / "d069" / name
    plan = json.loads((base / "plan.json").read_text(encoding="utf-8"))
    skeleton_path = base / "skeleton.json"
    skeleton = json.loads(skeleton_path.read_text(encoding="utf-8")) if skeleton_path.exists() else None
    return {"question": plan["research_question"], "plan": plan, "skeleton": skeleton,
            "subjects": plan["subjects"], "entities": plan["entities"],
            "market_profile": plan["market_profile"]}


def _segment_case(name: str, question: str) -> dict:
    """ALLOC 系老夹具：只有 plan-segments（skeleton + entity-N），题面按来源 worklog 写死。"""
    base = FIXTURES / name
    skeleton = json.loads((base / "skeleton.json").read_text(encoding="utf-8"))
    cards = []
    for index in range(1, 6):
        path = base / f"entity-{index}.json"
        if path.exists():
            card = json.loads(path.read_text(encoding="utf-8"))
            card.setdefault("id", card["canonical"])
            cards.append(card)
    plan = {"research_question": question, "title": question,
            "subjects": skeleton["subjects"], "entities": cards}
    return {"question": question, "plan": plan, "skeleton": skeleton,
            "subjects": skeleton["subjects"], "entities": cards,
            "market_profile": skeleton["market_profile"]}


def cases() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for name in ("wx1-0914", "wx1-0914b", "wx1-0914c"):
        out[f"{name}/standard"] = {**_plan_case(name), "scale": "standard"}
    out["r-50600e09f7dd/plan"] = {**_plan_case("r-50600e09f7dd"), "scale": None}
    out["alloc3-run-b/standard"] = {**_segment_case("alloc3-run-b", DOUBAO_Q), "scale": "standard"}
    for name, question in (("alloc2", DOUBAO_Q), ("d062fu", DOUBAO_Q), ("alloc3", DOUBAO_Q),
                           ("d062fu-run-a", "豆包语音输入法的竞品分析")):
        for scale in ("fast", "standard"):
            out[f"{name}/{scale}"] = {**_segment_case(name, question), "scale": scale}
    return out


def reading(case: dict) -> dict:
    subjects, entities = case["subjects"], case["entities"]
    protagonists = _protagonists(case["question"], subjects, entities)
    out: dict = {"protagonists": protagonists, "quote_gate_names": quote_gate_names(case["plan"])}
    if case["skeleton"] is None or case["scale"] is None:
        return out
    profile = load_research_scale_config().profile(case["scale"])
    first = _protagonist_first(subjects, case["market_profile"],
                               ordered_sources(case["market_profile"], entities),
                               entities, protagonists, profile, scale=case["scale"])
    scaffolds = [{"title": g["title"], "objective": g["objective"],
                  "depends_on": g["depends_on"], "subjects": list(subjects)}
                 for g in case["skeleton"]["goals"]]
    skipped: list[dict[str, str]] = []
    baseline: list[dict[str, str]] = []
    plan = collection_plan_dict(allocate_collections(
        subjects, case["market_profile"], scaffolds, profile, entities,
        scale=case["scale"], entity_slot_target=len(case["skeleton"]["subjects"]),
        skipped=skipped, protagonists=protagonists, baseline=baseline,
    ))
    out.update({
        "protagonist_first": list(first[:1]) + [list(first[1]), list(first[2])] if first else None,
        "allocation": plan,
        "allocation_short": {g: [f"{s['source_id']}·{s['entity']}" for s in slots]
                             for g, slots in plan.items()},
        "skipped": skipped, "baseline": baseline,
    })
    return out


def check() -> int:
    r = reading(cases()[RED_CASE])
    plan = r["allocation"]
    pairs = {(g, s["source_id"], s["entity"]) for g, slots in plan.items() for s in slots}
    doubao_cards = sorted(f"{g}:{src}" for g, src, ent in pairs if ent == "豆包")
    names = set(r["quote_gate_names"])
    verdicts = [
        ("主角表 = [豆包]", r["protagonists"] == EXPECT_PROTAGONISTS, r["protagonists"]),
        ("主角优先生效（_protagonist_first 非 None 且 lead=豆包）",
         bool(r["protagonist_first"]) and r["protagonist_first"][0] == "豆包", r["protagonist_first"]),
        *[(f"口碑 goal {WOM_GOAL} 有 {src}·豆包", (WOM_GOAL, src, "豆包") in pairs, plan[WOM_GOAL])
          for src in ZH_UGC],
        (f"媒体 goal {MEDIA_GOAL} 有 {WECHAT}·豆包", (MEDIA_GOAL, WECHAT, "豆包") in pairs, plan[MEDIA_GOAL]),
        ("竞品卡仍在（四家各 ≥1 张）",
         all(any(ent == rival for _, _, ent in pairs) for rival in EXPECT_RIVALS),
         sorted({ent for _, _, ent in pairs})),
        ("原声闸名单 = 豆包全部叫法", names == DOUBAO_NAMES, sorted(names)),
        ("原声闸名单不含任何竞品叫法", not (names & RIVAL_NAMES), sorted(names & RIVAL_NAMES)),
    ]
    print(f"[{RED_CASE}] 豆包卡：{doubao_cards}")
    print(f"[{RED_CASE}] 分配表：{json.dumps(r['allocation_short'], ensure_ascii=False)}")
    fails = 0
    for label, ok, seen in verdicts:
        fails += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {label}  ← {json.dumps(seen, ensure_ascii=False)}")
    print(f"FAIL 数 = {fails}")
    return fails


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "check"
    if mode == "dump":
        data = {name: reading(case) for name, case in cases().items()}
        Path(sys.argv[2]).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"dump {len(data)} 份 → {sys.argv[2]}")
        return 0
    if mode == "check":
        return check()
    if mode == "compare":
        golden = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
        diffs = 0
        for name, case in cases().items():
            if name == RED_CASE:
                continue
            same = reading(case) == golden[name]
            diffs += not same
            print(f"{'SAME' if same else 'DIFF'}  {name}")
        print(f"不同 = {diffs}")
        return diffs
    raise SystemExit(f"未知模式 {mode}")


if __name__ == "__main__":
    raise SystemExit(main())
