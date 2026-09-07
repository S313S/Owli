#!/usr/bin/env python3
"""§RPT-1 货 6：三份成稿 × 三模板 = 九次整理，跑完逐份上尺子出读数表。

    python3 scripts/acceptance/rpt1/rpt1_matrix.py --db var/rpt1-8956.db --runs var/runs

串行跑（每次一个 Opus 调用，几分钟起步）。三种跑法：

- 默认：**md 文件在就跳过**（只压尺子不写作，零引擎成本）。注意跳过的判据是
  「文件存在」不是「过了尺子」——所以它只适合复验尺子，不能拿来续跑。
- `--force`：九格全部重写。整轮从头跑用这个。
- `--resume`：按账本续跑——本轮已经写出来**且过了尺子**的格跳过，其余重写。
  账本记的是「哪个 git HEAD 下哪一格过了」，代码一变账本自动作废，
  不会拿旧代码写的稿冒充本轮成果。九格串行 5–6 h，中途机器重启用它接着跑。

撞到缺陷时重跑单格用 `--only <id>:<模板>`。
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.report.polish.run import artifact_paths, polish  # noqa: E402
from app.report.polish.skills import load_templates  # noqa: E402

_spec = importlib.util.spec_from_file_location("check_polished", Path(__file__).with_name("check_polished.py"))
check_polished = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_polished)

REPORTS = ("r-b10812f664d2", "r-3e04f808dffd", "r-045acebc352b")
#: 续跑账本默认落 var/ 而不是 /tmp——09-07 早上 /tmp 被重启清空，
#: 九格日志与三个哨兵探测器日志一起没了，读数只剩人工抄下来的那份。
DEFAULT_PROGRESS = "var/rpt1-matrix-progress.json"


def _code_revision() -> str:
    """当前代码版本。账本靠它作废：改了尺子或提示词，上一轮的绿一律不认。"""
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(ROOT),
                             capture_output=True, text=True, timeout=10)
        head = out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        head = ""
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=str(ROOT),
                           capture_output=True, text=True).stdout.strip()
    # 工作区脏就永不复用账本：改了没提交的那几行，恰恰最可能是这轮要验的东西。
    return f"{head}{'+dirty' if dirty else ''}" or "unknown"


def _load_progress(path: Path, revision: str) -> dict[str, dict]:
    """读账本。代码版本对不上就当没有——宁可多跑，不可拿旧码的绿冒充本轮。"""
    if not path.is_file():
        return {}
    try:
        book = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if book.get("revision") != revision:
        print(f"账本是 {book.get('revision')!r} 写的，当前是 {revision!r}，整本作废", flush=True)
        return {}
    return {k: v for k, v in (book.get("cells") or {}).items() if isinstance(v, dict)}


def _save_progress(path: Path, revision: str, cells: dict[str, dict]) -> None:
    """每跑完一格就落一次，先写临时文件再改名——跑到一半被杀不会留半个账本。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"revision": revision, "cells": cells},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def _load_runner():
    spec = importlib.util.spec_from_file_location(
        "rpt1_polish", Path(__file__).with_name("rpt1_polish.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def one(runner, store, runs_root: Path, research_id: str, template: str,
              force: bool) -> dict:
    """跑一格并上尺子。返回这一格的读数行。"""
    md_path, tables_path = artifact_paths(runs_root, research_id, template)
    report = store.get_report(research_id)
    work_path = runner.work_draft_path(runs_root, research_id, report["report_path"])
    text = work_path.read_text(encoding="utf-8")
    started = time.time()
    if force or not md_path.is_file():
        outcome = await polish(store, research_id, runs_root, text, template=template)
    else:
        outcome = {"status": "ok", "attempts": 0, "offpool": [], "skipped": True}
    row = {"research_id": research_id, "template": template, "status": outcome["status"],
           "attempts": outcome.get("attempts"), "seconds": round(time.time() - started, 1),
           "bytes": md_path.stat().st_size if md_path.is_file() else 0,
           # 这一格到底重写没重写，要跟着读数走——否则账本里「过了」的格分不出
           # 是本轮写出来的，还是上一轮留下的稿被重新压了一遍尺子。
           "skipped": bool(outcome.get("skipped"))}
    if outcome["status"] != "ok":
        row["ruler"] = {"—": ["未出稿：" + "；".join(outcome.get("errors") or ["未知"])]}
        return row
    findings = check_polished.run(md_path, tables_path, work_path)
    row["ruler"] = {name: problems for name, problems in findings.items() if problems}
    row["passed"] = not row["ruler"]
    return row


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--runs", required=True)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--resume", action="store_true",
                        help="按账本续跑：本轮已过尺子的格跳过，其余重写")
    parser.add_argument("--progress", default=DEFAULT_PROGRESS, help="续跑账本路径")
    parser.add_argument("--only", default=None, help="<research_id>:<模板>，只跑这一格")
    args = parser.parse_args()
    if args.resume and args.force:
        parser.error("--resume 与 --force 互斥：一个是接着跑，一个是从头跑")
    runs_root = Path(args.runs).resolve()
    runner = _load_runner()
    store = runner.ReadOnlyStore(Path(args.db).resolve())
    templates = [t.name for t in load_templates()]
    cells = [(rid, tpl) for rid in REPORTS for tpl in templates]
    if args.only:
        want_id, _, want_tpl = args.only.partition(":")
        cells = [c for c in cells if c == (want_id, want_tpl)]
    revision = _code_revision()
    progress_path = Path(args.progress)
    if not progress_path.is_absolute():
        progress_path = ROOT / progress_path
    # 账本**总是**先读进来再往上加，不管这轮带不带 --resume——否则 `--only 某一格`
    # 会把整本覆盖成只剩那一格，下次 --resume 就把本来已过的八格又跑一遍。
    # （版本对不上时 `_load_progress` 自己返回空，所以这样读不会串轮。）
    book = _load_progress(progress_path, revision)
    skippable = book if args.resume else {}
    if args.resume:
        # 这里必须跟下面的跳过条件用同一个式子，否则会报「6 格可跳过」而实际跳 0 格。
        done = sum(1 for v in book.values() if v.get("passed") and not v.get("skipped"))
        print(f"续跑账本 {progress_path}（代码 {revision}）："
              f"账本 {len(book)} 格、其中已过 {done} 格可跳过", flush=True)
    rows = []
    for research_id, template in cells:
        key = f"{research_id}:{template}"
        prior = skippable.get(key) or {}
        # 跳过的条件是「**本轮真写出来过**、并且过了尺子」——只看 passed 不够：
        # 默认路径（零成本复验尺子）也会给旧稿判 PASS 并记进账本，`skipped=True`。
        # 只认 passed 的话，最后一轮会把那几格直接跳掉，交出没有质量补丁的旧稿，
        # 读数还是 9/9 全绿。这一条同时堵住「md 是旧的、tables.json 是新的」那格：
        # 旧稿永远不被当成本轮成果。
        if prior.get("passed") and not prior.get("skipped"):
            row = dict(prior)
            row["resumed"] = True
            rows.append(row)
            print(f"[SKIP] {key}  本轮已写出且过尺子，不重跑", flush=True)
            continue
        # 续跑时没过的格一律重写：留着上一段那份没过的稿只会被默认路径跳过。
        row = await one(runner, store, runs_root, research_id, template,
                        args.force or args.resume)
        rows.append(row)
        book[key] = row
        _save_progress(progress_path, revision, book)
        mark = "PASS" if row.get("passed") else "FAIL"
        # 没重写就把话说明白：这一格量的是上一轮留下的稿，不是本轮的成果。
        if row.get("status") != "ok":
            stale = "  ← 本格未出稿（bytes 是上一轮留下的文件）"
        elif row.get("skipped"):
            stale = "  ← 未重写，只压尺子（量的是上一轮的稿）"
        else:
            stale = ""
        print(f"[{mark}] {research_id} × {template}  {row['bytes']} B  "
              f"{row['seconds']}s  attempts={row['attempts']}{stale}", flush=True)
        for name, problems in (row.get("ruler") or {}).items():
            print(f"        {name}: {len(problems)} 处 · {problems[0][:70]}", flush=True)
    print("\n" + json.dumps(rows, ensure_ascii=False, indent=1))
    print(f"\n读数账本：{progress_path}", flush=True)
    green = sum(1 for r in rows if r.get("passed"))
    print(f"\n尺子全过 {green}/{len(rows)}")
    return 0 if green == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
