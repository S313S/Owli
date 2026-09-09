#!/usr/bin/env python3
"""起跑前体检：在付掉一轮 40–170 分钟引擎之前，用几秒钟查出秒级可查的死因。

**⛔ 新增检查一律加进这一份，禁止再在 `scripts/acceptance/<包名>/` 下另造一套**
（那下面 31 个包目录各一套尺子、关账即死）。详见同目录 `README.md`。

零引擎：本脚本一次模型调用都不发；⑥ 只**构造**适配器，不发请求。

    python3 scripts/preflight/preflight.py all \
        --db var/shard1-serve.db --research r-3e04f808dffd \
        --goal goal-3 --chapter ch-6 --section sec-2 \
        --scale standard --mode section-replay --runs runs
    python3 scripts/preflight/preflight.py adapter      # 单项也能跑
    python3 scripts/preflight/preflight.py list         # 列出八项

退出码：0=八项全 PASS；1=有 FAIL；3=没 FAIL 但有 SKIP（**SKIP 不算过**）。
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


@dataclass
class Result:
    """一项检查的结论。`numbers` 是量出来的数——给判据不要只给结论。"""

    check: str
    status: str
    headline: str
    numbers: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def ok(check: str, headline: str, **numbers: Any) -> "Result":
        return Result(check, PASS, headline, numbers)

    @staticmethod
    def red(check: str, headline: str, **numbers: Any) -> "Result":
        return Result(check, FAIL, headline, numbers)

    @staticmethod
    def skip(check: str, why: str) -> "Result":
        return Result(check, SKIP, why, {})


# --------------------------------------------------------------------------
# 共用零件
# --------------------------------------------------------------------------

def _store(database: Path) -> Any:
    """读库一律用 `Store`，不用 `ReadOnlyStore`。

    后者不解 `extra`，同一个库能量出 0 和 287 两个数（记忆 `verification-path-seam`）。
    ①⑤ 都要读 `extra`，走错一个就静默假绿。
    """

    from app.store.dao import Store

    return Store(database)


def _plan_snapshot(database: Path, research_id: str) -> dict[str, Any]:
    """从库里取 plan_snapshot——**跑的时候读的是这一份**，不是 `app/config.py`。"""

    report = _store(database).get_report(research_id)
    if report is None:
        raise KeyError(f"库里没有这份研究：{research_id}")
    snapshot = report.get("plan_snapshot")
    if not snapshot:
        raise KeyError(f"{research_id} 没有 plan_snapshot")
    return dict(snapshot)


def _plan_of(database: Path, research_id: str) -> Any:
    """plan_snapshot → `Plan`。用真模型解析，不自己拆 dict。"""

    from app.plan.model import Plan

    return Plan.from_dict(_plan_snapshot(database, research_id))


def _goal_and_agent(plan: Any, goal_id: str, chapter_id: str) -> tuple[Any, Any]:
    """沿用重放那条路的定位函数，别再写一份（记忆 `reuse-before-rewrite`）。"""

    from app.replay.section import _resolve_agent

    return _resolve_agent(plan, goal_id, chapter_id)


def _section_ids(plan: Any, agent: Any) -> list[str]:
    """该章在计划里的节 id 全名（`goal-3/ch-6/sec-2` 这种）。"""

    from app.orchestrator.sectioning import _section_specs

    return [str(spec["section_id"]) for spec in _section_specs(plan, agent)]


# --------------------------------------------------------------------------
# ① 会不会回填（出处：D-043 —— `/stop` 掐到收尾期回填）
# --------------------------------------------------------------------------

#: 只有走到**收尾**的跑法才会碰 `_backfill_ratings_on_finalize`
#: （`app/orchestrator/runtime.py:2802`，收尾期无条件跑）。
#: 重放单节走的是 `runtime._run_task`，正式稿走 `polish()`——两条都不收尾。
_FINALIZING_MODES = {"fullrun"}
_BACKFILL_BATCH = 25          # `backfill_report(batch_size=25)` 的默认值


def check_backfill(args: argparse.Namespace) -> Result:
    """会不会回填；会，就把回填耗时算进预算。"""

    import os

    if not (args.db and args.research):
        return Result.skip("① 会不会回填", "缺 --db / --research")
    mode = args.mode or "fullrun"
    if os.getenv("OWLI_SKIP_RATING_BACKFILL") == "1":
        return Result.ok("① 会不会回填", "不回填：OWLI_SKIP_RATING_BACKFILL=1",
                         mode=mode, reason="env_skip")
    if mode not in _FINALIZING_MODES:
        return Result.ok("① 会不会回填", f"不回填：{mode} 不走收尾",
                         mode=mode, finalizing_modes=sorted(_FINALIZING_MODES))

    from app.reliability.backfill import (
        SCORE_FIELDS, _already_agent_rated, _crossref_verdict,
        _wants_representativeness,
    )

    rows = [dict(row) for row in (_store(Path(args.db)).list_evidence(args.research) or [])]
    hot = [
        row for row in rows
        if not _already_agent_rated(row)
        and (
            any(row.get(f) is None for f in SCORE_FIELDS)
            or _crossref_verdict(row.get("extra") if isinstance(row.get("extra"), dict) else {}) is None
            or _wants_representativeness(row)
        )
    ]
    batches = math.ceil(len(hot) / _BACKFILL_BATCH)
    # 判红不是「会回填」——回填本来就该跑；红的是「会回填却没算进预算」。
    status_ok = bool(args.budget_minutes) and args.budget_minutes >= batches * 2
    headline = (
        f"会回填：{len(hot)}/{len(rows)} 条待评 ≈ {batches} 批"
        + ("（已算进预算）" if status_ok else "，**未算进预算**：用 --budget-minutes 报出你给的分钟数")
    )
    return (Result.ok if status_ok else Result.red)(
        "① 会不会回填", headline, mode=mode, evidence_rows=len(rows),
        backfill_rows=len(hot), batches=batches,
        budget_minutes=args.budget_minutes, note="簇补的行未计入，实际只多不少",
    )


# --------------------------------------------------------------------------
# ② 未重写的节会不会撞号（出处：D-056 + 09-08 落点事故）
# --------------------------------------------------------------------------

def _section_markdown(path: Path) -> str:
    """节产物有两种形态，都要认（09-08 真实状态上现形）：

    - 纯 Markdown（`sec-1.md` 那种占位节）
    - `{"markdown": ..., "claims": [...]}` 的 JSON 信封（`sec-3.md` 那种成稿节）

    只认前一种，31 KB 带 30 个角标的节会被读成「0 条角标」——
    静默失效而表现为「平安无事」。
    """

    if not path.is_file():
        return ""
    raw = path.read_text(encoding="utf-8")
    if raw.lstrip().startswith("{"):
        try:
            document = json.loads(raw)
        except json.JSONDecodeError:
            return raw
        if isinstance(document, dict) and isinstance(document.get("markdown"), str):
            return document["markdown"]
    return raw


def _is_placeholder(text: str) -> bool:
    """占位节：去掉标题行后只剩「- 此处缺失：…；原因：…」一行。

    沿用 `render._PLACEHOLDER` 那把尺子，别自己再认一遍这个句式。
    """

    from app.report.render import _PLACEHOLDER

    body = [line for line in text.splitlines() if line.strip() and not line.startswith("#")]
    return len(body) == 1 and _PLACEHOLDER.match(body[0]) is not None


def _marks_in(path: Path) -> dict[int, str]:
    """一份节产物「信息源」段里的 号 → URL。号是号，URL 才是身份。"""

    from app.report.markdown import _LINK_URL, _MARK, _split_section_structures

    _, _, source_lines = _split_section_structures(_section_markdown(path))
    out: dict[int, str] = {}
    for line in source_lines:
        mark, url = _MARK.search(line), _LINK_URL.search(line)
        if mark and url:
            out[int(mark.group(0)[2:-1])] = url.group(1)
    return out


def check_citation_collision(args: argparse.Namespace) -> Result:
    """不重写的节里已有的号，与本轮要发给重写节的号，同号不同源即判红。

    09-08 的事故形状：同一份工作稿里 `[S07]` 指两条不同的源，**一半对一半错且零报错**。
    """

    keep = [Path(p) for p in (args.keep_section or [])]
    if not keep or not args.citation_numbers:
        return Result.skip("② 未重写节撞号", "缺 --keep-section / --citation-numbers")
    planned: dict[str, int] = json.loads(Path(args.citation_numbers).read_text(encoding="utf-8"))
    by_number: dict[int, str] = {int(no): url for url, no in planned.items()}
    clashes: list[dict[str, Any]] = []
    empty: list[str] = []
    placeholders: list[str] = []
    for path in keep:
        marks = _marks_in(path)
        if not marks:
            # 占位节本来就没角标，撞不了号；**别的**读不出才是可疑的
            # ——多半是路径错了或那节格式变了，静默判绿正是本项目一天现形
            # 七次的那一族（`green-on-existence-not-production`）。
            (placeholders if _is_placeholder(_section_markdown(path)) else empty).append(path.name)
        for number, url in marks.items():
            other = by_number.get(number)
            if other is not None and other != url:
                clashes.append({"mark": f"[S{number:02d}]", "不重写的节指向": url,
                                "本轮要给的": other, "节": path.name})
    if empty:
        return Result.red("② 未重写节撞号",
                          f"{len(empty)} 个不重写的节里一条角标都读不出：{'、'.join(empty)}"
                          "——多半是路径错了或那节格式变了，这时候判绿是假绿",
                          读不出角标的节=empty, keep_sections=len(keep))
    if clashes:
        return Result.red("② 未重写节撞号",
                          f"{len(clashes)} 个角标同号不同源（一半对一半错、零报错）",
                          keep_sections=len(keep), clashes=clashes)
    return Result.ok(
        "② 未重写节撞号",
        f"{len(keep)} 节、{len(by_number)} 个计划号，0 撞号"
        + (f"（其中 {len(placeholders)} 节是占位节、本就无角标：{'、'.join(placeholders)}）"
           if placeholders else ""),
        keep_sections=[p.name for p in keep], planned_numbers=len(by_number),
        占位节=placeholders)


# --------------------------------------------------------------------------
# ③ 时间预算乘法（出处：09-08 sec-2 死在片墙钟，不是节墙钟）
# --------------------------------------------------------------------------

def check_time_budget(args: argparse.Namespace) -> Result:
    """节预算 vs 片墙钟 × 预计片数；后者 ≥ 前者即判红并报出差额。

    分片有**两层**墙钟，只放宽其中一层无效：`sectioning._section_shard_budget`
    按 `min(SHARD_ENGINE_CAP_SECONDS, 节墙钟)` 给每片发时间，节预算先掐。
    """

    if not (args.db and args.research and args.goal):
        return Result.skip("③ 时间预算乘法", "缺 --db / --research / --goal")

    from app.orchestrator.sectioning import (
        SHARD_ENGINE_CAP_SECONDS, SHARD_MERGE_MARGIN_SECONDS, WRITE_SHARD_MAX,
    )

    plan = _plan_of(Path(args.db), args.research)
    goal, _agent = _goal_and_agent(plan, args.goal, args.chapter) if args.chapter else (
        next(g for g in plan.goals if g.goal_id == args.goal), None)
    section_budget = goal.retry_policy.get("chapter_deadline_seconds")
    if section_budget is None:
        return Result.skip("③ 时间预算乘法", f"{args.goal} 的 retry_policy 里没有节墙钟")
    section_budget = float(section_budget)
    shards = int(args.shards or WRITE_SHARD_MAX)
    # 片墙钟取实际生效的那个：重放放宽过 Claude 那一路，整跑用适配器默认。
    per_shard_cap = float(args.shard_wallclock) if args.shard_wallclock else SHARD_ENGINE_CAP_SECONDS
    per_shard = min(per_shard_cap, section_budget)
    want = shards * per_shard + SHARD_MERGE_MARGIN_SECONDS
    numbers = {
        "节预算 s": section_budget, "片墙钟 s": per_shard, "预计片数": shards,
        "片×数+并轨余量 s": want, "差额 s": round(want - section_budget, 1),
        "SHARD_ENGINE_CAP_SECONDS": SHARD_ENGINE_CAP_SECONDS,
    }
    if want >= section_budget:
        return Result.red("③ 时间预算乘法",
                          f"片墙钟乘出来 {want:.0f}s ≥ 节预算 {section_budget:.0f}s，"
                          f"差 {want - section_budget:.0f}s——最后一片没时间写完，整节作废",
                          **numbers)
    return Result.ok("③ 时间预算乘法",
                     f"节预算 {section_budget:.0f}s 装得下 {shards} 片 × {per_shard:.0f}s", **numbers)


# --------------------------------------------------------------------------
# ④ 要拧的开关通不通电（出处：09-08 15:5x「换 standard 档跑这一节」不通电）
# --------------------------------------------------------------------------

def check_switch_energized(args: argparse.Namespace) -> Result:
    """拧完开关读**实际生效值**（plan_snapshot 里那份），不读配置文件里那份。

    节墙钟在建计划时由 `plan/generate.py` 烙进 `retry_policy`；**改档位改不到它**。
    跑的时候读的是 `plan_snapshot`（`replay/section.py:195`、`scheduler.py:775`）。
    """

    if not (args.db and args.research and args.goal and args.scale):
        return Result.skip("④ 开关通不通电", "缺 --db / --research / --goal / --scale")

    from app.config import _SCALE_DEFAULTS

    if args.scale not in _SCALE_DEFAULTS:
        return Result.red("④ 开关通不通电", f"没有这个档位：{args.scale}",
                          可选=sorted(_SCALE_DEFAULTS))
    wanted = _SCALE_DEFAULTS[args.scale].get("chapter_wall_clock_seconds")
    plan = _plan_of(Path(args.db), args.research)
    goal = next((g for g in plan.goals if g.goal_id == args.goal), None)
    if goal is None:
        return Result.red("④ 开关通不通电", f"plan_snapshot 里没有 {args.goal}")
    effective = goal.retry_policy.get("chapter_deadline_seconds")
    numbers = {"想拧成": f"{args.scale}={wanted}s", "库里实际生效": effective,
               "读自": "reports.plan_snapshot.goals[].retry_policy.chapter_deadline_seconds"}
    if effective is None or float(effective) != float(wanted):
        return Result.red("④ 开关通不通电",
                          f"不通电：档位说 {wanted}s，plan_snapshot 里跑的仍是 {effective}s"
                          "——节墙钟烙在计划快照里，换档改不到它", **numbers)
    return Result.ok("④ 开关通不通电", f"通电：{args.scale} 的 {wanted}s 已在 plan_snapshot 里", **numbers)


# --------------------------------------------------------------------------
# ⑤ 两处编号逐条对表（出处：D-055 —— 两处差 57 条、合并后 37 处越池）
# --------------------------------------------------------------------------

def check_citation_two_sites(args: argparse.Namespace) -> Result:
    """文件侧 `sources[].mark` 与库侧 `evidence.citation_no`：min/max **与逐条集合**都要一致。

    `polish/run.py` 里那把现成的尺子**只比 min/max**——两侧号段一样、中间差几条时它是绿的。
    本项目的红正是这种形状（差 57 条），所以这里加逐条对表，0 不一致才过。
    """

    if not (args.db and args.research and args.report_text):
        return Result.skip("⑤ 两处编号对表", "缺 --db / --research / --report-text")

    from app.report.render import parse_report

    data = parse_report(Path(args.report_text).read_text(encoding="utf-8"))
    doc = {int(str(item["mark"])[1:]) for item in (data.get("sources") or []) if item.get("mark")}
    rows = _store(Path(args.db)).list_evidence(args.research) or []
    db = {int(r["citation_no"]) for r in rows if r.get("citation_no") is not None}
    only_doc, only_db = sorted(doc - db), sorted(db - doc)
    same_span = (min(doc), max(doc)) == (min(db), max(db)) if doc and db else False
    numbers = {
        "文件侧条数": len(doc), "库侧条数": len(db),
        "文件有库没有": [f"S{n:02d}" for n in only_doc][:20],
        "库有文件没有": [f"S{n:02d}" for n in only_db][:20],
        "min/max 是否相同": same_span,
    }
    if only_doc or only_db:
        return Result.red("⑤ 两处编号对表",
                          f"逐条对不上：文件多 {len(only_doc)} 条、库多 {len(only_db)} 条"
                          + ("（**min/max 却相同——只比号段就会假绿**）" if same_span else ""),
                          **numbers)
    return Result.ok("⑤ 两处编号对表", f"两处各 {len(doc)} 条，逐条 0 不一致", **numbers)


# --------------------------------------------------------------------------
# ⑥ 适配器构造冒烟（出处：09-08 16:45 少传 codex，崩溃、零消耗、两分钟白等）
# --------------------------------------------------------------------------

def check_adapter_smoke(args: argparse.Namespace) -> Result:
    """把本轮要用的适配器**真构造一次**（不发请求），构造失败即判红。

    09-08 那次 `pytest 1738` 全绿仍崩——重放的 `adapter_factory` 一条用例都没有。
    这一行若在起跑脚本里，那次根本不会起跑。**零消耗：只 `__init__`。**
    """

    which = args.adapter or "replay"
    try:
        if which == "replay":
            from app.replay.section import _replay_adapter_factory
            adapter = _replay_adapter_factory(None)()
        elif which == "production":
            from app.adapters.routing import RoutedAdapter
            from app.adapters.claude import ClaudeAdapter
            from app.adapters.codex import CodexAdapter
            from datetime import datetime, timezone
            adapter = RoutedAdapter(
                utc_clock=lambda: datetime.now(timezone.utc), source_store=None,
                adapters={"claude": ClaudeAdapter(), "codex": CodexAdapter()})
        else:
            return Result.red("⑥ 适配器构造冒烟", f"不认识的 --adapter：{which}")
    except Exception as exc:  # noqa: BLE001 —— 构造失败正是要报的那件事
        return Result.red("⑥ 适配器构造冒烟",
                          f"构造就崩了，起跑必然零消耗白等：{type(exc).__name__}: {exc}",
                          adapter=which)
    engines = sorted(getattr(adapter, "_adapters", {}) or {})
    if not engines:
        return Result.red("⑥ 适配器构造冒烟", "构造出来了但一条引擎都没有", adapter=which)
    return Result.ok("⑥ 适配器构造冒烟", f"{which} 构造成功，引擎齐：{'、'.join(engines)}",
                     adapter=which, engines=engines)


# --------------------------------------------------------------------------
# ⑦ 点名范围 vs 工具实际会动的范围（出处：D-057 —— 静默重写没点名的节）
# --------------------------------------------------------------------------

def check_replay_scope(args: argparse.Namespace) -> Result:
    """播种后读 `chapter_progress`，`pending` 的就是会被重写的；与点名范围逐项比对。

    D-057：`rp1_replay.py section --section` **不往 `open_sandbox` 传**——沙盒库里
    本来就 pending 的行照样被节循环捡走。调度明令「sec-3 不许动」因此没能执行，
    起跑奏折还报了「没碰」。**别信参数，信库。**
    """

    if not (args.sandbox_db and args.research and args.goal):
        return Result.skip("⑦ 点名范围 vs 实际范围", "缺 --sandbox-db / --research / --goal")
    named = {s.rsplit("/", 1)[-1] for s in (args.section or [])}
    if not named:
        return Result.skip("⑦ 点名范围 vs 实际范围", "没点名（--section 空 = 整章重放），本项不适用")
    connection = sqlite3.connect(f"file:{args.sandbox_db}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT chapter_id, status FROM chapter_progress "
            "WHERE research_id = ? AND goal_id = ?", (args.research, args.goal),
        ).fetchall()
    finally:
        connection.close()
    pending = {str(cid).rsplit("/", 1)[-1] for cid, status in rows if status == "pending"}
    extra, missing = sorted(pending - named), sorted(named - pending)
    numbers = {"点名要重写": sorted(named), "库里 pending（真会被重写）": sorted(pending),
               "多出来会被静默重写的": extra, "点了名却没置 pending 的": missing,
               "该 goal 账本行数": len(rows)}
    if extra:
        return Result.red("⑦ 点名范围 vs 实际范围",
                          f"多出 {len(extra)} 节会被静默重写：{'、'.join(extra)}"
                          "——点名范围拦不住它，起跑前必须先把它们复位成终态", **numbers)
    if missing:
        return Result.red("⑦ 点名范围 vs 实际范围",
                          f"点了名却没置 pending：{'、'.join(missing)}——这一轮不会重写它们", **numbers)
    return Result.ok("⑦ 点名范围 vs 实际范围", f"点名 {len(named)} 节、库里 pending {len(pending)} 节，逐项相同", **numbers)


# --------------------------------------------------------------------------
# ⑧ 起跑后 30 秒查存活（出处：09-08 起跑 106 秒即崩，调度据此转述「还在跑」）
# --------------------------------------------------------------------------

def check_liveness(args: argparse.Namespace) -> Result:
    """起跑后 sleep N → `pgrep` **本体脚本名**，不在即判红。

    坑 18：macOS 的 `pgrep` 没有 `-E`（退出码 2），只用 `-f`。
    坑 20：进程命令行里**没有 worktree 名**（那是 cwd 不是参数），
    拿 worktree 名做正则只会命中 zsh 包装层，那层做完就退 → 立刻误判「长跑结束」。
    「起跑了 ≠ 跑成了」，报起跑必须先验存活。
    """

    if not args.process:
        return Result.skip("⑧ 起跑后查存活", "缺 --process（本体脚本名，不是 worktree 名）")
    if "Owli-" in args.process:
        return Result.red("⑧ 起跑后查存活",
                          f"--process 传的像 worktree 名（{args.process}）——进程命令行里没有它，"
                          "会命中 zsh 包装层并立刻误判「跑完了」。传本体脚本名。")
    wait = float(args.wait_seconds if args.wait_seconds is not None else 30)
    time.sleep(wait)
    done = subprocess.run(["pgrep", "-f", args.process], capture_output=True, text=True)
    pids = [line for line in done.stdout.split() if line.strip()]
    numbers = {"等了 s": wait, "pgrep 模式": args.process, "退出码": done.returncode, "pid": pids}
    if done.returncode != 0 or not pids:
        return Result.red("⑧ 起跑后查存活",
                          f"等 {wait:.0f}s 后进程不在了——起跑即死，别报「还在跑」", **numbers)
    return Result.ok("⑧ 起跑后查存活", f"等 {wait:.0f}s 后仍在：pid {'、'.join(pids)}", **numbers)


# --------------------------------------------------------------------------
# 注册表与命令行
# --------------------------------------------------------------------------

CHECKS: dict[str, tuple[str, Callable[[argparse.Namespace], Result]]] = {
    "backfill":  ("① 会不会回填（D-043）", check_backfill),
    "collision": ("② 未重写节撞号（D-056）", check_citation_collision),
    "budget":    ("③ 时间预算乘法（09-08 sec-2）", check_time_budget),
    "switch":    ("④ 开关通不通电（09-08 15:5x）", check_switch_energized),
    "citations": ("⑤ 两处编号逐条对表（D-055）", check_citation_two_sites),
    "adapter":   ("⑥ 适配器构造冒烟（09-08 16:45）", check_adapter_smoke),
    "scope":     ("⑦ 点名范围 vs 实际范围（D-057）", check_replay_scope),
    "liveness":  ("⑧ 起跑后 30 秒查存活（09-08 106 s）", check_liveness),
}

#: `all` 默认不含 ⑧——它要 sleep，且只在**起跑之后**才有意义。
_ALL_DEFAULT = [name for name in CHECKS if name != "liveness"]


def render(results: list[Result]) -> str:
    """一张人能读的表：哪项过、哪项红、红在哪个数上。"""

    width = max(len(r.check) for r in results)
    lines = ["", f"{'检查':<{width}}  结论  说明", "-" * (width + 60)]
    for r in results:
        lines.append(f"{r.check:<{width}}  {r.status:<4}  {r.headline}")
    for r in results:
        if r.numbers and r.status != PASS:
            lines.append("")
            lines.append(f"【{r.check}】量出来的数：")
            for key, value in r.numbers.items():
                lines.append(f"  {key} = {value}")
    counts = {s: sum(1 for r in results if r.status == s) for s in (PASS, FAIL, SKIP)}
    lines += ["", f"合计：PASS {counts[PASS]} · FAIL {counts[FAIL]} · SKIP {counts[SKIP]}"
                  "（**SKIP 不算过**——跳过条件写成 `passed and not skipped`）"]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="起跑前体检（零引擎）。新增检查加在这一份里，禁止另造。",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    parser.add_argument("check", choices=["all", "list", *CHECKS],
                        help="跑哪一项；all=除 ⑧ 外七项；list=列出八项")
    g = parser.add_argument_group("库与目标")
    g.add_argument("--db", help="要读的库（跑什么就读什么那个库）")
    g.add_argument("--sandbox-db", help="⑦ 用：播种之后的沙盒库")
    g.add_argument("--research"); g.add_argument("--goal"); g.add_argument("--chapter")
    g.add_argument("--section", action="append", default=[], help="点名要重写的节，可多次给")
    b = parser.add_argument_group("各项自己的入参")
    b.add_argument("--mode", choices=["fullrun", "section-replay", "polish", "rescore"],
                   help="① 用：只有 fullrun 走收尾、才会回填")
    b.add_argument("--budget-minutes", type=float, help="① 用：你给这轮报的分钟数")
    b.add_argument("--keep-section", action="append", default=[], help="② 用：本轮**不重写**的节产物路径")
    b.add_argument("--citation-numbers", help="② 用：本轮要发给重写节的 {url: 号} JSON")
    b.add_argument("--shards", type=int, help="③ 用：预计片数，默认 WRITE_SHARD_MAX")
    b.add_argument("--shard-wallclock", type=float, help="③ 用：实际生效的片墙钟，默认 300")
    b.add_argument("--scale", choices=["fast", "standard"], help="④ 用：你以为拧到了哪档")
    b.add_argument("--report-text", help="⑤ 用：工作稿文件路径")
    b.add_argument("--adapter", choices=["replay", "production"], help="⑥ 用，默认 replay")
    b.add_argument("--process", help="⑧ 用：本体脚本名（⛔ 不是 worktree 名）")
    b.add_argument("--wait-seconds", type=float, help="⑧ 用：等几秒，默认 30")
    parser.add_argument("--json", action="store_true", help="除人读的表外，再打一份 JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.check == "list":
        for name, (label, _) in CHECKS.items():
            print(f"  {name:<10} {label}")
        return 0
    names = _ALL_DEFAULT if args.check == "all" else [args.check]
    results = []
    for name in names:
        try:
            results.append(CHECKS[name][1](args))
        except Exception as exc:  # noqa: BLE001 —— 体检自己崩了也要进表，不许静默
            results.append(Result.red(CHECKS[name][0], f"体检这一项自己崩了：{type(exc).__name__}: {exc}"))
    print(render(results))
    if args.json:
        print(json.dumps([r.__dict__ for r in results], ensure_ascii=False, indent=2))
    if any(r.status == FAIL for r in results):
        return 1
    return 3 if any(r.status == SKIP for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
