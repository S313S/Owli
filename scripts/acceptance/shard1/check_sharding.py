#!/usr/bin/env python3
"""§SHARD-1 零成本验收：分片的读数，全程零引擎调用、零采集、零评级。

判据不落在「跑没跑起来」上，落在**真稿**上：
① 真底料的执行摘要能读出几条发现、各带哪些角标（这就是真机那一格会切几片）；
② 09-06 那份 8 432 B 的「关键发现」切成 4 片再合，逐字节相同、角标集合不变；
③ 旧片就算漏清也拼不进来（两道闸各自独立）；
④ 3 条的摘要出 3 片、5 条出 5 片，都不产生空片；
⑤ 本轮动过的文件对禁区零命中。

    ../Owli/.venv/bin/python3 scripts/acceptance/shard1/check_sharding.py
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from app.report.polish.run import findings_for, offpool_marks          # noqa: E402
from app.report.polish.sharding import merge_shards, parse_findings, shard_paths  # noqa: E402
from app.report.polish.skills import get_template                      # noqa: E402

#: 提货单 §九 写死的禁区。本包一个都不许碰。
FORBIDDEN = ("app/orchestrator/sectioning.py", "app/store/", "app/adapters/",
             "app/replay/", "app/config.py", "source_mcp", "validation", "crossref",
             "ratelimit")
BASE = "70f38a0"
FIXTURES = ROOT / "tests" / "fixtures" / "rpt1"
rows: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    rows.append((name, ok, detail))


def real_summaries() -> None:
    """真底料读数：真机那一格实际会切几片，现在就能量出来。"""
    skill = get_template("consulting")
    for run_dir in sorted((ROOT / "var" / "runs").glob("r-*")):
        summary = run_dir / "goals" / "polished" / "consulting-parts" / "01-执行摘要.md"
        if not summary.is_file():
            continue
        findings = parse_findings(summary.read_text(encoding="utf-8"))
        marks = "；".join(f"第{f.index}条 {'/'.join(f.marks) or '无角标'}" for f in findings)
        check(f"① {run_dir.name} 摘要 → 片数", 3 <= len(findings) <= 5,
              f"{len(findings)} 片（SKILL 规定 3–5 条）｜{marks}")
    parts = [(n, FIXTURES / "01-执行摘要.md") for n in skill.sections]
    check("① 大纲节自己不切", findings_for(skill, "执行摘要", parts) == [],
          "第一节要先整节写成，片数从它里面读")


def roundtrip() -> None:
    """真稿往返：判据是**逐字节**，不是「看着差不多」。"""
    original = (FIXTURES / "02-关键发现.md").read_text(encoding="utf-8").strip()
    chunks = [c for c in re.split(r"(?m)^(?=## )", original) if c.strip()]
    with tempfile.TemporaryDirectory() as tmp:
        section = Path(tmp) / "02-关键发现.md"
        paths = shard_paths(section, len(chunks))
        for path, chunk in zip(paths, chunks):
            path.write_text(chunk.strip() + "\n", encoding="utf-8")
        merged = merge_shards(paths)
        check("② 真稿切 4 片再合｜逐字节相同", merged == original,
              f"{len(original.encode())} B → {len(chunks)} 片 → {len(merged.encode())} B")
        marks = lambda t: sorted(set(re.findall(r"\[S\d{2,}\]", t)))
        check("② 角标集合不变", marks(merged) == marks(original),
              f"{len(marks(original))} 个角标：{'/'.join(m.strip('[]') for m in marks(original))}")
        pool = frozenset(range(1, 100))
        check("② 越池角标读数不变", offpool_marks(merged, pool) == offpool_marks(original, pool))

        (Path(tmp) / "02-关键发现.shard-9.md").write_text("旧片[S99]。", encoding="utf-8")
        check("③ 旧片不被误拼", "S99" not in merge_shards(paths),
              "合并按片序取清单，不 glob 目录——与「开跑前清一网」是两道独立的闸")


def shard_counts() -> None:
    for count in (3, 5):
        body = "背景。\n\n关键发现：\n\n" + "".join(
            f"{i}. 【B】第 {i} 条结论[S0{i}]。\n" for i in range(1, count + 1))
        found = parse_findings(body)
        ok = len(found) == count and all(f.text.strip() and f.marks for f in found)
        check(f"④ {count} 条的摘要 → {count} 片，无空片", ok, f"实得 {len(found)} 片")


def blast_radius() -> None:
    diff = subprocess.run(["git", "diff", "--name-only", f"{BASE}..HEAD"],
                          cwd=ROOT, capture_output=True, text=True).stdout.split()
    hits = [f for f in diff for k in FORBIDDEN if k in f]
    check("⑤ 禁区零命中", not hits, f"动了 {len(diff)} 个文件：{'、'.join(diff)}"
          if not hits else f"命中 {hits}")


def main() -> int:
    real_summaries()
    roundtrip()
    shard_counts()
    blast_radius()
    width = max(len(name) for name, _, _ in rows)
    for name, ok, detail in rows:
        print(f"{'PASS' if ok else 'FAIL'}  {name.ljust(width)}  {detail}")
    bad = [name for name, ok, _ in rows if not ok]
    print(f"\n零成本验收：{len(rows) - len(bad)}/{len(rows)} 过" + (f"；未过 {bad}" if bad else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
