#!/usr/bin/env python3
"""§OBS-6 判据：真机跑一次 polish，打回原因必须**落进库**（events）而不只是进 prompt。

    python3 scripts/acceptance/obs6/obs6_reject_log.py \
        --db var/obs6-sandbox.db --runs var/obs6-runs --id r-3e04f808dffd

**零引擎**：适配器是桩，不连模型——这一包验的是「打回留不留痕」，
不是「写手写得好不好」，付一次引擎一个字都不会多告诉我们。
桩故意让第二节引一个池外角标，把 offpool 那道闸踩响；第一节照常写成，
好让「行数 == attempts 减成功次数」这条等式量出**非平凡**读数
（两边都是 0 的等式谁都能过，那是假绿）。

⛔ 库与 runs 都必须指沙盒副本：8969 服的是 `../Owli-rpt1/var/shard1-serve.db`
与它的 `var/runs`，往那儿写就是当着用户的面改他正在读的稿。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.report.polish.run import REJECT_EVENT_TYPE, polish, rejects_path  # noqa: E402
from app.store.dao import Store  # noqa: E402

#: 池外角标。真池是 S01~S<N>，S99 铁定不在里面。
OFFPOOL_MARK = "[S99]"


class _StubAdapter:
    """第一节写干净的，其余节各引一个池外角标。不连引擎，落盘即返回。"""

    def __init__(self) -> None:
        self.calls = 0

    async def run(self, task, ctx, on_event=None):
        self.calls += 1
        clean = self.calls == 1
        body = "这一节的正文。" * 60 + ("" if clean else f"\n\n越池的这一处 {OFFPOOL_MARK}。\n")
        task.output_path.write_text(body, encoding="utf-8")
        return type("R", (), {"succeeded": True, "engine_error": None})()


def work_draft_path(runs_root: Path, research_id: str, report_path: str) -> Path:
    """库里的 report_path 可能是别的 worktree 的路径，按本次 runs_root 重新拼。"""
    raw = Path(report_path)
    return runs_root / research_id / "goals" / raw.parent.name / raw.name


def _count_rejects(database: Path, research_id: str) -> list[dict]:
    conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "select sequence, type, payload, created_at from events "
        "where research_id=? and type=? order by sequence",
        (research_id, REJECT_EVENT_TYPE)).fetchall()
    conn.close()
    return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--runs", required=True)
    parser.add_argument("--id", required=True)
    parser.add_argument("--template", default="consulting")
    args = parser.parse_args()
    database = Path(args.db).resolve()
    runs_root = Path(args.runs).resolve()
    store = Store(database)
    report = store.get_report(args.id)
    if report is None:
        raise SystemExit(f"× 库里没有 {args.id}")

    before = len(_count_rejects(database, args.id))
    path = rejects_path(runs_root, args.id, args.template)
    path.unlink(missing_ok=True)

    text = work_draft_path(runs_root, args.id, report["report_path"]).read_text("utf-8")
    adapter = _StubAdapter()
    outcome = await polish(store, args.id, runs_root, text,
                           template=args.template, adapter=adapter)

    events = _count_rejects(database, args.id)[before:]
    lines = [json.loads(line) for line in
             (path.read_text("utf-8").splitlines() if path.is_file() else []) if line]
    successes = adapter.calls - len(lines)
    checks = [
        ("① events 里出现 ≥1 条 polish_reject", len(events) >= 1, len(events)),
        ("② rejects.jsonl 行数 == attempts 减成功次数",
         len(lines) == outcome["attempts"] - successes, f"{len(lines)} vs "
         f"{outcome['attempts']}-{successes}"),
        ("③ 读数非平凡（确有一节写成、确有打回）",
         successes >= 1 and len(lines) >= 1, f"成功 {successes} / 打回 {len(lines)}"),
        ("④ 事件 payload 与 jsonl 逐条同内容",
         [e["payload"]["errors"] for e in events] == [r["errors"] for r in lines], ""),
        ("⑤ 每行说得出节 / 闸 / 第几次尝试",
         all(r.get("section") and r.get("gate") and r.get("attempt") for r in lines),
         {r["gate"] for r in lines}),
    ]
    for label, ok, detail in checks:
        print(f"{'✓' if ok else '×'} {label}" + (f"    {detail}" if detail != "" else ""))
    print("\n— polish 返回 —")
    print(json.dumps({k: outcome[k] for k in ("status", "attempts", "failed_section")
                      if k in outcome}, ensure_ascii=False))
    print("\n— 打回台账首行 —")
    print(json.dumps(lines[0], ensure_ascii=False, indent=1) if lines else "（空）")
    return 0 if all(ok for _, ok, _ in checks) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
