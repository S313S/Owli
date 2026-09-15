"""§WX-1 重采放行前核形态读数（留服 8980 上的真研究，不是一次性小跑库）。

    ../Owli/.venv/bin/python scripts/acceptance/wx1/wx1_plan_form.py <research_id> [port=8980]

读：GET /api/researches/<id>/plan（落盘 var/wx1-form/<id>.plan.json）+ 留服库 events（按 research_id）+ plan-segments。
报：goal 章数与卡、主角判定、0 章 goal、空心 goal 验收条原文、chapter_type_correction、chapter_lint_not_converged、
⑦ 独立尺子（子进程，按 research_id 过滤事件）。只读，不判放行——放行由调度核。
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
RID = sys.argv[1]
PORT = sys.argv[2] if len(sys.argv) > 2 else "8980"
DB = REPO / "var" / "wx1-serve.db"
SEG = REPO / "var" / "runs" / RID / "plan-segments"
OUTDIR = REPO / "var" / "wx1-form"
OUTDIR.mkdir(parents=True, exist_ok=True)

for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "all_proxy"):
    os.environ.pop(key, None)
with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/researches/{RID}/plan", timeout=20) as resp:
    body = json.load(resp)
plan = body.get("data") or {}
plan_path = OUTDIR / f"{RID}.plan.json"
plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")

from app.report.polish.tables import subject_canonicals  # noqa: E402  主角判定读数（D-069 生产口径）

goals = plan.get("goals") or []
lines: list[str] = []
protagonists = subject_canonicals({
    "research_question": plan.get("research_question") or plan.get("query") or "",
    "title": "", "entities": plan.get("entities") or [], "subjects": plan.get("subjects") or [],
})
allocation = json.loads((SEG / "allocation.json").read_text("utf-8")) if (SEG / "allocation.json").exists() else {}
skeleton = json.loads((SEG / "skeleton.json").read_text("utf-8")) if (SEG / "skeleton.json").exists() else {}
lines.append(f"research_id={RID} 库={DB}")
lines.append(f"题面={plan.get('research_question') or skeleton.get('query') or ''}")
lines.append(f"subjects={skeleton.get('subjects')} 主角判定(subject_canonicals)={protagonists}")
alloc_view = {g: [str(s["source_id"]) + "·" + str(s["entity"]) for s in v] for g, v in allocation.items()}
lines.append(f"分配表={alloc_view}")
total = 0
corrections = []
for goal in goals:
    agents = goal.get("agents") or []
    total += len(agents)
    cards = [f"{(a.get('capability') or {}).get('sources', [''])[0]}·{a.get('entity')}"
             for a in agents if (a.get("capability") or {}).get("sources")]
    lines.append(f"{goal['goal_id']}「{goal.get('title')}」depends={goal.get('depends_on')} 章 {len(agents)} 卡 {cards}")
    if not cards:
        lines.append(f"  空心 goal 验收条原文：")
        for i, item in enumerate(goal.get("acceptance") or []):
            lines.append(f"    [{i}] {item}")
    for a in agents:
        note = (((a.get("chapter") or {}).get("closing") or {}).get("notes") or {}).get("chapter_type_correction")
        if note:
            corrections.append(f"{goal['goal_id']}/{a.get('agent_id')}: {json.dumps(note, ensure_ascii=False)}")
empty = [g["goal_id"] for g in goals if not g.get("agents")]
lines.append(f"合计 goal {len(goals)} 章 {total}｜0 章 goal={empty or '无'}")
lines.append(f"chapter_type_correction={corrections or '无'}")
conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
texts = [r[0] for r in conn.execute("select payload from events where research_id=? order by sequence", (RID,))]
conn.close()
lines.append(f"chapter_lint_not_converged 出现次数={sum('chapter_lint_not_converged' in t for t in texts)}｜事件 {len(texts)} 条")
stale = subprocess.run([sys.executable, str(Path(__file__).with_name("wx1_stale_acceptance.py")),
                        str(plan_path), str(DB), RID], capture_output=True, text=True)
try:
    hits = json.loads(stale.stdout).get("hits", [])
except json.JSONDecodeError:
    hits = [f"尺子崩：{stale.stderr[-300:]}"]
lines.append(f"⑦ 独立尺子（空心 goal 要本 goal 采集产物的验收条）命中={hits or 0}")
print("\n".join(lines))
