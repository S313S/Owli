from __future__ import annotations

import asyncio
import json
from copy import deepcopy


def test_验收_规划段中断续写落盘并整体过_lint(tmp_path):
    from app.adapters.contracts import PlanningSegmentResult
    from app.adapters.routing import RoutedAdapter
    from app.config import ResilienceConfig
    from app.plan.generate import generate_plan
    from app.plan.lint import lint

    research_id = "r-m3f-segment"
    skeleton = {
        "market_profile": "global_product",
        "market_profile_justification": "产品面向全球市场。",
        "goals": [
            {"title": "采集", "objective": "形成证据数组。", "depends_on": []},
            {"title": "审计", "objective": "形成可靠度评级。", "depends_on": ["goal-1"]},
            {"title": "报告", "objective": "形成带角标报告。", "depends_on": ["goal-2"]},
        ]
    }
    expansions = {
        "goal-1": {
            "deliverable": {"format": "json", "shape": "array", "path": "evidence.json", "description": "证据数组"},
            "acceptance": ["文件存在且至少包含 1 条 permalink 记录"],
            "agents": [{"name": "HN 数据抓取", "task": "采集竞品证据", "output": {"shape": "array"}}],
        },
        "goal-2": {
            "deliverable": {"format": "json", "shape": "array", "path": "audit.json", "description": "评级数组"},
            "acceptance": ["文件存在且每条记录包含 5 个评分字段"],
            "agents": [{"name": "可靠度审计", "task": "完成可靠度评级", "output": {"shape": "array"}}],
        },
        "goal-3": {
            "deliverable": {"format": "markdown", "shape": "object", "path": "report.md", "description": "最终报告"},
            "acceptance": ["文件存在且包含结论、信息源 2 个章节"],
            "agents": [{"name": "报告撰写", "task": "撰写带双向角标的报告", "output": {"shape": "object"}}],
        },
    }

    class Claude:
        goal_one_calls = 0

        async def generate_plan_segment(self, request, on_text=None):
            if "-ch-" in request.segment_name:
                goal_name, chapter_text = request.segment_name.split("-ch-", 1)
                raw_agent = expansions[goal_name]["agents"][int(chapter_text) - 1]
                output_tail = request.prompt.partition("系统声明 output.path=")[2]
                output_path = json.JSONDecoder().raw_decode(output_tail)[0]
                type_by_name = {
                    "HN 数据抓取": "collection",
                    "可靠度审计": "audit",
                    "报告撰写": "report",
                }
                value = {
                    "chapter_type": type_by_name[raw_agent["name"]],
                    "opening": {
                        "inputs": [], "task": raw_agent["task"],
                        "acceptance": ["产物按声明路径落盘"],
                    },
                    "closing": {
                        "output": {"path": output_path}, "entities": ["飞书"],
                        "expected_count": 1, "notes": {},
                    },
                }
            else:
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
    adapter = RoutedAdapter(
        adapters={"claude": Claude(), "codex": Codex()},
    )
    plan = asyncio.run(generate_plan(
        "飞书竞品优缺点",
        store,
        adapter,
        ResilienceConfig(3, 60, 900),
        segment_retry_sleep=lambda seconds: asyncio.sleep(0),
    ))
    segment_root = store.runs_root / research_id / "plan-segments"
    names = sorted(path.name for path in segment_root.iterdir())
    print(f"规划分段文件={names}")
    print(f"plan_lint errors={lint(plan)['errors']}")
    assert names == [
        "assembled.json", "goal-1-ch-1.json", "goal-1.json",
        "goal-2-ch-1.json", "goal-2.json", "goal-3-ch-1.json",
        "goal-3.json", "skeleton.json"
    ]
    assert lint(plan)["errors"] == []
    assert not list(segment_root.glob("*.partial"))
