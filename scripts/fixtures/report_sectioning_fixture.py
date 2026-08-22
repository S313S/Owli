"""报告模块夹具（M3-h-fix-2 交接）：假采集数据 + 手工账本 → 真实 Claude 按节写报告。
用法：cd Owli-m3h && ../Owli/.venv/bin/python scripts/fixtures/report_sectioning_fixture.py <输出目录>
已知坑：capability.profile 必须在闭集内；event_buffer.publish 必须是 async。
原：单模块实测：假采集数据 + 账本 → 真实 Claude 按节写报告（fast 档节化）。"""
import asyncio, json, sqlite3, sys, time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, ".")
from tests.plan_factory import make_plan_dict
from app.store.dao import Store
from app.plan.model import Plan
from app.orchestrator.runtime import RuntimeCoordinator

ROOT = Path(sys.argv[1]); RID = "r-modtest"
ROOT.mkdir(parents=True, exist_ok=True)
db = ROOT / "owli.db"
schema = Path("app/store/schema.sql")
with sqlite3.connect(db) as c:
    c.executescript(schema.read_text(encoding="utf-8"))
store = Store(db)
store.create_report(id=RID, title="豆包语音输入法的竞品分析", research_question="豆包语音输入法的竞品分析", created_at="2026-08-22T08:00:00Z")

src = make_plan_dict()
src["research_id"] = RID; src["scale"] = "fast"; src["baseline"] = None
src["title"] = "豆包语音输入法的竞品分析"; src["research_question"] = "豆包语音输入法的竞品分析"
g1, g2, g3 = src["goals"]
def chap(cid, ctype, path, entities, inputs=()):
    return {"chapter_id": cid, "chapter_type": ctype, "plan_path": f"x/{cid}.md",
            "opening": {"inputs": [{"path": p} for p in inputs], "task": "见 task", "acceptance": ["完成"]},
            "closing": {"output": {"path": path}, "entities": entities, "expected_count": None, "notes": {}}}
# goal-1：两章采集（一章 done 有假数据，一章 missing 工具不可用）
g1["title"] = "竞品基准信息采集"; g1["objective"] = "采集讯飞/搜狗/百度输入法的语音输入功能与用户反馈"
a, = g1["agents"]; a["agent_id"] = "data-collection"; a["display_name"] = "数据采集·web_search"
a["task"] = "采集三家竞品语音输入相关页面与评测"; a["output"] = {"format": "json", "path": "goals/goal-1/data-collection.json", "validators": ["file_exists"]}
a["chapter"] = chap("ch-1", "collection", a["output"]["path"], ["讯飞输入法", "搜狗输入法", "百度输入法"])
b = json.loads(json.dumps(a)); b["agent_id"] = "data-collection-2"; b["display_name"] = "数据采集·x"
b["output"]["path"] = "goals/goal-1/data-collection-2.json"; b["chapter"] = chap("ch-2", "collection", b["output"]["path"], ["讯飞输入法"])
g1["agents"] = [a, b]
# goal-2：交叉验证 done
g2["title"] = "竞品六维对标"; g2["objective"] = "按准确率/方言/大模型/隐私/变现/生态六维对标"
c, = g2["agents"]; c["agent_id"] = "cross-validation"; c["display_name"] = "交叉验证"
c["task"] = "合并采集产物做六维对标矩阵"; c["output"] = {"format": "json", "path": "goals/goal-2/matrix.json", "validators": ["file_exists"]}
c["chapter"] = chap("ch-1", "cross_validation", c["output"]["path"], ["讯飞输入法", "搜狗输入法", "百度输入法"], [a["output"]["path"], b["output"]["path"]])
# goal-3：报告章（待实测）
g3["title"] = "竞品分析报告"; g3["objective"] = "面向产品经理输出豆包语音输入法竞品分析报告"
g3["acceptance"] = ["报告含结论与信息源小节", "每个竞品按同一组维度交代", "缺失清单逐条带原因"]
r, = g3["agents"]; r["agent_id"] = "report-writing"; r["display_name"] = "报告撰写"
r["task"] = "基于账本中 done 章产物写竞品分析报告，缺失处原位标注"
r["prompt"]["body"] = "读者=产品经理；语言=中文；每个竞品按 准确率/方言/大模型/隐私/变现/生态 六维交代；每条判断带角标 permalink。"
r["capability"] = {"profile": "report-writer", "tools": ["fs.read", "fs.write"], "sources": [], "fs": {"read": ["goals/**"], "write": ["goals/goal-3/**"]}, "network": "none", "shell": "none"}
r["output"] = {"format": "markdown", "path": "goals/goal-3/report.md", "validators": ["file_exists", "sections_exist:结论,信息源"]}
r["chapter"] = chap("ch-1", "report", r["output"]["path"], ["豆包语音输入法"], [c["output"]["path"]])
plan = Plan.from_dict(src)

runs = ROOT / "runs"; rr = runs / RID
def w(p, obj):
    p = rr / p; p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8"); return str(p)
items = [
 {"competitor": "讯飞输入法", "title": "讯飞输入法语音识别准确率实测 98%，支持 23 种方言", "permalink": "https://example.com/iflytek-dialect", "fetched_at": "2026-08-20T02:00:00Z", "snippet": "方言免切换识别；离线包可用；会员 15 元/月"},
 {"competitor": "搜狗输入法", "title": "搜狗输入法接入腾讯混元，语音转写支持中英混说", "permalink": "https://example.com/sogou-hunyuan", "fetched_at": "2026-08-20T02:10:00Z", "snippet": "AI 润色；隐私政策提及云端处理；广告变现"},
 {"competitor": "百度输入法", "title": "百度输入法语音输入接入文心，方言覆盖 12 种", "permalink": "https://example.com/baidu-wenxin", "fetched_at": "2026-08-20T02:20:00Z", "snippet": "AI 帮写；与百度生态打通；免费"},
]
p1 = w("goals/goal-1/data-collection.json", items)
p3 = w("goals/goal-2/matrix.json", {"matrix": {"讯飞输入法": {"准确率": "98%（自称）", "方言": "23 种", "大模型": "星火", "隐私": "离线可用", "变现": "会员", "生态": "讯飞系"},
       "搜狗输入法": {"准确率": "未披露", "方言": "未知", "大模型": "混元", "隐私": "云端处理", "变现": "广告", "生态": "腾讯系"},
       "百度输入法": {"准确率": "未披露", "方言": "12 种", "大模型": "文心", "隐私": "未知", "变现": "免费", "生态": "百度系"}},
       "differentiation": [{"competitor": "讯飞输入法", "gap": "方言覆盖领先豆包", "evidence": ["https://example.com/iflytek-dialect"]}],
       "sources": [{"permalink": i["permalink"], "fetched_at": i["fetched_at"]} for i in items]})
now = lambda: datetime.now(timezone.utc).isoformat()
store.ensure_chapters(RID, [{"goal_id": "goal-1", "chapter_id": "ch-1"}, {"goal_id": "goal-1", "chapter_id": "ch-2"}, {"goal_id": "goal-2", "chapter_id": "ch-1"}, {"goal_id": "goal-3", "chapter_id": "ch-1"}], updated_at=now())
for gid, cid in [("goal-1", "ch-1"), ("goal-1", "ch-2"), ("goal-2", "ch-1")]:
    store.start_chapter(RID, gid, cid, engine="claude", updated_at=now())
store.finish_chapter(RID, "goal-1", "ch-1", status="done", reason=None, actual_output_path=p1, actual_count=3, updated_at=now())
store.finish_chapter(RID, "goal-1", "ch-2", status="missing", reason="tool_unavailable", actual_output_path=None, actual_count=0, updated_at=now())
store.finish_chapter(RID, "goal-2", "ch-1", status="done", reason=None, actual_output_path=p3, actual_count=3, updated_at=now())

events = []
async def _publish(rid, payload):
    events.append(payload)
coord = RuntimeCoordinator(store=store, event_buffer=SimpleNamespace(publish=_publish), researches={}, cards={}, runs_root=runs, routing_utc_clock=lambda: datetime.now(timezone.utc))
coord._adapters[RID] = coord.adapter_factory()
_ad = coord._adapters[RID]; _orig = _ad.run
async def _run(task, ctx, on_event=None):
    r = await _orig(task, ctx, on_event=on_event)
    print(f"\n>>> 节 {task.output_path.name} engine_error={getattr(r,'engine_error',None)!r} conclusion_error={getattr(r,'conclusion_error',None)!r} "
          f"verdict={getattr(getattr(r,'validation',None),'verdict',None)} failures={[ (f.name,f.message) for f in getattr(getattr(r,'validation',None),'failures',[])]} denials={getattr(r,'permission_denials',None)} "
          f"conclusion={getattr(r,'conclusion',None)!r}"[:900], flush=True)
    errs=[str(getattr(e,'text',''))[:200] for e in getattr(r,'events',[]) if getattr(e,'is_error',False)]
    print(">>> 错误事件:", errs[:3], flush=True)
    return r
_ad.run = _run
t0 = time.time()
async def on_event(e):
    k = getattr(e, "item_kind", None); t = str(getattr(e, "text", ""))[:160].replace("\n", " ")
    if k is not None and str(k) != "thinking": print(f"{time.time()-t0:6.1f}s [{k}] {t}", flush=True)
res = asyncio.run(coord._run_task(plan, plan.goals[2].agents[0], SimpleNamespace(goal_id="goal-3", attempt=1, engine="claude", failure_feedback=None, on_event=on_event)))
print("\n=== 结果", res, f"总耗时 {time.time()-t0:.0f}s")
for row in store.list_chapters(RID): print(dict(row))
print("\n=== 节文件"); [print(p, p.stat().st_size) for p in sorted((rr/"goals/goal-3/report").glob("*.md"))]
print("\n=== 报告正文\n", (rr/"goals/goal-3/report.md").read_text(encoding="utf-8"))
