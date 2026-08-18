import asyncio
import json
import sqlite3
import sys
from collections import Counter, defaultdict, deque
from functools import wraps
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "app" / "store" / "schema.sql"
sys.path.insert(0, str(ROOT))


def async_test(function):
    """让异步用例不依赖 pytest-asyncio 插件。"""
    @wraps(function)
    def wrapper(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return wrapper


def _init_store(tmp_path):
    from app.store.dao import Store

    database_path = tmp_path / "owli.db"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    return Store(database_path), database_path


def _evidence(keyword: str, number: int) -> dict:
    item_id = f"{keyword}-{number}"
    return {
        "platform": "hacker_news",
        "permalink": f"https://news.ycombinator.com/item?id={item_id}",
        "fetched_at": "2026-08-18T10:00:00+00:00",
        "raw_metrics": {"points": 100 + number, "num_comments": number},
        "source_keyword": keyword,
        "source_type": "post",
        "platform_item_id": item_id,
        "title": f"{keyword} discussion {number}",
        "content_excerpt": f"evidence {number}",
        "author_name": "tester",
        "fetch_method": "official_api",
        "published_at": "2026-08-17T10:00:00+00:00",
        "score_authority": 1,
        "score_freshness": 1,
        "score_crossref": 0,
        "score_completeness": 2,
        "score_independence": 2,
        "rated_by": "baseline",
    }


class FakeSource:
    def __init__(self, items_per_query=4):
        self.calls = []
        self.items_per_query = items_per_query

    def __call__(self, query: str, window: str):
        self.calls.append((query, window))
        return [_evidence(query, number) for number in range(self.items_per_query)]


class FakeAdapter:
    def __init__(self, outcomes=None, raw_error=None):
        self.outcomes = defaultdict(lambda: deque(["pass"]))
        for agent_id, values in (outcomes or {}).items():
            self.outcomes[agent_id] = deque(values)
        self.raw_error = raw_error
        self.calls = defaultdict(int)
        self.tasks = []

    async def run(self, task, ctx, on_event=None):
        from app.adapters.claude import ClaudeEvent, ClaudeRunResult, OwliResult
        from app.adapters.validation import Result, ValidationReport, Verdict, validate

        self.calls[task.agent_id] += 1
        self.tasks.append(task)
        outcome = self.outcomes[task.agent_id].popleft()
        if on_event is not None:
            await on_event(ClaudeEvent("thinking", "正在执行", {"agent": task.agent_id}))

        if outcome == "unavailable":
            unavailable = Result(
                Verdict.UNAVAILABLE,
                "fake_dependency",
                "依赖缺失",
                [],
                {"raw": self.raw_error},
            )
            return ClaudeRunResult(
                None,
                "依赖缺失",
                ValidationReport(Verdict.UNAVAILABLE, [unavailable]),
                [ClaudeEvent("error", "依赖缺失", self.raw_error)],
                [],
                "依赖缺失",
            )

        if outcome == "pass":
            task.output_path.parent.mkdir(parents=True, exist_ok=True)
            if task.agent_id == "keyword-extractor":
                task.output_path.write_text(
                    json.dumps(["Feishu", "Lark", "team collaboration"]),
                    encoding="utf-8",
                )
            else:
                evidence = json.loads(
                    (task.output_path.parent / "evidence.json").read_text(encoding="utf-8")
                )[:3]
                conclusions = "\n".join(
                    f"- 结论 {index} [{item['citation_id']}]"
                    for index, item in enumerate(evidence, 1)
                )
                sources = "\n".join(
                    f"- [{item['citation_id']}] [{item['title']}]({item['permalink']})"
                    for item in evidence
                )
                task.output_path.write_text(
                    f"# 结论\n{conclusions}\n\n# 信息源\n{sources}\n",
                    encoding="utf-8",
                )
        elif outcome == "invalid_keywords":
            task.output_path.parent.mkdir(parents=True, exist_ok=True)
            task.output_path.write_text(
                json.dumps([{"query": "Lark"}, {"query": "Slack"}, {"query": "Teams"}]),
                encoding="utf-8",
            )
        elif outcome == "too_many_keywords":
            task.output_path.parent.mkdir(parents=True, exist_ok=True)
            task.output_path.write_text(
                json.dumps(["Lark", "Slack", "Teams", "Notion", "Zoom", "Asana", "Monday"]),
                encoding="utf-8",
            )

        report = validate(ctx, task.validators)
        conclusion = OwliResult(
            "done",
            str(task.output_path),
            "测试产物",
            [],
            [],
            [],
        )
        return ClaudeRunResult(conclusion, None, report, [], [])


async def _run_case(
    tmp_path, monkeypatch, *, outcomes=None, raw_error=None, items_per_query=4
):
    from app.adapters import validation
    from app.api.events import ResearchEventBuffer
    from app.orchestrator.mini import MiniOrchestrator, build_initial_state

    runs_root = tmp_path / "runs"
    monkeypatch.setattr(validation, "RUNS_ROOT", runs_root)
    store, database_path = _init_store(tmp_path)
    event_buffer = ResearchEventBuffer()
    adapter = FakeAdapter(outcomes, raw_error)
    source = FakeSource(items_per_query)
    state = build_initial_state("r-test", "飞书竞品优缺点")
    orchestrator = MiniOrchestrator(
        research_id="r-test",
        query="飞书竞品优缺点",
        store=store,
        event_buffer=event_buffer,
        state=state,
        adapter=adapter,
        source_search=source,
        runs_root=runs_root,
    )
    await orchestrator.run()
    replay = await event_buffer.replay_after("r-test", 0)
    return state, adapter, source, database_path, replay.events, runs_root


@async_test
async def test_正常通路_串起产物事件入库和报告(tmp_path, monkeypatch):
    from app.adapters import validation

    state, adapter, source, database_path, events, runs_root = await _run_case(
        tmp_path, monkeypatch
    )

    assert state["status"] == "completed"
    assert state["progress"] == {
        "done": 4,
        "total": 4,
        "summary": "报告已生成并通过全部校验",
    }
    assert adapter.calls == {"keyword-extractor": 1, "report-writer": 1}
    assert "每项只能是 1–2 个英文单词" in adapter.tasks[0].body
    assert "Lark、Slack、Teams、Notion" in adapter.tasks[0].body
    assert "summary 固定填写『英文检索词已写入』" in adapter.tasks[0].body
    assert "M0 本步的完成标准" in adapter.tasks[1].body
    assert "status 必须填写 done" in adapter.tasks[1].body
    assert "summary 固定填写『报告已写入并完成自检』" in adapter.tasks[1].body
    assert sorted(source.calls) == sorted([
        ("Feishu", "90d"),
        ("Lark", "90d"),
        ("team collaboration", "90d"),
    ])
    goal_root = runs_root / "r-test" / "goals" / "goal-1"
    assert json.loads((goal_root / "keywords.json").read_text()) == [
        "Feishu", "Lark", "team collaboration"
    ]
    evidence = json.loads((goal_root / "evidence.json").read_text())
    assert len(evidence) == 12
    assert evidence[0]["citation_id"] == "S01"
    report_path = goal_root / "report.md"
    ctx = validation.Ctx(
        output_path=report_path,
        output_format="markdown",
        research_id="r-test",
        goal_id="goal-1",
        agent_id="report-writer",
        read_text=lambda: report_path.read_text(encoding="utf-8"),
        read_json=lambda: json.loads(report_path.read_text(encoding="utf-8")),
        store=None,
        source_domains=frozenset({"news.ycombinator.com"}),
    )
    citation_report = validation.validate(
        ctx, ["citation_marks_resolvable", "no_orphan_citation"]
    )
    assert citation_report.verdict is validation.Verdict.PASS
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT count(*) FROM evidence WHERE report_id = 'r-test'"
        ).fetchone()[0] == 12
        report = connection.execute(
            "SELECT status, report_path FROM reports WHERE id = 'r-test'"
        ).fetchone()
    assert report == ("completed", "runs/r-test/goals/goal-1/report.md")
    assert {event.payload["type"] for event in events} >= {
        "research_update", "progress", "agent_update", "artifact"
    }


@async_test
async def test_FAIL_重试两次后成功(tmp_path, monkeypatch):
    state, adapter, _, _, events, _ = await _run_case(
        tmp_path,
        monkeypatch,
        outcomes={"keyword-extractor": ["fail", "fail", "pass"]},
    )

    assert state["status"] == "completed"
    assert adapter.calls["keyword-extractor"] == 3
    retries = [
        event for event in events
        if event.payload["type"] == "agent_update"
        and event.payload.get("data", {}).get("status") == "retrying"
    ]
    assert len(retries) == 2


@async_test
async def test_FAIL_三次后终止(tmp_path, monkeypatch):
    state, adapter, _, _, events, _ = await _run_case(
        tmp_path,
        monkeypatch,
        outcomes={"keyword-extractor": ["fail", "fail", "fail"]},
    )

    assert state["status"] == "failed"
    assert adapter.calls["keyword-extractor"] == 3
    assert not adapter.calls["report-writer"]
    assert events[-1].payload["type"] == "research_update"


@async_test
async def test_UNAVAILABLE_不重试且原始载荷完整(tmp_path, monkeypatch):
    raw = {"subtype": "success", "api_error_status": 429, "nested": ["原始载荷"]}
    state, adapter, _, _, events, _ = await _run_case(
        tmp_path,
        monkeypatch,
        outcomes={"keyword-extractor": ["unavailable", "pass", "pass"]},
        raw_error=raw,
    )

    assert state["status"] == "unavailable"
    assert state["status_label"] == "引擎不可用"
    assert adapter.calls["keyword-extractor"] == 1
    error = next(event for event in events if event.payload["type"] == "error")
    assert error.payload["raw"] == raw


@async_test
async def test_每步都有事件且序号单调无缺口(tmp_path, monkeypatch):
    _, _, _, _, events, _ = await _run_case(tmp_path, monkeypatch)

    sequences = [event.sequence for event in events]
    assert sequences == list(range(1, len(events) + 1))
    updates = [
        event.payload["data"]
        for event in events
        if event.payload["type"] == "agent_update"
    ]
    for agent_id in ("keyword-extractor", "hn-collector", "report-writer"):
        statuses = [item["status"] for item in updates if item["agent_id"] == agent_id]
        assert statuses[0] == "running"
        assert statuses[-1] == "done"
    validation_updates = [
        item for item in updates
        if item["agent_id"] == "report-writer" and item.get("phase") == "validation"
    ]
    assert [item["status"] for item in validation_updates] == ["running", "done"]


@async_test
async def test_关键词必须是_3_到_6_个字符串_否则按_FAIL_重试(tmp_path, monkeypatch):
    state, adapter, source, _, events, _ = await _run_case(
        tmp_path,
        monkeypatch,
        outcomes={
            "keyword-extractor": ["invalid_keywords", "too_many_keywords", "pass"]
        },
    )

    assert state["status"] == "completed"
    assert adapter.calls["keyword-extractor"] == 3
    assert len(source.calls) == 3
    retries = [
        event for event in events
        if event.payload["type"] == "agent_update"
        and event.payload.get("data", {}).get("status") == "retrying"
    ]
    assert len(retries) == 2


@async_test
async def test_证据少于_10_条按_FAIL_重试三次后终止(tmp_path, monkeypatch):
    state, _, source, database_path, events, _ = await _run_case(
        tmp_path, monkeypatch, items_per_query=1
    )

    assert state["status"] == "failed"
    assert len(source.calls) == 9
    retries = [
        event for event in events
        if event.payload["type"] == "agent_update"
        and event.payload.get("data", {}).get("status") == "retrying"
    ]
    assert len(retries) == 2
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT count(*) FROM evidence WHERE report_id = 'r-test'"
        ).fetchone()[0] == 0


@async_test
async def test_证据角标不超过_S99_注册表上限(tmp_path, monkeypatch):
    _, _, _, database_path, _, runs_root = await _run_case(
        tmp_path, monkeypatch, items_per_query=40
    )

    evidence = json.loads(
        (runs_root / "r-test" / "goals" / "goal-1" / "evidence.json").read_text()
    )
    assert len(evidence) == 99
    assert evidence[-1]["citation_id"] == "S99"
    keyword_counts = Counter(item["source_keyword"] for item in evidence)
    assert max(keyword_counts.values()) - min(keyword_counts.values()) <= 1
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT count(*) FROM evidence WHERE report_id = 'r-test'"
        ).fetchone()[0] == 99


@async_test
async def test_POST_触发后台编排且快照不是演示数据(tmp_path):
    from app.api.events import ResearchEventBuffer
    from app.api.main import ResearchRequest, create_app

    database_path = tmp_path / "owli.db"
    event_buffer = ResearchEventBuffer()
    started = asyncio.Event()

    class BackgroundRunner:
        async def run(self):
            started.set()

    def factory(**kwargs):
        assert kwargs["query"] == "飞书竞品优缺点"
        return BackgroundRunner()

    app = create_app(
        database_path,
        SCHEMA_PATH,
        event_buffer=event_buffer,
        orchestrator_factory=factory,
    )
    async with app.router.lifespan_context(app):
        route = next(
            route for route in app.routes
            if getattr(route, "path", None) == "/api/researches"
            and "POST" in getattr(route, "methods", set())
        )
        response = await route.endpoint(ResearchRequest(query="飞书竞品优缺点"))
        await asyncio.wait_for(started.wait(), timeout=1)

    research_id = response["data"]["research_id"]
    snapshot = app.state.researches[research_id]
    assert [goal["id"] for goal in snapshot["goals"]] == ["goal-1"]
    assert [agent["id"] for agent in snapshot["goals"][0]["agents"]] == [
        "keyword-extractor", "hn-collector", "report-writer"
    ]


@async_test
async def test_POST_后台未捕获异常转为引擎不可用且保留原始载荷(tmp_path):
    from app.api.events import ResearchEventBuffer
    from app.api.main import ResearchRequest, create_app

    event_buffer = ResearchEventBuffer()
    entered = asyncio.Event()

    class BrokenRunner:
        def __init__(self, **kwargs):
            self.store = kwargs["store"]
            self.research_id = kwargs["research_id"]
            self.query = kwargs["query"]

        async def run(self):
            self.store.create_report(
                id=self.research_id,
                title=self.query,
                research_question=self.query,
                created_at="2026-08-18T10:00:00+00:00",
                status="running",
            )
            entered.set()
            raise RuntimeError("背景线程崩溃")

    app = create_app(
        tmp_path / "owli.db",
        SCHEMA_PATH,
        event_buffer=event_buffer,
        orchestrator_factory=lambda **kwargs: BrokenRunner(**kwargs),
    )
    async with app.router.lifespan_context(app):
        route = next(
            route for route in app.routes
            if getattr(route, "path", None) == "/api/researches"
            and "POST" in getattr(route, "methods", set())
        )
        response = await route.endpoint(ResearchRequest(query="飞书竞品优缺点"))
        await asyncio.wait_for(entered.wait(), timeout=1)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    research_id = response["data"]["research_id"]
    state = app.state.researches[research_id]
    assert state["status"] == "unavailable"
    assert state["status_label"] == "引擎不可用"
    replay = await event_buffer.replay_after(research_id, 0)
    error = next(event for event in replay.events if event.payload["type"] == "error")
    assert error.payload["raw"] == {
        "exception": "RuntimeError",
        "message": "背景线程崩溃",
    }
    assert app.state.store.get_report(research_id)["status"] == "failed"


@async_test
async def test_POST_后台异常时读库也失败_仍能进入终态(tmp_path, monkeypatch):
    from app.api.events import ResearchEventBuffer
    from app.api.main import ResearchRequest, create_app

    event_buffer = ResearchEventBuffer()
    entered = asyncio.Event()

    class BrokenRunner:
        async def run(self):
            entered.set()
            raise RuntimeError("背景线程崩溃")

    app = create_app(
        tmp_path / "owli.db",
        SCHEMA_PATH,
        event_buffer=event_buffer,
        orchestrator_factory=lambda **kwargs: BrokenRunner(),
    )
    monkeypatch.setattr(
        app.state.store,
        "get_report",
        lambda research_id: (_ for _ in ()).throw(OSError("数据库离线")),
    )
    async with app.router.lifespan_context(app):
        route = next(
            route for route in app.routes
            if getattr(route, "path", None) == "/api/researches"
            and "POST" in getattr(route, "methods", set())
        )
        response = await route.endpoint(ResearchRequest(query="飞书竞品优缺点"))
        await asyncio.wait_for(entered.wait(), timeout=1)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    research_id = response["data"]["research_id"]
    assert app.state.researches[research_id]["status"] == "unavailable"
    replay = await event_buffer.replay_after(research_id, 0)
    error = next(event for event in replay.events if event.payload["type"] == "error")
    assert error.payload["raw"]["original"] == {
        "exception": "RuntimeError",
        "message": "背景线程崩溃",
    }
    assert error.payload["raw"]["storage_finalize_error"] == {
        "exception": "OSError",
        "message": "数据库离线",
    }
