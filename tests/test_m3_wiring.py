from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace


NOW = "2026-08-21T00:00:00+00:00"
RESEARCH_ID = "r-m3-wiring"


def _goal(number: int, agents: list[dict], *, format_name: str = "json") -> dict:
    shape = "object" if format_name == "markdown" else "array"
    return {
        "title": f"阶段{number}",
        "objective": f"形成阶段{number}可独立复核的产物。",
        "depends_on": [] if number == 1 else [f"goal-{number - 1}"],
        "deliverable": {
            "format": format_name,
            "shape": shape,
            "path": f"stage-{number}.{'md' if format_name == 'markdown' else 'json'}",
            "description": "可供下游复核的结构化产物。",
        },
        "acceptance": ["文件存在且至少包含 1 条可判定记录"],
        "agents": [{**agent, "output": {"shape": shape}} for agent in agents],
    }


def _multi_source_skeleton() -> dict:
    return {
        "market_profile": "global_product",
        "market_profile_justification": "产品面向全球市场。",
        "subjects": ["飞书"],
        "subjects_justification": "研究主体为飞书。",
        "goals": [
            _goal(1, [
                {"name": "HN 数据抓取·飞书", "task": "采集 Hacker News 讨论"},
                {"name": "网页搜索数据抓取·飞书", "task": "采集官方与评测原文"},
                {"name": "Product Hunt 数据抓取·飞书", "task": "采集发布与评论"},
                {"name": "X 数据抓取·飞书", "task": "采集近期用户反馈"},
            ]),
            _goal(2, [{"name": "可靠度审计", "task": "完成闭集判定与五维评分"}]),
            _goal(
                3,
                [{"name": "报告撰写", "task": "输出带双向角标的信息源清单"}],
                format_name="markdown",
            ),
        ]
    }


class PlanStore:
    def __init__(self, root: Path) -> None:
        self.runs_root = root / "runs"
        self.saved: list[dict] = []

    def get_drafting_report(self, query: str):
        return {
            "id": RESEARCH_ID,
            "research_question": query,
            "created_at": NOW,
            "extra": {"plan_generated_at": NOW},
        }

    def save_plan_snapshot(self, report_id, *, snapshot, expected_rev):
        assert report_id == RESEARCH_ID and expected_rev == 0
        self.saved.append(deepcopy(snapshot))


class PlanEngine:
    def __init__(self, skeleton: dict) -> None:
        self.skeleton = skeleton
        self.tasks = []
        self.entity_tasks = []

    async def run(self, task, ctx, on_event=None):
        del ctx, on_event
        if task.output_path.stem.startswith("entity-"):
            # §ENT-1 货 1：实体卡段与其余规划段分开记，见 test_plan_generate 同注。
            self.entity_tasks.append(task)
        else:
            self.tasks.append(task)
        task.output_path.parent.mkdir(parents=True, exist_ok=True)
        if task.output_path.stem.startswith("entity-"):
            # §ENT-1 货 1：goals 之前多一层实体卡段，每个 subject 一次短流。
            index = int(task.output_path.stem.removeprefix("entity-"))
            name = self.skeleton["subjects"][index - 1]
            payload = {
                "canonical": name,
                # §ENT-2：只留中文叫法——给了英文名就等于要求分配表再排一张海外卡。
                "names": {"zh": name, "en": None, "aliases": []},
                "official_handles": {},
                "same_product": True,
                "note": f"{name} 的实体卡（替身引擎产出）",
            }
        elif task.output_path.name == "skeleton.json":
            payload = {
                "market_profile": self.skeleton["market_profile"],
                "market_profile_justification": self.skeleton[
                    "market_profile_justification"
                ],
                "subjects": self.skeleton["subjects"],
                "subjects_justification": self.skeleton[
                    "subjects_justification"
                ],
                "goals": [
                    {
                        "title": goal["title"],
                        "objective": goal["objective"],
                        "depends_on": goal["depends_on"],
                    }
                    for goal in self.skeleton["goals"]
                ]
            }
        elif "-ch-" in task.output_path.stem:
            number = int(task.output_path.stem.split("-ch-", 1)[0].removeprefix("goal-"))
            output_path = json.loads(
                task.body.split("系统声明 output.path=", 1)[1].split("。", 1)[0]
            )
            payload = {
                "chapter_type": {1: "collection", 2: "audit", 3: "report"}[number],
                "opening": {
                    "inputs": [], "task": "执行本章", "acceptance": ["产物通过校验"],
                },
                "closing": {
                    "output": {"path": output_path}, "entities": ["飞书"],
                    "expected_count": 1, "notes": {},
                },
            }
        else:
            number = int(task.output_path.stem.removeprefix("goal-"))
            goal = self.skeleton["goals"][number - 1]
            payload = {
                "deliverable": goal["deliverable"],
                "acceptance": goal["acceptance"],
                "agents": goal["agents"],
            }
        task.output_path.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        return SimpleNamespace(succeeded=True)


def test_多源计划生成由注册表补齐能力与产物契约(tmp_path: Path) -> None:
    from app.adapters.routing import RoutedAdapter
    from app.plan.generate import generate_plan

    engine = PlanEngine(_multi_source_skeleton())
    store = PlanStore(tmp_path)
    adapter = RoutedAdapter(
        adapters={"claude": engine, "codex": engine},
    )

    plan = asyncio.run(generate_plan("飞书竞品优缺点", store, adapter))

    # §RATE-1 货 2：每个采集章后面跟着自动排出的评级章，这里只看采集章。
    collectors = [
        agent for agent in plan.goals[0].agents
        if agent.capability["profile"] == "web-collector"
    ]
    expected = ["hacker_news", "web_search", "product_hunt", "x"]
    assert [agent.capability["sources"][0] for agent in collectors] == expected
    for agent, source_id in zip(collectors, expected):
        assert agent.capability["tools"][0] == f"source.{source_id}"
        assert agent.output["format"] == "json"
        assert "json_array_min_items:1" in agent.output["validators"]
        assert "each_item_has:permalink,fetched_at" in agent.output["validators"]
    planning_prompt = engine.tasks[1].body
    assert all(
        name in planning_prompt
        for name in ("Hacker News", "网页搜索", "Product Hunt", "X")
    )
    assert "capability.sources" in planning_prompt


class ClassificationEngine:
    def __init__(self, answers: list[dict]) -> None:
        self.answers = answers
        self.tasks = []

    async def run(self, task, ctx, on_event=None):
        del ctx, on_event
        self.tasks.append(task)
        answer = self.answers[min(len(self.tasks) - 1, len(self.answers) - 1)]
        task.output_path.parent.mkdir(parents=True, exist_ok=True)
        task.output_path.write_text(json.dumps([answer], ensure_ascii=False), encoding="utf-8")
        return SimpleNamespace(succeeded=True)


def _evidence() -> list[dict]:
    return [{
        "platform": "hacker_news",
        "permalink": "https://news.ycombinator.com/item?id=1",
        "title": "Ask HN: Teams alternatives",
        "author_name": "alice",
        "source_type": "post",
        "fetch_method": "official_api",
        "published_at": "2026-08-20T00:00:00Z",
        "fetched_at": NOW,
        "has_body": True,
        "normalized_score": 0.9,
        "extra": {"content_kind": "user_opinion"},
    }]


def test_LLM_闭集越界经_RoutedAdapter_重试后接受合法值(tmp_path: Path) -> None:
    from app.adapters.routing import RoutedAdapter
    from app.reliability.audit import classify_and_score

    engine = ClassificationEngine([
        {"authority_kind": "高权威", "interest_relation": "中立"},
        {"authority_kind": "community_high_signal", "interest_relation": "arms_length"},
    ])
    adapter = RoutedAdapter(
        adapters={"claude": engine, "codex": engine},
    )

    result = asyncio.run(classify_and_score(
        _evidence(),
        adapter=adapter,
        output_path=tmp_path / "runs" / RESEARCH_ID / "goals" / "goal-2" / "audit.json",
        research_id=RESEARCH_ID,
        goal_id="goal-2",
        agent_id="reliability-audit",
    ))

    assert len(engine.tasks) == 2
    assert result[0]["extra"]["authority_kind"] == "community_high_signal"
    assert result[0]["extra"]["interest_relation"] == "arms_length"
    assert result[0]["rated_by"] == "agent:reliability-audit"
    assert "上一轮闭集错误" in engine.tasks[1].body


def test_LLM_闭集连续三次越界取平台基线并标注_degraded(tmp_path: Path) -> None:
    from app.adapters import validation
    from app.adapters.routing import RoutedAdapter
    from app.reliability.audit import classify_and_score
    from app.reliability.scoring import PLATFORM_BASELINES, rating_notes_problem

    engine = ClassificationEngine([
        {"authority_kind": "高权威", "interest_relation": "中立"}
    ])
    adapter = RoutedAdapter(
        adapters={"claude": engine, "codex": engine},
    )

    result = asyncio.run(classify_and_score(
        _evidence(),
        adapter=adapter,
        output_path=tmp_path / "runs" / RESEARCH_ID / "goals" / "goal-2" / "audit.json",
        research_id=RESEARCH_ID,
        goal_id="goal-2",
        agent_id="reliability-audit",
    ))

    item = result[0]
    assert len(engine.tasks) == 3
    assert {
        field: item[field] for field in PLATFORM_BASELINES["hacker_news"]
    } == PLATFORM_BASELINES["hacker_news"]
    assert item["extra"]["reliability_degraded"]["attempts"] == 3
    assert item["rated_by"] == "baseline:hacker_news@v1:degraded"
    assert item["rating_notes"].endswith("⚠️闭集判定降级")
    assert rating_notes_problem(item["rating_notes"], item) is None
    path = tmp_path / "runs" / RESEARCH_ID / "goals" / "goal-2" / "audit.json"
    ctx = validation.Ctx(
        output_path=path,
        output_format="json",
        research_id=RESEARCH_ID,
        goal_id="goal-2",
        agent_id="reliability-audit",
        read_text=lambda: path.read_text(encoding="utf-8"),
        read_json=lambda: json.loads(path.read_text(encoding="utf-8")),
        store=None,
        source_domains=frozenset(),
        runs_root=tmp_path / "runs",
    )
    assert validation.validate(
        ctx, ["field_domain_whitelist:reliability_closed_set"]
    ).verdict is validation.Verdict.PASS


def test_闭集_validator_真实执行且拒绝_LLM_伪造_degraded(tmp_path: Path) -> None:
    from app.adapters import validation

    path = tmp_path / "runs" / RESEARCH_ID / "goals" / "goal-2" / "audit.json"
    path.parent.mkdir(parents=True)
    forged = _evidence()[0] | {
        "rated_by": "agent:reliability-audit:degraded",
        "extra": {"reliability_degraded": {"attempts": 1}},
    }
    path.write_text(json.dumps([forged], ensure_ascii=False), encoding="utf-8")
    ctx = validation.Ctx(
        output_path=path,
        output_format="json",
        research_id=RESEARCH_ID,
        goal_id="goal-2",
        agent_id="reliability-audit",
        read_text=lambda: path.read_text(encoding="utf-8"),
        read_json=lambda: json.loads(path.read_text(encoding="utf-8")),
        store=None,
        source_domains=frozenset(),
        runs_root=tmp_path / "runs",
    )

    report = validation.validate(
        ctx, ["field_domain_whitelist:reliability_closed_set"]
    )

    assert report.verdict is validation.Verdict.FAIL
    assert report.unavailable == []
    assert report.failures[0].offenders == [
        "items[0].extra.reliability_degraded"
    ]


def test_信息源清单渲染五维分理由且沿用双向校验四件套(tmp_path: Path) -> None:
    from app.adapters import validation
    from app.report.markdown import render_report

    evidence = _evidence()[0] | {
        "citation_no": 1,
        "score_authority": 1,
        "score_freshness": 2,
        "score_crossref": 1,
        "score_completeness": 2,
        "score_independence": 2,
        "rating_notes": (
            "权威1:社区高信号作者 · 时效2:距采集1天 · 交叉1:弱源或已说明分歧 · "
            "完整2:正文作者时间齐全 · 无关2:无可见利益关系"
        ),
    }
    path = tmp_path / "runs" / RESEARCH_ID / "goals" / "goal-3" / "report.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        render_report(["飞书的开放集成有社区正向反馈 [S01]"], [evidence]),
        encoding="utf-8",
    )
    ctx = validation.Ctx(
        output_path=path,
        output_format="markdown",
        research_id=RESEARCH_ID,
        goal_id="goal-3",
        agent_id="report-writing",
        read_text=lambda: path.read_text(encoding="utf-8"),
        read_json=lambda: None,
        store=None,
        source_domains=frozenset(),
        runs_root=tmp_path / "runs",
    )

    report = validation.validate(ctx, [
        "file_exists",
        "sections_exist:结论,信息源",
        "citation_marks_resolvable",
        "no_orphan_citation",
    ])
    text = path.read_text(encoding="utf-8")
    assert report.verdict is validation.Verdict.PASS
    assert "[S01] [Ask HN: Teams alternatives](https://news.ycombinator.com/item?id=1)" in text
    assert "fetched_at=2026-08-21T00:00:00+00:00" in text
    assert "权威1/时效2/交叉1/完整2/无关2" in text
    assert evidence["rating_notes"] in text


def test_既有报告信息源章节按_permalink_补入五维数据() -> None:
    from app.report.markdown import enrich_source_section

    evidence = _evidence()[0] | {
        "score_authority": 1,
        "score_freshness": 2,
        "score_crossref": 1,
        "score_completeness": 2,
        "score_independence": 2,
        "rating_notes": (
            "权威1:社区高信号作者 · 时效2:距采集1天 · 交叉1:弱源或已说明分歧 · "
            "完整2:正文作者时间齐全 · 无关2:无可见利益关系"
        ),
    }
    original = (
        "# 结论\n\n- 飞书有开放集成优势 [S01]\n\n# 信息源\n\n"
        "- [S01] [HN 原文](https://news.ycombinator.com/item?id=1)\n"
    )

    enriched = enrich_source_section(original, [evidence])

    assert "[S01] [HN 原文](https://news.ycombinator.com/item?id=1)" in enriched
    assert "fetched_at=2026-08-21T00:00:00+00:00" in enriched
    assert "五维=权威1/时效2/交叉1/完整2/无关2" in enriched
    assert evidence["rating_notes"] in enriched


def test_成稿信息源清单可解析为_permalink_到citation_no_映射() -> None:
    from app.report.markdown import source_citations

    report = (
        "# 结论\n\n- 结论一 [S01]\n- 结论二 [S02]\n\n# 信息源\n\n"
        "- [S01] [HN](https://NEWS.YCOMBINATOR.com:443/item?id=1#top)\n"
        "- [S02] [PH](https://www.producthunt.com/posts/lark/)\n"
    )

    assert source_citations(report) == {
        "https://news.ycombinator.com/item?id=1": 1,
        "https://www.producthunt.com/posts/lark": 2,
    }


def test_同_permalink_报告清单优先可靠度审计产物(tmp_path: Path) -> None:
    from app.report.markdown import load_evidence_artifacts

    root = tmp_path / "runs" / RESEARCH_ID
    collection = root / "goals" / "goal-1" / "product-hunt.json"
    audit = root / "goals" / "goal-2" / "audit.json"
    collection.parent.mkdir(parents=True)
    audit.parent.mkdir(parents=True)
    base = _evidence()[0] | {
        "platform": "product_hunt",
        "permalink": "https://www.producthunt.com/posts/lark",
        "score_authority": 1,
        "score_freshness": 1,
        "score_crossref": 0,
        "score_completeness": 1,
        "score_independence": 1,
        "rating_notes": (
            "权威1:采集基线 · 时效1:采集基线 · 交叉0:采集基线 · "
            "完整1:采集基线 · 无关1:采集基线"
        ),
        "rated_by": "baseline:product_hunt@v1",
    }
    audited = base | {
        "score_authority": 2,
        "score_independence": 2,
        "rating_notes": (
            "权威2:官方 maker · 时效1:时窗内 · 交叉0:尚未交叉 · "
            "完整1:仅主楼 · 无关2:无可见利益关系"
        ),
        "rated_by": "agent:reliability-audit",
    }
    collection.write_text(json.dumps([base], ensure_ascii=False), encoding="utf-8")
    audit.write_text(json.dumps([audited], ensure_ascii=False), encoding="utf-8")

    items = load_evidence_artifacts(root)

    assert len(items) == 1
    assert items[0]["rated_by"] == "agent:reliability-audit"
    assert items[0]["score_authority"] == 2


def test_报告finalizer拒绝缺少五维分与_rating_notes_的清单条目(tmp_path: Path) -> None:
    from app.adapters import validation

    path = tmp_path / "runs" / RESEARCH_ID / "goals" / "goal-3" / "report.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "# 结论\n\n- 飞书有开放集成优势 [S01]\n\n# 信息源\n\n"
        "- [S01] [HN](https://news.ycombinator.com/item?id=1)\n",
        encoding="utf-8",
    )
    ctx = validation.Ctx(
        output_path=path,
        output_format="markdown",
        research_id=RESEARCH_ID,
        goal_id="goal-3",
        agent_id="report-finalizer",
        read_text=lambda: path.read_text(encoding="utf-8"),
        read_json=lambda: None,
        store=None,
        source_domains=frozenset(),
        runs_root=tmp_path / "runs",
    )

    report = validation.validate(ctx, ["citation_marks_resolvable"])

    assert report.verdict is validation.Verdict.FAIL
    assert report.failures[0].offenders == ["[S01]"]
    assert "五维分" in report.failures[0].message


def test_报告finalizer拒绝不可点回原文的清单条目(tmp_path: Path) -> None:
    from app.adapters import validation

    path = tmp_path / "runs" / RESEARCH_ID / "goals" / "goal-3" / "report.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "# 结论\n\n- 飞书有开放集成优势 [S01]\n\n# 信息源\n\n"
        "- [S01] HN 原文（fetched_at=2026-08-21T00:00:00+00:00）"
        " · 五维=权威1/时效2/交叉1/完整2/无关2 · "
        "rating_notes=权威1:社区作者 · 时效2:距采集1天 · 交叉1:弱源 "
        "· 完整2:正文完整 · 无关2:无利益关系\n",
        encoding="utf-8",
    )
    ctx = validation.Ctx(
        output_path=path,
        output_format="markdown",
        research_id=RESEARCH_ID,
        goal_id="goal-3",
        agent_id="report-finalizer",
        read_text=lambda: path.read_text(encoding="utf-8"),
        read_json=lambda: None,
        store=None,
        source_domains=frozenset(),
        runs_root=tmp_path / "runs",
    )

    report = validation.validate(ctx, ["citation_marks_resolvable"])

    assert report.verdict is validation.Verdict.FAIL
    assert "可点击" in report.failures[0].detail["items"][0]["problem"]


def test_报告清单不得整体遗漏已消费平台_product_hunt(tmp_path: Path) -> None:
    from app.adapters import validation
    from app.report.markdown import load_evidence_artifacts, render_report

    root = tmp_path / "runs" / RESEARCH_ID
    evidence_path = root / "goals" / "goal-2" / "normalized.json"
    evidence_path.parent.mkdir(parents=True)
    platforms = ["hacker_news", "x", "product_hunt"]
    evidence = [
        {
            "platform": platform,
            "platform_item_id": str(index),
            "permalink": f"https://example.com/{platform}/{index}",
            "title": platform,
            "fetched_at": NOW,
            "score_authority": 1,
            "score_freshness": 2,
            "score_crossref": 1,
            "score_completeness": 2,
            "score_independence": 2,
            "rating_notes": (
                "权威1:来源可核 · 时效2:时间窗内 · 交叉1:已有弱交叉 · "
                "完整2:字段齐全 · 无关2:无利益关系"
            ),
            "rated_by": f"baseline:{platform}@v1",
        }
        for index, platform in enumerate(platforms, start=1)
    ]
    raw_unconsumed = {
        "platform": "reddit",
        "permalink": "https://example.com/reddit/raw",
        "fetched_at": NOW,
    }
    evidence_path.write_text(
        json.dumps(
            {"evidence": [*evidence, raw_unconsumed]}, ensure_ascii=False
        ),
        encoding="utf-8",
    )
    assert {
        item["platform"] for item in load_evidence_artifacts(root)
    } == {"hacker_news", "x", "product_hunt"}
    report_path = root / "goals" / "goal-3" / "report.md"
    report_path.parent.mkdir(parents=True)

    def check(selected: list[dict]):
        report_path.write_text(
            render_report(
                [
                    f"平台证据形成可执行结论 [S{index:02d}]"
                    for index in range(1, len(selected) + 1)
                ],
                [item | {"citation_no": index}
                 for index, item in enumerate(selected, start=1)],
            ),
            encoding="utf-8",
        )
        ctx = validation.Ctx(
            output_path=report_path,
            output_format="markdown",
            research_id=RESEARCH_ID,
            goal_id="goal-3",
            agent_id="report-finalizer",
            read_text=lambda: report_path.read_text(encoding="utf-8"),
            read_json=lambda: None,
            store=None,
            source_domains=frozenset(),
            runs_root=tmp_path / "runs",
        )
        return validation.validate(ctx, ["citation_marks_resolvable"])

    missing = check(evidence[:2])
    assert missing.verdict is validation.Verdict.FAIL
    assert missing.failures[0].offenders == ["product_hunt"]
    assert "已消费平台" in missing.failures[0].message

    complete = check(evidence)
    assert complete.verdict is validation.Verdict.PASS


def test_X_超预算软提示经_source工具进入事件流且调用不断链() -> None:
    from app.adapters.capability import Capability
    from app.adapters.routing import RoutedAdapter

    events: list[dict] = []

    def fake_x(query: str, window: str, *, on_event=None):
        del query, window
        on_event({
            "type": "card_update",
            "data": {
                "card": {
                    "card_id": "x-budget-1",
                    "card_type": "EXTRA_QUOTA_CONFIRM",
                    "research_id": "source-x",
                    "goal_id": None,
                    "agent_id": None,
                    "title": "X 信息源预算提示",
                    "body": "这是软提示，本次任务继续执行。",
                    "target": {"source": "x"},
                    "actions": [{"type": "CHOICE_2", "options": ["继续", "稍后"]}],
                    "blocking": "none",
                    "deadline": None,
                    "status": "pending",
                    "result": None,
                    "created_at": NOW,
                    "resolved_at": None,
                }
            },
        })
        return SimpleNamespace(evidence=[], conclusion={"status": "completed", "task_continues": True})

    adapter = RoutedAdapter(
        adapters={"claude": object(), "codex": object()},
        source_tools={"source.x": fake_x},
    )
    result = asyncio.run(adapter.call_source(
        "source.x",
        "飞书",
        "7d",
        research_id=RESEARCH_ID,
        goal_id="goal-1",
        agent_id="data-collection-x",
        capability=Capability(
            tools=("source.x",),
            sources=("x",),
            network="sources_only",
        ),
        on_event=events.append,
    ))

    assert result.conclusion["task_continues"] is True
    assert events[0]["data"]["card"]["research_id"] == RESEARCH_ID
    assert events[0]["data"]["card"]["goal_id"] == "goal-1"
    assert events[0]["data"]["card"]["agent_id"] == "data-collection-x"
    assert events[0]["data"]["card"]["blocking"] == "none"
    assert "继续执行" in events[0]["data"]["card"]["body"]


def test_source工具桥兼容不声明_on_event_的_HN_入口() -> None:
    from app.adapters.capability import Capability
    from app.adapters.routing import RoutedAdapter

    def fake_hn(query: str, window: str):
        return [{"platform": "hacker_news", "query": query, "window": window}]

    adapter = RoutedAdapter(
        adapters={"claude": object(), "codex": object()},
        source_tools={"source.hacker_news": fake_hn},
    )

    result = asyncio.run(adapter.call_source(
        "source.hacker_news",
        "飞书",
        "90d",
        research_id=RESEARCH_ID,
        goal_id="goal-1",
        agent_id="data-collection",
        capability=Capability(
            tools=("source.hacker_news",),
            sources=("hacker_news",),
            network="sources_only",
        ),
    ))

    assert result == [{"platform": "hacker_news", "query": "飞书", "window": "90d"}]


def test_source工具桥拒绝调用_capability_未声明的信息源() -> None:
    from app.adapters.capability import Capability
    from app.adapters.routing import RoutedAdapter

    adapter = RoutedAdapter(
        adapters={"claude": object(), "codex": object()},
        source_tools={"source.x": lambda query, window: []},
    )

    try:
        asyncio.run(adapter.call_source(
            "source.x",
            "飞书",
            "7d",
            research_id=RESEARCH_ID,
            goal_id="goal-1",
            agent_id="data-collection",
            capability=Capability(
                tools=("source.hacker_news",),
                sources=("hacker_news",),
                network="sources_only",
            ),
        ))
    except PermissionError as error:
        assert "capability" in str(error)
    else:
        raise AssertionError("未声明的 source.x 不得被调用")


def test_真实_Claude_Codex_适配器消费注册源并注入_source_MCP(
    tmp_path: Path, monkeypatch
) -> None:
    from app.adapters import validation
    from app.adapters.capability import Capability, FileSystemScope
    from app.adapters.claude import build_claude_options, make_permission_callback
    from app.adapters.codex import build_codex_command
    from app.adapters.contracts import EngineTask

    class Allow:
        pass

    class Deny:
        def __init__(self, *, message):
            self.message = message

    class Options:
        def __init__(self, **values):
            self.__dict__.update(values)

    class HookMatcher:
        def __init__(self, **values):
            self.__dict__.update(values)

    FakeSdk = SimpleNamespace(
        PermissionResultAllow=Allow,
        PermissionResultDeny=Deny,
        ClaudeAgentOptions=Options,
        HookMatcher=HookMatcher,
    )

    monkeypatch.setattr(validation, "RUNS_ROOT", tmp_path / "runs")
    output = tmp_path / "runs" / RESEARCH_ID / "goals" / "goal-1" / "evidence.json"
    task = EngineTask(
        body="必须调用 source.hacker_news 采集",
        output_path=output,
        output_format="json",
        research_id=RESEARCH_ID,
        goal_id="goal-1",
        agent_id="data-collection",
        agent_kind="data_collection",
        validators=["file_exists"],
        capability=Capability(
            tools=("source.hacker_news", "fs.write"),
            sources=("hacker_news",),
            fs=FileSystemScope(write=("goals/goal-1/**",)),
            network="sources_only",
        ),
    )

    callback = make_permission_callback(task, [], sdk=FakeSdk)
    options = build_claude_options(task, callback, sdk=FakeSdk)
    command = build_codex_command(task, executable="codex")

    assert "owli_sources" in options.mcp_servers
    assert "mcp__owli_sources__source.hacker_news" in options.allowed_tools
    joined = " ".join(command)
    assert "mcp_servers.owli_sources.command" in joined
    assert "app.adapters.source_mcp" in joined
    assert "hacker_news" in joined


def test_注册表_source_x_统一两参入口缺配置时受控不可用() -> None:
    from app.sources.registry import get_tool

    events: list[dict] = []
    result = get_tool("source.x")("飞书", "7d", on_event=events.append)

    assert result.conclusion["status"] == "unavailable"
    assert result.conclusion["task_continues"] is True
    assert events[0]["type"] == "source_unavailable"


def test_source_MCP_子进程事件经_RoutedAdapter_进入宿主事件流(
    tmp_path: Path, monkeypatch,
) -> None:
    import sys

    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    from app.adapters.capability import Capability
    from app.adapters.contracts import EngineTask
    from app.adapters.routing import RoutedAdapter
    from app.adapters.source_mcp import source_event_path, stdio_server_config

    runtime_env = {
        "OWLI_X_BEARER_TOKEN_ENV": "OWLI_TEST_TOKEN_MUST_NOT_EXIST",
        "OWLI_X_WEEKLY_BUDGET_USD": "0.01",
        "OWLI_X_BALANCE_USD": "10",
        "OWLI_X_BILLING_CYCLE_CAP_USD": "10",
        "OWLI_X_BILLING_CYCLE_SPENT_USD": "0",
        "OWLI_X_PRICE_PER_READ_USD": "0.005",
        "OWLI_X_USAGE_DB_PATH": str(tmp_path / "usage.db"),
    }
    for name, value in runtime_env.items():
        monkeypatch.setenv(name, value)

    class MCPCallingEngine:
        async def run(self, task, ctx, on_event=None, source_adapter=None):
            del ctx, on_event
            assert source_adapter is not None
            config = stdio_server_config(
                ("x",),
                event_path=source_event_path(task),
                research_id=task.research_id,
                goal_id=task.goal_id,
                agent_id=task.agent_id,
            )
            parameters = StdioServerParameters(
                command=config["command"], args=config["args"], env=config["env"]
            )
            async with stdio_client(parameters, errlog=sys.stderr) as streams:
                async with ClientSession(*streams) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    assert [item.name for item in tools.tools] == ["source.x"]
                    result = await session.call_tool(
                        "source.x", {"query": "飞书", "window": "7d"}
                    )
            # 预算卡先发出；测试 token 故意不存在，后续请求受控失败。
            assert result.is_error is True
            return SimpleNamespace(succeeded=True)

    task = EngineTask(
        body="调用 source.x",
        output_path=tmp_path / "runs" / RESEARCH_ID / "goals" / "goal-1" / "x.json",
        output_format="json",
        research_id=RESEARCH_ID,
        goal_id="goal-1",
        agent_id="data-collection-x",
        agent_kind="data_collection",
        validators=["file_exists"],
        capability=Capability(
            tools=("source.x",), sources=("x",), network="sources_only"
        ),
    )
    engine = MCPCallingEngine()
    adapter = RoutedAdapter(
        adapters={"claude": engine, "codex": engine},
    )
    events: list[dict] = []

    assert not source_event_path(task).is_relative_to(task.output_path.parent)

    result = asyncio.run(adapter.run(task, object(), on_event=events.append))

    assert result.succeeded is True
    card = next(event["data"]["card"] for event in events if event["type"] == "card_update")
    assert card["research_id"] == RESEARCH_ID
    assert card["goal_id"] == "goal-1"
    assert card["agent_id"] == "data-collection-x"
    assert card["blocking"] == "none"
    assert "继续执行" in card["body"]
    assert not source_event_path(task).exists()
