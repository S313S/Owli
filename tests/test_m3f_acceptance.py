from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace


def test_验收_三次传输故障让路完成并探活复位(tmp_path):
    import inspect
    from datetime import datetime, timezone

    from app.adapters.capability import Capability
    from app.adapters.contracts import EngineTask
    from app.adapters.events import ItemKind, NormalizedEvent
    from app.adapters.routing import RoutedAdapter
    from app.config import ResilienceConfig
    from app.orchestrator.scheduler import Scheduler
    from app.plan.model import Plan
    from tests.plan_factory import make_plan_dict

    raw_events = []
    scheduler_events = []
    calls = []
    probe_waiting = asyncio.Event()
    release_probe = asyncio.Event()

    async def probe_sleep(seconds):
        assert seconds == 300
        probe_waiting.set()
        await release_probe.wait()

    class Engine:
        def __init__(self, name, failing=False):
            self.name = name
            self.failing = failing
            self.probes = 0

        async def run(self, task, ctx, on_event=None):
            del task, ctx
            calls.append(self.name)
            if self.failing:
                event = NormalizedEvent(
                    engine=self.name, thread_id="t", turn_id="u",
                    item_kind=ItemKind.ERROR, text="stream disconnected",
                    is_error=True, raw={}, route_state="BACKOFF",
                    suspend_new_tasks=True, cause="transport",
                )
                result = on_event(event)
                if inspect.isawaitable(result):
                    await result
                return SimpleNamespace(succeeded=False)
            return SimpleNamespace(succeeded=True)

        async def probe(self):
            self.probes += 1
            return True

    claude = Engine("claude", failing=True)
    codex = Engine("codex")
    adapter = RoutedAdapter(
        adapters={"claude": claude, "codex": codex},
        resilience_config=ResilienceConfig(3, 3, 60, 900, 300),
        probe_sleep=probe_sleep,
        backoff_sleep=lambda seconds: asyncio.sleep(0),
    )
    source = make_plan_dict()
    source["goals"] = source["goals"][:1]
    source["baseline"] = None
    source["goals"][0]["retry_policy"].update(
        max_attempts_per_round=4,
        ask_engine_switch_at=3,
        max_rounds=1,
    )
    plan = Plan.from_dict(source)

    async def run_task(agent, context):
        task = EngineTask(
            body="执行故障注入",
            output_path=tmp_path / "runs" / plan.research_id / "result.md",
            output_format="markdown",
            research_id=plan.research_id,
            goal_id=context.goal_id,
            agent_id=agent.agent_id,
            agent_kind="report",
            validators=["file_exists"],
            capability=Capability(),
        )

        async def on_event(event):
            raw_events.append(event)
            await context.on_event(event)

        return await adapter.run(task, object(), on_event=on_event)

    async def scenario():
        scheduler = Scheduler(
            plan,
            run_task,
            scheduler_events.append,
            lambda: datetime(2026, 8, 21, tzinfo=timezone.utc),
            lambda delay, callback: None,
        )
        await scheduler.start()
        card = next(
            item["data"]["card"]
            for item in scheduler_events
            if item["type"] == "card_update"
            and item["data"]["card"]["card_type"] == "INTERVENE"
        )
        await scheduler.answer_card(card["card_id"], {"choice": "continue"})
        await probe_waiting.wait()
        release_probe.set()
        for _ in range(5):
            await asyncio.sleep(0)
        return scheduler

    scheduler = asyncio.run(scenario())
    outcomes = [event.outcome for event in raw_events if event.outcome]
    print(f"故障注入调用序列={calls}")
    print(f"健康事件序列={outcomes}")
    print(f"最终状态={scheduler.status}")
    assert calls == ["claude", "claude", "claude", "codex"]
    assert outcomes == ["ENGINE_DOWN", "PROBE_OK", "RESET"]
    assert scheduler.status == "completed"
    assert adapter.route_override is None


def test_验收_规划段中断续写落盘并整体过_lint(tmp_path):
    from app.adapters.contracts import PlanningSegmentResult
    from app.adapters.routing import RoutedAdapter
    from app.config import ResilienceConfig
    from app.plan.generate import generate_plan
    from app.plan.lint import lint

    research_id = "r-m3f-segment"
    skeleton = {
        "goals": [
            {"title": "采集", "objective": "形成证据数组。", "depends_on": []},
            {"title": "审计", "objective": "形成可靠度评级。", "depends_on": ["goal-1"]},
            {"title": "报告", "objective": "形成带角标报告。", "depends_on": ["goal-2"]},
        ]
    }
    expansions = {
        "goal-1": {
            "deliverable": {"format": "json", "path": "evidence.json", "description": "证据数组"},
            "acceptance": ["文件存在且至少包含 1 条 permalink 记录"],
            "agents": [{"name": "HN 数据抓取", "task": "采集竞品证据"}],
        },
        "goal-2": {
            "deliverable": {"format": "json", "path": "audit.json", "description": "评级数组"},
            "acceptance": ["文件存在且每条记录包含 5 个评分字段"],
            "agents": [{"name": "可靠度审计", "task": "完成可靠度评级"}],
        },
        "goal-3": {
            "deliverable": {"format": "markdown", "path": "report.md", "description": "最终报告"},
            "acceptance": ["文件存在且包含结论、信息源 2 个章节"],
            "agents": [{"name": "报告撰写", "task": "撰写带双向角标的报告"}],
        },
    }

    class Claude:
        goal_one_calls = 0

        async def generate_plan_segment(self, request, on_text=None):
            value = skeleton if request.segment_name == "skeleton" else expansions[request.segment_name]
            text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            if request.segment_name == "goal-1":
                self.goal_one_calls += 1
                marker = text.index("竞品") + 1
                if self.goal_one_calls == 1:
                    prefix = text[:marker]
                    await on_text(prefix)
                    return PlanningSegmentResult(
                        prefix, False, True, "stream disconnected"
                    )
                assert request.continuation.endswith("竞")
                suffix = text[marker - 1:]
                await on_text(suffix)
                return PlanningSegmentResult(suffix, True)
            await on_text(text)
            return PlanningSegmentResult(text, True)

    class Codex:
        async def generate_plan_segment(self, request, on_text=None):
            raise AssertionError("规划不得进入 Codex")

    class Store:
        runs_root = tmp_path / "runs"
        saved = []
        events = []

        def get_drafting_report(self, query):
            return {"id": research_id, "created_at": "2026-08-21T00:00:00Z", "extra": {}}

        def save_plan_snapshot(self, report_id, *, snapshot, expected_rev):
            self.saved.append((report_id, deepcopy(snapshot), expected_rev))

        async def on_plan_event(self, event):
            self.events.append(event)

    store = Store()
    adapter = RoutedAdapter(adapters={"claude": Claude(), "codex": Codex()})
    plan = asyncio.run(generate_plan(
        "飞书竞品优缺点",
        store,
        adapter,
        ResilienceConfig(3, 3, 60, 900, 300),
        segment_retry_sleep=lambda seconds: asyncio.sleep(0),
    ))
    segment_root = store.runs_root / research_id / "plan-segments"
    names = sorted(path.name for path in segment_root.iterdir())
    print(f"规划分段文件={names}")
    print(f"plan_lint errors={lint(plan)['errors']}")
    assert names == [
        "assembled.json", "goal-1.json", "goal-2.json", "goal-3.json", "skeleton.json"
    ]
    assert lint(plan)["errors"] == []
    assert not list(segment_root.glob("*.partial"))
