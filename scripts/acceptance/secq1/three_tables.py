#!/usr/bin/env python3
"""QUOTA-1 三表重量器（用法：three_tables.py <源库> <research_id>）。

⛔ 源库只读：先用 sqlite backup API 拷副本再量（不用 cp——会漏 WAL）。
读数层次一律标清：表一/表二在**池组成层**（D 闸之后、99 封顶之后、写手之前），
表三在**语料层**（候选集，还没进池）。下游还有一道闸：写手从池里挑几条引，
所以「池里有 N 条」≠「引了 N 条」。
"""
import json, sqlite3, sys, tempfile, os
from pathlib import Path

SRC, RID = sys.argv[1], sys.argv[2]
REPO = Path(__file__).resolve()
# 相对本仓根（scripts/acceptance/secq1/ 上三级），⛔ 不再写死某个 worktree 的绝对路径。
sys.path.insert(0, str(REPO.parents[3]))
from app.orchestrator.sectioning import _evidence_index, _evidence_grade
from app.reliability.relevance import rows_naming_entities
from app.plan.model import Plan, agent_kind_of

# ── 0. backup API 拷副本 ─────────────────────────────────────────
snap = Path(tempfile.gettempdir()) / f"q1-snap-{os.getpid()}.db"
s = sqlite3.connect(f"file:{Path(SRC).resolve()}?mode=ro", uri=True)
d = sqlite3.connect(str(snap)); s.backup(d); d.close(); s.close()
print(f"副本（backup API，非 cp）：{snap}  {snap.stat().st_size/1e6:.1f} MB\n")

con = sqlite3.connect(f"file:{snap}?mode=ro", uri=True); con.row_factory = sqlite3.Row
row = con.execute("SELECT research_question,status,plan_snapshot FROM reports WHERE id=?", (RID,)).fetchone()
if row is None:
    sys.exit(f"库里没有 {RID}")
plan = Plan.from_dict(json.loads(row["plan_snapshot"]))
print(f"研究 {RID}  status={row['status']}  scale={plan.scale}")
print(f"题面：{row['research_question']}\n")

rows = []
for r in con.execute("SELECT * FROM evidence WHERE report_id=? ORDER BY id", (RID,)):
    it = dict(r)
    for f in ("author_meta", "raw_metrics", "norm_context", "extra"):
        if it.get(f) is not None:
            it[f] = json.loads(it[f])
    rows.append(it)
gated = rows_naming_entities(rows, plan) or rows
print(f"证据行 {len(rows)} → POOL-1 相关性闸后 {len(gated)}")

UGC = {"xhs","douyin","weibo","reddit","bilibili","zhihu","x","hacker_news","product_hunt","wechat_mp"}

# ── 表三（先给，它是上游）· 语料层非 D 候选 ─────────────────────
print("\n===== 表三 · 语料层：每个 goal 名下非 D 候选按平台（还没进池）=====")
for g in plan.goals:
    c = {}
    for r in gated:
        if str(r.get("goal_id")) != g.goal_id or _evidence_grade(r) == "D":
            continue
        c[str(r.get("platform"))] = c.get(str(r.get("platform")), 0) + 1
    u = sum(v for k, v in c.items() if k in UGC); o = sum(v for k, v in c.items() if k not in UGC)
    print(f"  {g.goal_id}  {g.title[:28]:<30} {c or '（零行）'}  → UGC {u} / 非UGC {o}")

# ── 表一、表二 · 池组成层 ───────────────────────────────────────
kinds = sorted({agent_kind_of(a.agent_id, a.capability.get("profile"))
                for g in plan.goals for a in g.agents} & {"report_writing", "cross_validation"})
allowed = {g.goal_id for g in plan.goals}
print(f"\n===== 表一/表二 · 池组成层（allowed=全 goal；章类型 {kinds}）=====")
for g in plan.goals:
    for kind in kinds or [None]:
        pool, _ = _evidence_index(gated, set(allowed), section_goal_id=g.goal_id, chapter_kind=kind)
        items = pool["items"]
        plat = {}
        for i in items:
            plat[str(i.get("platform"))] = plat.get(str(i.get("platform")), 0) + 1
        own = sum(1 for i in items if str(i.get("goal_id")) == g.goal_id)
        ugc = sum(v for k, v in plat.items() if k in UGC)
        print(f"  节讲 {g.goal_id} / {kind or '旧口径':<17} 池{len(items):<3}本goal{own:<3}跨{len(items)-own:<3}"
              f" UGC {ugc}/{len(items):<3} {plat}")
print("\n⚠️ 读数层次：表一/表二在池组成层（D 闸后、99 封顶后、写手前）；"
      "表三在语料层。下游还有写手挑引那一道闸，池里有 N 条 ≠ 引了 N 条。")
os.unlink(snap)
