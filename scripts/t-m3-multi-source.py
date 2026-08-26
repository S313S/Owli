#!/usr/bin/env python3
"""M3-e 无人值守三源最小链路；M7 可复用，不依赖外网与凭证。"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import sys
import tempfile
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
SOURCE_METRICS = {
    "hacker_news": "points",
    "web_search": None,
    "product_hunt": "votes_count",
}


def _goals_root(path: Path) -> Path:
    """章节化后 output.path 会多出小节层级，按 goals 目录名向上定位更稳。"""

    for parent in path.parents:
        if parent.name == "goals":
            return parent
    raise AssertionError(f"未能在 {path} 的父链中找到 goals 目录")


def _source_tool(source_id: str):
    def search(query: str, window: str, *, on_event=None):
        del query, window, on_event
        index = SOURCE_ORDER.index(source_id) + 1
        return [{
            "platform": source_id,
            "platform_item_id": f"demo-{index}",
            "permalink": SOURCE_URLS[source_id],
            "title": SOURCE_TITLES[source_id],
            "author_name": "具名作者",
            "source_type": "post" if source_id != "web_search" else "article",
            "fetch_method": "official_api" if source_id != "web_search" else "search_index",
            "published_at": "2026-08-20T00:00:00+00:00",
            "fetched_at": NOW,
            "has_body": True,
            "permalink_reachable": True,
            "raw_metrics": (
                {SOURCE_METRICS[source_id]: 1}
                if SOURCE_METRICS[source_id] is not None else {}
            ),
            "normalized_score": None,
            "norm_method": "none",
            "norm_context": {
                "scope": "batch",
                "platform": source_id,
                "metric": SOURCE_METRICS[source_id],
                "n": 1,
                "formula": "none",
                "stats": {},
                "computed_at": NOW,
                "reason": "insufficient_sample",
            },
            "citation_no": index,
            "extra": {"content_kind": "user_opinion"},
        }]

    return search


def _skeleton() -> dict[str, Any]:
    return {
        "market_profile": "global_product",
        "market_profile_justification": "取样源为 Hacker News、Product Hunt 与网页搜索，按全球产品口径。",
        "subjects": ["飞书"],
        "subjects_justification": "三源均围绕主体飞书自身取证。",
        "goals": [
            {
                "title": "三源采集",
                "objective": "从三个已注册信息源采集飞书竞品优缺点证据。",
                "depends_on": [],
                "deliverable": {
                    "format": "json",
                    "shape": "array",
                    "path": "multi-source.json",
                    "description": "三源证据 JSON 数组。",
                },
                "acceptance": ["每条记录含 permalink 与 fetched_at 字段"],
                "agents": [
                    {"name": "HN 数据抓取·飞书", "task": "采集 Hacker News 用户讨论",
                     "output": {"shape": "array"}},
                    {"name": "网页搜索数据抓取·飞书", "task": "采集官方与评测原文",
                     "output": {"shape": "array"}},
                    {"name": "Product Hunt 数据抓取·飞书", "task": "采集 launch 与评论",
                     "output": {"shape": "array"}},
                ],
            },
            {
                "title": "可靠度审计",
                "objective": "闭集判定 authority 与 independence 并计算五维分。",
                "depends_on": ["goal-1"],
                "deliverable": {
                    "format": "json",
                    "shape": "array",
                    "path": "audit.json",
                    "description": "三源五维评分数组。",
                },
                "acceptance": ["每条记录的五维分与 rating_notes 均通过正则校验"],
                "agents": [{"name": "可靠度审计", "task": "完成闭集判定与五维评分",
                            "output": {"shape": "array"}}],
            },
            {
                "title": "报告成稿",
                "objective": "形成正文与信息源清单双向一致的 Markdown 报告。",
                "depends_on": ["goal-2"],
                "deliverable": {
                    "format": "markdown",
                    "shape": "object",
                    "path": "report.md",
                    "description": "带三源角标和五维清单的报告。",
                },
                "acceptance": ["报告包含结论与信息源两个章节且角标双向一致"],
                "agents": [{"name": "报告撰写", "task": "撰写飞书竞品优缺点报告",
                            "output": {"shape": "object"}}],
            },
            {
                "title": "报告标签",
                "objective": "为已生成报告产出受控标签。",
                "depends_on": ["goal-3"],
                "deliverable": {
                    "format": "json",
                    "shape": "array",
                    "path": "tags.json",
                    "description": "3–8 个报告标签。",
                },
                "acceptance": ["报告标签为 3–8 个非空字符串"],
                "agents": [{"name": "标签", "task": "生成受控报告标签",
                            "output": {"shape": "array"}}],
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
            stem = task.output_path.stem
            skeleton = _skeleton()
            if stem == "skeleton":
                payload = {
                    key: skeleton[key]
                    for key in (
                        "market_profile",
                        "market_profile_justification",
                        "subjects",
                        "subjects_justification",
                    )
                }
                payload["goals"] = [
                    {key: goal[key] for key in ("title", "objective", "depends_on")}
                    for goal in skeleton["goals"]
                ]
            elif "-ch-" in stem:
                goal_number = int(stem.split("-ch-", 1)[0].removeprefix("goal-"))
                output_path = json.loads(
                    task.body.split("系统声明 output.path=", 1)[1].split("。", 1)[0]
                )
                payload = {
                    "chapter_type": {
                        1: "collection", 2: "audit", 3: "report", 4: "tagging"
                    }[goal_number],
                    "opening": {
                        "inputs": [], "task": "执行本章", "acceptance": ["产物通过校验"],
                    },
                    "closing": {
                        "output": {"path": output_path}, "entities": ["飞书"],
                        "expected_count": 1, "notes": {},
                    },
                }
            else:
                goal_number = int(stem.removeprefix("goal-"))
                goal = skeleton["goals"][goal_number - 1]
                payload = {
                    key: goal[key] for key in ("deliverable", "acceptance", "agents")
                }
            task.output_path.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
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
            research_root = _goals_root(task.output_path).parent
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
            audit_path = _goals_root(task.output_path) / "goal-2" / "audit.json"
            evidence = json.loads(audit_path.read_text(encoding="utf-8"))
            conclusions = [
                f"{item['platform']} 提供了一条可追溯的竞品观察 [S{item['citation_no']:02d}]"
                for item in evidence
            ]
            task.output_path.write_text(
                render_report(conclusions, evidence), encoding="utf-8"
            )
            return SimpleNamespace(succeeded=True)
        if task.agent_kind == "tagging":
            task.output_path.write_text(
                json.dumps(
                    ["协作软件", "效率工具", "飞书", "自动化优先"],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            return SimpleNamespace(succeeded=True)
        raise AssertionError(f"未覆盖的 agent_kind：{task.agent_kind}")


async def _wait_completed(client: httpx.AsyncClient, research_id: str) -> dict[str, Any]:
    # M5-c 后创建接口立即返回，召回与规划均由后台任务推进；这里按真实墙钟
    # 等终态，不能再假设 300 次零等待事件循环让步足够跑完整条链。
    state: dict[str, Any] = {}
    for _ in range(3000):
        response = await client.get(f"/api/researches/{research_id}")
        state = response.json()["data"]
        if state["status"] in {"completed", "failed"}:
            return state
        await asyncio.sleep(0.01)
    raise RuntimeError(
        "三源回归未在限定轮次内进入终态："
        f"status={state.get('status')} progress={state.get('progress')} "
        f"cards={[item.get('card_type') for item in state.get('cards', [])]}"
    )


async def main() -> int:
    with tempfile.TemporaryDirectory(prefix="owli-m3-") as temp:
        root = Path(temp)
        engine = DemoEngine()
        source_tools = {
            f"source.{source_id}": _source_tool(source_id)
            for source_id in SOURCE_ORDER
        }
        adapter = RoutedAdapter(
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
        before_recall_count = -1
        async with application.router.lifespan_context(application):
            with sqlite3.connect(root / "owli.db") as connection:
                before_recall_count = connection.execute(
                    "SELECT count(*) FROM recall_fts"
                ).fetchone()[0]
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
        stored_rows = application.state.store.read_validation_path(
            "evidence_platform_citations", research_id
        )
        stored_counts = Counter(row["platform"] for row in stored_rows)
        marks = sorted(set(re.findall(r"\[S\d{2}\]", text)))
        with sqlite3.connect(root / "owli.db") as connection:
            tag_rows = connection.execute(
                "SELECT tag, source FROM report_tags "
                "WHERE report_id = ? ORDER BY tag",
                (research_id,),
            ).fetchall()
            recall_rows = connection.execute(
                "SELECT report_id, title, tags, summary_line FROM recall_fts "
                "WHERE report_id = ?",
                (research_id,),
            ).fetchall()
        expected_tags = " ".join(row[0] for row in tag_rows)
        passed = (
            state["status"] == "completed"
            and all(counts[source_id] >= 1 for source_id in SOURCE_ORDER)
            and all(stored_counts[source_id] >= 1 for source_id in SOURCE_ORDER)
            and [row["citation_no"] for row in stored_rows] == [1, 2, 3]
            and marks == ["[S01]", "[S02]", "[S03]"]
            and all("rating_notes=" in line for line in text.splitlines() if line.startswith("- [S"))
            and before_recall_count == 0
            and len(recall_rows) == 1
            and tag_rows
            and all(source == "agent" for _, source in tag_rows)
            and recall_rows[0] == (
                research_id,
                report["title"],
                expected_tags,
                report["summary_line"],
            )
        )
        print(
            "M3 三源无人值守："
            f"{'PASS' if passed else 'FAIL'}（status={state['status']}，auto_confirm=1）"
        )
        print("各源证据计数：" + "，".join(f"{item}={counts[item]}" for item in SOURCE_ORDER))
        print(
            "evidence 表平台计数："
            + "，".join(f"{item}={stored_counts[item]}" for item in SOURCE_ORDER)
        )
        print("信息源清单角标：" + " ".join(marks))
        print(
            f"recall_fts 行数：改前={before_recall_count}，"
            f"改后={len(recall_rows)}"
        )
        print("report_tags：" + json.dumps(tag_rows, ensure_ascii=False))
        print("recall_fts 文档：" + json.dumps(recall_rows, ensure_ascii=False))
        return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
