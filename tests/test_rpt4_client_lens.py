"""§RPT-4 正式稿客户视角修正：五货的用例。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from app.store.dao import Store
from app.store.schema import initialize_database_if_empty

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "app" / "store" / "schema.sql"


# ---------------------------------------------------------------- 货 1 状态如实

def _seed(tmp_path: Path, *, second: str = "missing") -> tuple[Path, str]:
    database = tmp_path / "owli.db"
    initialize_database_if_empty(database, SCHEMA_PATH)
    research_id = "r-rpt4"
    plan = {"goals": [{"goal_id": "goal-1", "title": "采集", "agents": [
        {"agent_id": "xhs-doubao", "chapter": {"chapter_id": "ch-1", "chapter_type": "collection"},
         "capability": {"sources": ["xhs"]}, "entity": "豆包"},
        {"agent_id": "douyin-doubao", "chapter": {"chapter_id": "ch-2", "chapter_type": "collection"},
         "capability": {"sources": ["douyin"]}, "entity": "豆包"},
    ]}]}
    store = Store(database)
    store.create_report(id=research_id, title="国内大家对豆包的看法", research_question="国内大家对豆包的看法",
                        created_at="2026-09-14T00:00:00Z", status="completed", plan_snapshot=plan)
    store.ensure_chapters(research_id, [{"goal_id": "goal-1", "chapter_id": "ch-1"},
                                        {"goal_id": "goal-1", "chapter_id": "ch-2"}],
                          updated_at="2026-09-14T00:00:00Z")
    store.finish_chapter(research_id, "goal-1", "ch-1", status=second,
                         reason="timeout" if second == "missing" else None,
                         actual_output_path=None, actual_count=None, engine_error=None,
                         conclusion_error=None, updated_at="2026-09-14T00:10:00Z")
    store.finish_chapter(research_id, "goal-1", "ch-2", status="done", reason=None,
                         actual_output_path="goals/goal-1/ch-2.md", actual_count=1,
                         updated_at="2026-09-14T00:10:00Z")
    store.upsert_evidence_batch([
        {"id": f"ev-{i}", "report_id": research_id, "platform": "xhs", "agent_name": "xhs-doubao",
         "permalink": f"https://www.xiaohongshu.com/explore/{i}", "fetched_at": "2026-09-14T00:00:00Z"}
        for i in range(3)
    ] + [{"id": "ev-d", "report_id": research_id, "platform": "douyin", "agent_name": "douyin-doubao",
          "permalink": "https://www.douyin.com/video/1", "fetched_at": "2026-09-14T00:00:00Z"}])
    return database, research_id


def _get(database: Path, research_id: str, tmp_path: Path) -> dict:
    from app.api.main import create_app

    application = create_app(database, SCHEMA_PATH, runs_root=tmp_path / "runs", engine_probe=lambda: {})

    async def exercise() -> httpx.Response:
        async with application.router.lifespan_context(application):
            transport = httpx.ASGITransport(app=application)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.get(f"/api/researches/{research_id}")

    response = asyncio.run(exercise())
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_货1_已完成但有段超时没写成_快照带未写成读数且标签不动(tmp_path: Path) -> None:
    database, research_id = _seed(tmp_path)
    snapshot = _get(database, research_id, tmp_path)
    assert snapshot["status_label"] == "已完成"
    # 条数与附录缺失清单同一把尺子：按 agent_name 数这一章入库的行（3 条小红书），抖音那章 done 不算。
    assert snapshot["unwritten"] == {"sections": 1, "timeouts": 1, "yielded": 3}


def test_货1_没有缺段的研究不挂黄条(tmp_path: Path) -> None:
    database, research_id = _seed(tmp_path, second="done")
    assert _get(database, research_id, tmp_path)["unwritten"] is None


# ---------------------------------------------------------------- 货 2 实体归属

PLAN = {
    "research_question": "国内大家对豆包的看法", "title": "国内大家对豆包的看法",
    "entities": [
        {"id": "豆包", "canonical": "豆包", "names": {"zh": "豆包", "en": "Doubao", "aliases": []}},
        {"id": "Kimi", "canonical": "Kimi", "names": {"zh": "Kimi", "en": "Kimi", "aliases": ["月之暗面"]}},
    ],
    "goals": [{"goal_id": "goal-1", "agents": [
        {"agent_id": "xhs-doubao", "entity": "豆包", "chapter": {"chapter_id": "ch-1"}},
        {"agent_id": "xhs-kimi", "entity": "Kimi", "chapter": {"chapter_id": "ch-2"}},
    ]}],
}


def _coded(index: int, *, agent: str, body: str, title: str = "评论 · 父帖", attitude: str = "负",
           topics=("回答质量",), scenario: str = "其他", quote: str = "", citation_no: int | None = None,
           grade: str = "B") -> dict:
    from app.reliability.coding import CODING_VERSION

    return {
        "id": f"ev-{index}", "report_id": "r-rpt4", "platform": "xhs", "kind": "comment",
        "agent_name": agent, "title": title, "content_excerpt": body, "grade": grade,
        "citation_no": citation_no if citation_no is not None else index,
        "extra": {"content_kind": "user_opinion", "coding": {
            "coding_version": CODING_VERSION, "audience": "不明", "scenario": scenario,
            "attitude": attitude, "topics": list(topics), "quote": quote}},
    }


def test_货2_评论的实体只看评论自己的话_父帖标题里的名字不算() -> None:
    from app.report.polish.tables import coding_entity_roles

    rows = [
        # 挂在豆包帖下、却在说 Kimi：父帖标题点了豆包也不算主体
        _coded(1, agent="xhs-doubao", title="评论 · 豆包将新增付费版本", body="还是Kimi好用"),
        # 自己点名豆包 ⇒ 主体（顺带提竞品也算主体）
        _coded(2, agent="xhs-kimi", title="评论 · Kimi 教程", body="豆包比 Kimi 敷衍多了"),
        # 谁都没点名、挂在竞品章下 ⇒ 对照
        _coded(3, agent="xhs-kimi", title="评论 · Kimi 教程", body="这是一定要逼开会员啊"),
        # 谁都没点名、挂在主角章下 ⇒ 未点名（不硬归主体）
        _coded(4, agent="xhs-doubao", title="评论 · 豆包陪我吃火锅", body="好卡"),
        # 原声摘自父帖标题（编码器摘错地方）：不许借它把父帖名漏回来
        _coded(5, agent="xhs-doubao", title="评论 · 豆包收费不可怕", body="",
               quote="豆包收费不可怕"),
    ]
    roles = coding_entity_roles(PLAN, rows)
    assert roles == {
        "ev-1": {"entity": "对照", "entity_name": "Kimi"},
        "ev-2": {"entity": "主体", "entity_name": "豆包"},
        "ev-3": {"entity": "对照", "entity_name": "Kimi"},
        "ev-4": {"entity": "未点名", "entity_name": None},
        "ev-5": {"entity": "未点名", "entity_name": None},
    }


def test_货2_题面读不出主角时不分主体对照() -> None:
    from app.report.polish.tables import coding_entity_roles

    plan = {**PLAN, "research_question": "国产 AI 助手口碑如何", "title": "国产 AI 助手口碑如何"}
    assert coding_entity_roles(plan, [_coded(1, agent="xhs-kimi", body="Kimi 慢")]) == {}


def test_货2_正文态度表只数主体_对照实体另出附录表_口径写出三档条数() -> None:
    from app.report.polish.tables import build_tables

    evidence = [
        _coded(1, agent="xhs-doubao", body="豆包被戳破了还反复出错", attitude="负", scenario="其他"),
        _coded(2, agent="xhs-doubao", body="豆包是我唯一的朋友", attitude="正", topics=("功能与能力",),
               scenario="情感陪伴", quote="豆包是我唯一的朋友"),
        _coded(3, agent="xhs-kimi", body="Kimi 回答重复", attitude="负"),
        _coded(4, agent="xhs-kimi", body="一周的额度只能用三四天", attitude="负", topics=("价格与付费",)),
        _coded(5, agent="xhs-doubao", body="好卡", attitude="负", topics=("速度与稳定",)),
    ]
    data = build_tables(report={"id": "r-rpt4"}, plan=PLAN, claims=[], evidence=evidence,
                        view={"title": "t", "sources": []})
    tables = data["tables"]
    by_topic = {(r["主题"], r["态度"]): (r["条数"], r["marks"]) for r in tables["attitude_by_topic"]["rows"]}
    assert tables["attitude_by_topic"]["n"] == 2
    assert by_topic[("回答质量", "负")] == (1, ["S01"])
    assert tables["scenario_attitude"]["n"] == tables["scenario_counts"]["n"] == 2
    contrast = tables["contrast_attitude"]
    assert contrast["n"] == 2 and [(r["对照实体"], r["态度"], r["条数"]) for r in contrast["rows"]] == [
        ("Kimi", "负", 2)]
    basis = tables["scenario_attitude"]["basis"]
    assert "点名了研究对象的 2 条" in basis and "对照实体的 2 条" in basis and "没点名的 1 条" in basis
    # C-2 甲：全在引用池里的编码要说「引用池里的评论」「不是随机抽样」
    assert "引用池里的评论" in basis and "不是随机抽样" in basis
    assert data["subjects"] == ["豆包"]
    # 原声表仍按全部行挑（原声闸本身只收点名主角的句子）
    assert [q["原声"] for q in tables["quotes"]["rows"]] == ["豆包是我唯一的朋友"]


def test_货2_总体态度行插在把握度之前_四数之和等于表的n(tmp_path: Path) -> None:
    from app.report.polish.run import assemble, attitude_line

    tables = {"scenario_attitude": {"n": 24, "rows": [
        {"场景": "情感陪伴", "态度": "正", "条数": 8}, {"场景": "其他", "态度": "正", "条数": 7},
        {"场景": "其他", "态度": "负", "条数": 3}, {"场景": "其他", "态度": "中", "条数": 5},
        {"场景": "其他", "态度": "混合", "条数": 1}]}}
    line = attitude_line(tables, ["豆包"])
    assert line.startswith("已编码评论 24 条：正 15 / 负 3 / 中 5 / 混合 1（只数点名了豆包的评论")
    part = tmp_path / "01-执行摘要.md"
    part.write_text("摘要第一段。\n\n> 本报告结论的把握度为**低**。\n\n后文。", encoding="utf-8")
    text = assemble([("执行摘要", part)], tables=tables, subjects=["豆包"])
    assert text.index("已编码评论 24 条") < text.index("> 本报告结论的把握度")
    assert attitude_line({}, ["豆包"]) == ""


def test_货2_编码落库带实体_回填只写变了的行(tmp_path: Path) -> None:
    import json

    from app.reliability.coding import _coding_payload, assign_coding_entities

    database = tmp_path / "owli.db"
    initialize_database_if_empty(database, SCHEMA_PATH)
    store = Store(database)
    store.create_report(id="r-rpt4", title="t", research_question=PLAN["research_question"],
                        created_at="2026-09-15T00:00:00Z", plan_snapshot=PLAN)
    label = {"audience": "不明", "scenario": "其他", "attitude": "负", "topics": [], "quote": ""}
    payload = _coding_payload(_coded(9, agent="xhs-kimi", body="慢"), label,
                              {"entity": "对照", "entity_name": "Kimi"})
    assert payload["extra"]["coding"]["entity"] == "对照"
    store.upsert_evidence_batch([
        {**{k: v for k, v in _coded(i, agent=a, body=b).items() if k != "grade"},
         "permalink": f"https://www.xiaohongshu.com/explore/{i}",
         "parent_permalink": "https://www.xiaohongshu.com/explore/parent",
         "fetched_at": "2026-09-15T00:00:00Z"}
        for i, a, b in ((1, "xhs-doubao", "豆包太笨"), (2, "xhs-kimi", "会员太贵"))])
    assert assign_coding_entities(store, "r-rpt4") == 2
    assert assign_coding_entities(store, "r-rpt4") == 0, "判法没变就一行都不写"
    got = {r["id"]: (r["extra"] if isinstance(r["extra"], dict) else json.loads(r["extra"]))["coding"]
           for r in store.list_evidence("r-rpt4")}
    assert got["ev-1"]["entity"] == "主体" and got["ev-2"]["entity"] == "对照"
    assert got["ev-1"]["attitude"] == "负", "只补实体，编码其余字段不动"


# ---------------------------------------------------------------- 货 3 写手规则与软检

def test_货3_题面问国内时海外平台与对照实体的源带旁证标_写手池里看得见() -> None:
    from app.report.polish.run import build_prompt
    from app.report.polish.skills import get_template
    from app.report.polish.tables import offtopic_reason

    assert offtopic_reason({"platform": "reddit"}, contrast=False, question="国内大家对豆包的看法") == "海外平台"
    assert offtopic_reason({"platform": "reddit"}, contrast=False, question="大家对豆包的看法") is None
    assert offtopic_reason({"platform": "xhs"}, contrast=True, question="国内大家对豆包的看法") == "对照实体"
    assert offtopic_reason({"platform": "xhs"}, contrast=None, question="国内大家对豆包的看法") is None
    data = {"research_question": "国内大家对豆包的看法", "tables": {}, "sources": [
        {"mark": "S01", "grade": "A", "title": "Devs forgot", "url": "u", "offtopic": "海外平台"},
        {"mark": "S02", "grade": "B", "title": "豆包", "url": "u2"}]}
    prompt = build_prompt(get_template("consulting"), data, "# 工作稿\n", Path("/tmp/x.md"))
    assert "- S01｜A 级｜未登记｜旁证·海外平台｜Devs forgot｜u" in prompt
    assert "- S02｜B 级｜未登记｜豆包｜u2" in prompt


def test_货3_把握度行去掉程序前缀并接正文实引数_引用池数只在与实引不同时写(tmp_path: Path) -> None:
    from app.report.polish.run import confidence_line

    tables = {"crossref_mix": {"n": 3, "rows": [{"交叉验证结论": "SINGLE", "主张数": 3}]}}
    assert confidence_line(tables) == "主张 3 条：单源 3。"
    assert confidence_line(tables, {"cited": 80, "正文实引": 28}) == \
        "主张 3 条：单源 3。正文实际引用证据 28 条（引用池共 80 条）。"


def test_货3_小节名与读者不明提示不再出现机器话() -> None:
    from app.report.polish.run import IMPLICATIONS_SECTION, _audience_view
    from app.report.polish.skills import get_template, shared_rules

    assert IMPLICATIONS_SECTION == "这意味着什么"
    assert "这意味着什么" in get_template("competitor-matrix").sections
    for name in ("consulting", "competitor-matrix", "sentiment-brief"):
        assert "对提问方" not in get_template(name).body
    rules = shared_rules()
    assert "对提问方意味着什么" not in rules
    assert "本次未采到官方口径" in rules and "旁证·对照实体" in rules
    assert "身份未知" not in _audience_view({}).split("⛔")[0]


def _ruler():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "check_polished_rpt4", ROOT / "scripts" / "acceptance" / "rpt1" / "check_polished.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_货3_四道软检判黄不判红(tmp_path: Path) -> None:
    import json

    ruler = _ruler()
    md = tmp_path / "r.polished.consulting.md"
    md.write_text(
        "# 执行摘要\n\n答案[S01]。\n\n"
        "1. 【A】豆包情感陪伴被夸[S01][S02][S03]\n"
        "2. 【B】豆包回答质量被骂[S01][S02][S04]\n"
        "3. 【A】海外玩家嘲笑水印[S05][S06]\n\n"
        "> 本报告结论的把握度为**低**。\n\n"
        # 同一组数换了表形（透视 vs 长表）也要认出是同一张表
        "# 关键发现\n\n## 一\n\n| 场景 | 正 | 负 |\n|---|---|---|\n| 陪伴 | 10 | 4 |\n\n"
        "## 二\n\n| 场景 | 态度 | 条数 |\n|---|---|---|\n| 陪伴 | 正 | 10 |\n| 陪伴 | 负 | 4 |\n\n"
        "占比 0.8226，支撑本报告结论的是 80 条被引证据。\n\n"
        "# 附录\n\n无。\n", encoding="utf-8")
    tables = md.with_name("r.polished.consulting.tables.json")
    tables.write_text(json.dumps({"sources": [
        {"mark": "S05", "offtopic": "海外平台"}, {"mark": "S06", "offtopic": "海外平台"},
        {"mark": "S01"}]}, ensure_ascii=False), encoding="utf-8")
    warns = ruler.warnings_of(md)
    assert len(warns["⒞ 摘要关键发现跑题"]) == 1 and "第 3 条" in warns["⒞ 摘要关键发现跑题"][0]
    assert len(warns["⒟ 两条关键发现角标重合"]) == 1
    assert len(warns["⒠ 同一张表正文重复"]) == 1
    hits = " ".join(warns["⒡ 机器话残留"])
    assert "占比 0.8" in hits and "80 条被引证据" in hits
    assert "⒞ 摘要关键发现跑题" not in ruler.CHECKS, "软检只判黄"


# ---------------------------------------------------------------- 货 4 表口径

def _post(i: int, platform: str, *, parent: str | None = None, cited: int | None = None,
          published: str | None = None, text: str = "") -> dict:
    return {"id": f"p-{i}", "platform": platform, "kind": "comment" if parent else "post",
            "permalink": f"https://x/{i}", "parent_permalink": parent, "citation_no": cited,
            "published_at": published, "title": text, "content_excerpt": text}


def test_货4_平台表有独立帖子数_同帖评论算一个帖子_写手池标同帖() -> None:
    from app.report.polish.tables import build_tables

    evidence = [_post(1, "xhs", parent="https://x/a", cited=1), _post(2, "xhs", parent="https://x/a", cited=4),
                _post(3, "xhs", parent="https://x/b"), _post(4, "xhs")]
    data = build_tables(report={"id": "r"}, plan={}, claims=[], evidence=evidence,
                        view={"title": "t", "sources": [{"citation_no": 1}, {"citation_no": 4}]})
    table = data["tables"]["platform_mix"]
    assert "独立帖子数" in table["columns"]
    row = table["rows"][0]
    assert (row["采集条数"], row["独立帖子数"], row["被引条数"], row["被引来自帖子数"]) == (4, 3, 2, 1)
    assert {s["mark"]: s["same_thread"] for s in data["sources"]} == {"S01": ["S04"], "S04": ["S01"]}


def test_货4_提及量表带采集平台数_平台数不同时口径写不可横向比较() -> None:
    from app.report.polish.tables import _entity_mentions

    plan = {"entities": [{"id": "豆包", "canonical": "豆包", "names": {"zh": "豆包"}},
                         {"id": "Kimi", "canonical": "Kimi", "names": {"en": "Kimi"}}],
            "goals": [{"agents": [
                {"entity": "豆包", "capability": {"sources": ["xhs"]}},
                {"entity": "豆包", "capability": {"sources": ["douyin"]}},
                {"entity": "Kimi", "capability": {"sources": ["xhs"]}}]}]}
    table = _entity_mentions([], [], plan)
    assert {r["实体"]: r["采集平台数"] for r in table["rows"]} == {"豆包": 2, "Kimi": 1}
    assert "不可横向比较" in table["basis"]
    same = {**plan, "goals": [{"agents": [{"entity": "豆包", "capability": {"sources": ["xhs"]}},
                                          {"entity": "Kimi", "capability": {"sources": ["xhs"]}}]}]}
    assert "不可横向比较" not in _entity_mentions([], [], same)["basis"]


def test_货4_时间集中区间取最短覆盖八成的连续月份_附录一句由程序挂() -> None:
    from app.report.polish.run import PROGRAM_APPENDIX_HEADINGS, TIMESPAN_HEADING, timespan_block
    from app.report.polish.tables import _timeline, concentration_window

    assert concentration_window({"2025-01": 1, "2026-03": 4, "2026-04": 5}) == {
        "起": "2026-03", "止": "2026-04", "条数": 9, "占比": 0.9}
    rows = [_post(i, "xhs", published=m) for i, m in enumerate(["2025-01"] + ["2026-03"] * 4 + ["2026-04"] * 5)]
    table = _timeline(rows)
    block = timespan_block({"timeline": table})
    assert block.startswith(TIMESPAN_HEADING) and TIMESPAN_HEADING in PROGRAM_APPENDIX_HEADINGS
    assert "集中在 2026-03 至 2026-04，占有发布时间的 10 条里的 90%" in block
    assert timespan_block({}) == ""


# ---------------------------------------------------------------- 货 5 信息源标题只剩一种写法

def test_货5_信息源清单标题归一成评论点帖子标题_有独立标题用库里原题() -> None:
    from app.report.polish.run import source_title, sources_table

    assert source_title({"title": "「评论 · 成年人不配有情感陪伴？」"}) == "评论 · 成年人不配有情感陪伴？"
    assert source_title({"title": "评论 · 「DeepSeek 官方信息发布」"}) == "评论 · DeepSeek 官方信息发布"
    assert source_title({"title": "「评论：豆包收费不可怕」"}) == "评论 · 豆包收费不可怕"
    # Reddit：工作稿誊成了中文译名，清单用库里的原题（与页面证据表一致）
    assert source_title({"title": "「评论：开发者忘记移除豆包水印」",
                         "raw_title": "评论 · Devs forgot to remove the Doubao watermark",
                         "title_independent": True}) == "评论 · Devs forgot to remove the Doubao watermark"
    # 微博：库里 title 是正文拷贝，沿用工作稿的概括标题
    assert source_title({"title": "「恩师豆包」", "raw_title": "恩师豆包\n我教师节都忘给豆包发",
                         "title_independent": False}) == "恩师豆包"
    assert source_title({"title": "x" * 80}).endswith("…")
    table = sources_table([{"mark": "S01", "grade": "A", "title": "「评论 · 帖」", "url": "u"}])
    assert "| 评论 · 帖 |" in table and "「" not in table


def test_逐字闸不把全角弯引号落成半角当改字_改了字照样打回() -> None:
    from app.report.polish.run import altered_quotes
    from app.reliability.coding import _squeeze

    corpus = _squeeze("在我看来“是否下载猫箱”是一场典型的囚徒博弈，我个人认为仍然会有很多人去下载的")
    ok = '> 在我看来"是否下载猫箱"是一场典型的囚徒博弈，我个人认为仍然会有很多人去下载的\n> —— 小红书 · 等级 B [S28]\n'
    assert altered_quotes(ok, corpus) == []
    bad = ok.replace("囚徒博弈", "囚徒困境")
    assert altered_quotes(bad, corpus), "改了字照样打回"
