#!/usr/bin/env python3
"""M3-e 无人值守三源最小链路；M7 可复用，不依赖外网与凭证。"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.adapters.routing import RoutedAdapter  # noqa: E402
from app.api.main import create_app  # noqa: E402
from app.reliability.audit import classify_and_score  # noqa: E402
from app.report.markdown import render_report  # noqa: E402


NOW = "2026-08-21T00:00:00+00:00"
SOURCE_ORDER = ("hacker_news", "web_search", "product_hunt")
SOURCE_TITLES = {
    "hacker_news": "Hacker News 用户讨论",
    "web_search": "飞书官方能力说明",
    "product_hunt": "Product Hunt 竞品发布",
}
SOURCE_URLS = {
    "hacker_news": "https://news.ycombinator.com/item?id=1",
    "web_search": "https://www.feishu.cn/product/base",
    "product_hunt": "https://www.producthunt.com/posts/notion",
}


def _source_tool(source_id: str):
    def search(query: str, window: str, *, on_event=None):
        del query, window, on_event
        index = SOURCE_ORDER.index(source_id) + 1
        return [{
            "platform": source_id,
            "permalink": SOURCE_URLS[source_id],
            "title": SOURCE_TITLES[source_id],
            "author_name": "具名作者",
            "source_type": "post" if source_id != "web_search" else "article",
            "fetch_method": "official_api" if source_id != "web_search" else "search_index",
            "published_at": "2026-08-20T00:00:00+00:00",
            "fetched_at": NOW,
            "has_body": True,
            "permalink_reachable": True,
            "normalized_score": 0.9,
            "citation_no": index,
            "extra": {"content_kind": "user_opinion"},
        }]

    return search


def _skeleton() -> dict[str, Any]:
    return {
        "goals": [
            {
                "title": "三源采集",
                "objective": "从三个已注册信息源采集飞书竞品优缺点证据。",
                "depends_on": [],
                "deliverable": {
                    "format": "json",
                    "path": "multi-source.json",
                    "description": "三源证据 JSON 数组。",
                },
                "acceptance": ["每条记录含 permalink 与 fetched_at 字段"],
                "agents": [
                    {"name": "HN 数据抓取", "task": "采集 Hacker News 用户讨论"},
                    {"name": "网页搜索数据抓取", "task": "采集官方与评测原文"},
                    {"name": "Product Hunt 数据抓取", "task": "采集 launch 与评论"},
                ],
            },
            {
                "title": "可靠度审计",
                "objective": "闭集判定 authority 与 independence 并计算五维分。",
                "depends_on": ["goal-1"],
                "deliverable": {
                    "format": "json",
                    "path": "audit.json",
                    "description": "三源五维评分数组。",
                },
                "acceptance": ["每条记录的五维分与 rating_notes 均通过正则校验"],
                "agents": [{"name": "可靠度审计", "task": "完成闭集判定与五维评分"}],
            },
            {
                "title": "报告成稿",
                "objective": "形成正文与信息源清单双向一致的 Markdown 报告。",
                "depends_on": ["goal-2"],
                "deliverable": {
                    "format": "markdown",
                    "path": "report.md",
                    "description": "带三源角标和五维清单的报告。",
                },
                "acceptance": ["报告包含结论与信息源两个章节且角标双向一致"],
                "agents": [{"name": "报告撰写", "task": "撰写飞书竞品优缺点报告"}],
            },
        ]
    }


class DemoEngine:
    def __init__(self) -> None:
        self.router: RoutedAdapter | None = None

    async def run(self, task, ctx, on_event=None):
        del ctx
        task.output_path.parent.mkdir(parents=True, exist_ok=True)
        if task.agent_kind == "planning":
            task.output_path.write_text(
                json.dumps(_skeleton(), ensure_ascii=False), encoding="utf-8"
            )
            return SimpleNamespace(succeeded=True)
        if task.agent_kind == "data_collection":
            assert self.router is not None
            tool_name = next(
                name for name in task.capability.tools if name.startswith("source.")
            )
            evidence = await self.router.call_source(
                tool_name,
                "飞书竞品优缺点",
                "90d",
                research_id=task.research_id,
                goal_id=task.goal_id,
                agent_id=task.agent_id,
                capability=task.capability,
                on_event=on_event,
            )
            task.output_path.write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            return SimpleNamespace(succeeded=True)
        if task.agent_kind == "reliability_audit" and "逐条判定来源权威性" in task.body:
            source_items = json.loads(task.body.split("输入证据：", 1)[1].split("\n上一轮", 1)[0])
            labels = [{
                "authority_kind": (
                    "verified_principal"
                    if item["platform"] == "product_hunt"
                    else "named_secondary"
                ),
                "interest_relation": (
                    "disclosed_interest"
                    if item["platform"] == "product_hunt"
                    else "arms_length"
                ),
            } for item in source_items]
            task.output_path.write_text(
                json.dumps(labels, ensure_ascii=False), encoding="utf-8"
            )
            return SimpleNamespace(succeeded=True)
        if task.agent_kind == "reliability_audit":
            assert self.router is not None
            research_root = task.output_path.parents[2]
            evidence = []
            for path in sorted((research_root / "goals" / "goal-1").glob("*.json")):
                value = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(value, list):
                    evidence.extend(value)
            await classify_and_score(
                evidence,
                adapter=self.router,
                output_path=task.output_path,
                research_id=task.research_id,
                goal_id=task.goal_id,
                agent_id=task.agent_id,
                on_event=on_event,
            )
            return SimpleNamespace(succeeded=True)
        if task.agent_kind == "report_writing":
            audit_path = task.output_path.parents[1] / "goal-2" / "audit.json"
            evidence = json.loads(audit_path.read_text(encoding="utf-8"))
            conclusions = [
                f"{item['platform']} 提供了一条可追溯的竞品观察 [S{item['citation_no']:02d}]"
                for item in evidence
            ]
            task.output_path.write_text(
                render_report(conclusions, evidence), encoding="utf-8"
            )
            return SimpleNamespace(succeeded=True)
        raise AssertionError(f"未覆盖的 agent_kind：{task.agent_kind}")


async def _wait_completed(client: httpx.AsyncClient, research_id: str) -> dict[str, Any]:
    for _ in range(300):
        response = await client.get(f"/api/researches/{research_id}")
        state = response.json()["data"]
        if state["status"] in {"completed", "failed"}:
            return state
        await asyncio.sleep(0)
    raise RuntimeError("三源回归未在限定轮次内进入终态")


async def main() -> int:
    with tempfile.TemporaryDirectory(prefix="owli-m3-") as temp:
        root = Path(temp)
        engine = DemoEngine()
        source_tools = {
            f"source.{source_id}": _source_tool(source_id)
            for source_id in SOURCE_ORDER
        }
        adapter = RoutedAdapter(
            clock=time.monotonic,
            adapters={"claude": engine, "codex": engine},
            source_tools=source_tools,
        )
        engine.router = adapter
        application = create_app(
            root / "owli.db",
            ROOT / "app" / "store" / "schema.sql",
            engine_probe=lambda: {},
            adapter_factory=lambda: adapter,
            runs_root=root / "runs",
            auto_confirm=True,
        )
        async with application.router.lifespan_context(application):
            transport = httpx.ASGITransport(app=application)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                created = await client.post(
                    "/api/researches",
                    json={"query": "飞书竞品优缺点"},
                    headers={"X-Request-ID": "m3-multi-source"},
                )
                created.raise_for_status()
                research_id = created.json()["data"]["research_id"]
                state = await _wait_completed(client, research_id)

        report = application.state.store.get_report(research_id)
        report_path = ROOT / str(report["report_path"])
        if not report_path.is_file():
            report_path = root / "runs" / research_id / "goals" / "goal-3" / "report.md"
        text = report_path.read_text(encoding="utf-8")
        audit = json.loads(
            (root / "runs" / research_id / "goals" / "goal-2" / "audit.json").read_text(
                encoding="utf-8"
            )
        )
        counts = Counter(item["platform"] for item in audit)
        marks = sorted(set(re.findall(r"\[S\d{2}\]", text)))
        passed = (
            state["status"] == "completed"
            and all(counts[source_id] >= 1 for source_id in SOURCE_ORDER)
            and marks == ["[S01]", "[S02]", "[S03]"]
            and all("rating_notes=" in line for line in text.splitlines() if line.startswith("- [S"))
        )
        print(
            "M3 三源无人值守："
            f"{'PASS' if passed else 'FAIL'}（status={state['status']}，auto_confirm=1）"
        )
        print("各源证据计数：" + "，".join(f"{item}={counts[item]}" for item in SOURCE_ORDER))
        print("信息源清单角标：" + " ".join(marks))
        return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
