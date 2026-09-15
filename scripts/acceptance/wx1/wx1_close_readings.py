"""§WX-1 重采关账读数（只读；库走 sqlite backup API 快照，不用 cp）。

    ../Owli/.venv/bin/python scripts/acceptance/wx1/wx1_close_readings.py r-20271e8a5028 [port=8980]

读数（每项标明量在哪一层）：
1 公众号：evidence 表 platform=wechat_mp 行数（入库层）/ 账号家数；被引 = 各 goal 交付物 claims.evidence 里的公众号 permalink（交付物层）+ evidence.citation_no 非空（收尾回填层）
2 X / HN / PH 产量：evidence 行数（入库层）
3 缺章清单：chapter_progress status∈{missing,failed}，按 reason；engine_error/事件含 socket 断连的单列「断连致缺」
4 耗时：approved_at → 研究终态事件时刻
5 费用：/cost llm 分引擎 + sources（标价折算）
6 路由：GET /researches/<id>/report、/api/researches/<id>/report、/api/researches/<id>/polished
主线章池单平台占比另跑 scripts/acceptance/secq1/three_tables.py（池组成层）。
"""
from __future__ import annotations

import json, os, sqlite3, sys, tempfile, urllib.error, urllib.request
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
RID = sys.argv[1]
PORT = sys.argv[2] if len(sys.argv) > 2 else "8980"
for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "all_proxy"):
    os.environ.pop(k, None)

snap = Path(tempfile.gettempdir()) / f"wx1-close-{os.getpid()}.db"
src = sqlite3.connect(f"file:{REPO / 'var' / 'wx1-serve.db'}?mode=ro", uri=True)
dst = sqlite3.connect(str(snap)); src.backup(dst); dst.close(); src.close()
con = sqlite3.connect(f"file:{snap}?mode=ro", uri=True)
out: list[str] = []

rep = con.execute("select status, plan_snapshot from reports where id=?", (RID,)).fetchone()
plan = json.loads(rep[1]) if rep and rep[1] else {}
out.append(f"研究 {RID} status={rep[0] if rep else '?'}")

# 1/2 evidence
by_plat = con.execute("select platform, count(*), sum(citation_no is not null) from evidence where report_id=? group by platform", (RID,)).fetchall()
out.append("入库层 evidence（平台, 行数, citation_no 非空）=" + str(by_plat))
wx_accounts = Counter(r[0] for r in con.execute("select author_name from evidence where report_id=? and platform='wechat_mp'", (RID,)))
out.append(f"公众号账号家数={len(wx_accounts)} 头号={wx_accounts.most_common(1)}")
wx_links = {r[0] for r in con.execute("select permalink from evidence where report_id=? and platform='wechat_mp'", (RID,))}
cited_by_goal = {}
all_cited = Counter()
for goal in plan.get("goals") or []:
    dp = str((goal.get("deliverable") or {}).get("path") or "")
    path = REPO / "var" / "runs" / RID / (dp if dp.startswith("goals/") else f"goals/{goal['goal_id']}/{dp}")
    if not path.exists():
        cited_by_goal[goal["goal_id"]] = "交付物缺"
        continue
    doc = json.loads(path.read_text("utf-8"))
    links = {e.get("permalink") for c in doc.get("claims") or [] for e in c.get("evidence") or [] if isinstance(e, dict)}
    wx = links & wx_links
    all_cited.update(wx)
    plat = Counter("wechat_mp" if l in wx_links else (l or "").split("/")[2] if l and "//" in l else "?" for l in links)
    cited_by_goal[goal["goal_id"]] = {"claims": len(doc.get("claims") or []), "去重引用链接": len(links), "公众号被引": len(wx)}
out.append(f"交付物层 各 goal 引用={cited_by_goal}")
out.append(f"公众号被引（交付物层去重）={len(all_cited)} / 入库 {len(wx_links)}")
for plat in ("x", "hacker_news", "product_hunt"):
    n = con.execute("select count(*) from evidence where report_id=? and platform=?", (RID, plat)).fetchone()[0]
    out.append(f"{plat} 入库行数={n}")

# 3 missing chapters
rows = con.execute("select goal_id, chapter_id, status, attempts, reason, engine_error from chapter_progress where research_id=? and status in ('missing','failed') and chapter_id not like '%/%' order by goal_id, chapter_id", (RID,)).fetchall()
events = [r[0] for r in con.execute("select payload from events where research_id=?", (RID,))]
disconnect_agents = set()
for e in events:
    if "socket connection was closed unexpectedly" not in e:
        continue
    try:
        data = json.loads(e).get("data") or {}
    except json.JSONDecodeError:
        continue
    if data.get("agent_id"):
        disconnect_agents.add(str(data["agent_id"]))
plan_agents = {(g["goal_id"], (a.get("chapter") or {}).get("chapter_id")): a["agent_id"] for g in plan.get("goals") or [] for a in g.get("agents") or []}
out.append(f"缺章 {len(rows)}：")
for goal_id, ch, st, att, reason, err in rows:
    agent = plan_agents.get((goal_id, ch), "?")
    disconnect = "socket connection was closed" in str(err or "") or agent in disconnect_agents
    out.append(f"  {goal_id}/{ch} {agent} {st} reason={reason} attempts={att}{' ⚡断连致缺' if disconnect else ''}")

# 4 duration
term = None
for e in events:
    d = json.loads(e)
    if d.get("type") == "research_update" and (d.get("data") or {}).get("status") in ("completed", "failed", "stopped"):
        term = (d["data"]["status"], d)
approved = plan.get("approved_at")
out.append(f"approved_at={approved} 终态事件={term[0] if term else '无'}")
end_row = con.execute("select max(created_at) from events where research_id=?", (RID,)).fetchone()[0]
out.append(f"最后事件时刻={end_row}")

# 5 cost + 6 routes
def get(path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=30) as r:
            return r.status, r.headers.get("content-type"), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("content-type"), b""
code, _, body = get(f"/api/researches/{RID}/cost")
cost = json.loads(body); cost = cost.get("data", cost)
llm = cost.get("llm") or {}
out.append("模型费（标价折算）=" + json.dumps({"cost_usd": llm.get("cost_usd"), "calls": llm.get("calls"),
    "by_engine": {k: {"calls": v.get("calls"), "cost_usd": v.get("cost_usd"), "estimated_cost_usd": v.get("estimated_cost_usd"),
                      "input": v.get("input_tokens"), "cached": v.get("cached_input_tokens"), "output": v.get("output_tokens")}
                  for k, v in (llm.get("by_engine") or {}).items()}}, ensure_ascii=False))
srcs = cost.get("sources") or {}
out.append("源费（标价折算）=" + json.dumps({"total": srcs.get("total_cost_usd"), "calls": srcs.get("calls"),
    "rows": [(r.get("platform"), r.get("provider"), r.get("calls"), r.get("cost_usd")) for r in srcs.get("rows") or []]}, ensure_ascii=False))
for path in (f"/researches/{RID}/report", f"/api/researches/{RID}/report", f"/api/researches/{RID}/polished"):
    code, ctype, body = get(path)
    out.append(f"GET {path} → {code} {ctype} {len(body)}B")
print("\n".join(out))
snap.unlink(missing_ok=True)
