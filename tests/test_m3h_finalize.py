"""收尾（整卷 finalize）回归：缺陷 D —— 报告章声明 json 时兜底到不存在的 report.md。"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from tests.plan_factory import make_plan_dict
from tests.test_m3h_ledger import _store


def _plan(*, report_format: str | None, path: str = "goals/goal-3/report.md"):
    """report_format=None 表示计划里根本没有报告章。"""
    from app.plan.model import Plan

    source = make_plan_dict()
    source["research_id"] = "r-ledger"
    source["baseline"] = None
    if report_format is not None:
        agent = source["goals"][2]["agents"][0]
        agent["agent_id"] = "report-writing"
        agent["capability"]["profile"] = "report-writer"
        agent["capability"]["fs"]["write"] = ["goals/goal-3/**"]
        agent["output"] = {
            "format": report_format,
            "shape": "array" if report_format == "json" else "object",
            "path": path,
            "validators": ["file_exists"],
        }
        agent["chapter"]["chapter_type"] = "report"
        agent["chapter"]["closing"]["output"] = {"path": path}
    return Plan.from_dict(source)


def _finalize(tmp_path: Path, plan, monkeypatch, *, goal_statuses=None, prepare=None):
    """跑一次收尾，返回 (coordinator, store, 事件列表)。"""
    from app.orchestrator import runtime as runtime_module
    from app.orchestrator.runtime import RuntimeCoordinator

    store = _store(tmp_path)
    if prepare is not None:
        prepare(store)
    events: list[dict] = []

    async def publish(research_id, payload):
        events.append(payload)

    monkeypatch.setattr(runtime_module, "load_plan", lambda store_, rid: plan)
    coordinator = RuntimeCoordinator(
        store=store,
        event_buffer=SimpleNamespace(publish=publish),
        researches={},
        cards={},
        runs_root=tmp_path / "runs",
        auto_confirm=True,
        routing_utc_clock=lambda: datetime(2026, 8, 22, tzinfo=timezone.utc),
    )
    coordinator.researches[plan.research_id] = coordinator._state_from_plan(plan)
    coordinator._schedulers[plan.research_id] = SimpleNamespace(
        status="completed",
        goal_statuses=goal_statuses or {"goal-1": "done", "goal-2": "done", "goal-3": "done"},
    )
    asyncio.run(coordinator._finalize_if_terminal(plan.research_id))
    return coordinator, store, events


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_报告章声明json时收尾取真实产物而不是兜底到report_md(tmp_path, monkeypatch):
    path = "goals/goal-3/comparative-analysis.json"
    plan = _plan(report_format="json", path=path)
    artifact = tmp_path / "runs" / "r-ledger" / path
    _write(artifact, json.dumps({"title": "竞品分析", "sections": []}, ensure_ascii=False))

    coordinator, store, events = _finalize(tmp_path, plan, monkeypatch)

    assert coordinator._report_path(plan) == artifact
    document = json.loads(artifact.read_text(encoding="utf-8"))
    assert document["title"] == "竞品分析"
    assert document["收尾注释"]["决策天平"][0]["q_id"] == "q-1"
    assert "本次运行未生成完整结论" not in artifact.read_text(encoding="utf-8")
    report = store.get_report("r-ledger")
    assert report["status"] == "completed"
    assert coordinator.researches["r-ledger"]["status"] == "completed"
    verdicts = [
        event["data"] for event in events if event.get("type") == "report_validation"
    ]
    assert verdicts and verdicts[-1]["validators"] == [
        "file_exists", "chapter_missing_items_reported",
    ]


def test_收尾校验不过只发告警_研究仍然completed(tmp_path, monkeypatch):
    plan = _plan(report_format="markdown")
    artifact = tmp_path / "runs" / "r-ledger" / "goals" / "goal-3" / "report.md"
    _write(artifact, "# 结论\n\n- 没有任何角标。\n\n# 信息源\n\n- 无。\n")

    coordinator, store, events = _finalize(tmp_path, plan, monkeypatch)

    validations = [
        event["data"] for event in events if event.get("type") == "report_validation"
    ]
    warnings = [
        event["data"] for event in events if event.get("type") == "report_warning"
    ]
    assert validations[-1]["verdict"] == "fail"
    assert warnings and warnings[-1]["reason"] == "report_validation_failed"
    assert store.get_report("r-ledger")["status"] == "completed"
    assert coordinator.researches["r-ledger"]["status"] == "completed"


def test_声明了报告章却没有产物才判failed(tmp_path, monkeypatch):
    plan = _plan(report_format="markdown")

    coordinator, store, events = _finalize(tmp_path, plan, monkeypatch)

    artifact = tmp_path / "runs" / "r-ledger" / "goals" / "goal-3" / "report.md"
    assert "本次运行未生成完整结论" in artifact.read_text(encoding="utf-8")
    assert store.get_report("r-ledger")["status"] == "failed"
    assert coordinator.researches["r-ledger"]["status"] == "failed"
    assert store.get_report("r-ledger")["summary_line"] == "报告未生成"


def test_计划不含报告章也能completed_且收尾正文汇总已完成章(tmp_path, monkeypatch):
    plan = _plan(report_format=None)

    def prepare(store):
        store.ensure_chapters(
            "r-ledger", [{"goal_id": "goal-1", "chapter_id": "ch-1"}],
            updated_at="2026-08-22T00:00:00Z",
        )
        store.start_chapter(
            "r-ledger", "goal-1", "ch-1", engine="codex",
            updated_at="2026-08-22T00:01:00Z",
        )
        store.finish_chapter(
            "r-ledger", "goal-1", "ch-1", status="done", reason=None,
            actual_output_path="goals/goal-1/agent-1.md", actual_count=1,
            updated_at="2026-08-22T00:02:00Z",
        )

    coordinator, store, events = _finalize(tmp_path, plan, monkeypatch, prepare=prepare)

    text = (tmp_path / "runs" / "r-ledger" / "goals" / "goal-3" / "report.md").read_text(
        encoding="utf-8",
    )
    assert "本次运行未生成完整结论" not in text
    assert "goal-1/ch-1：goals/goal-1/agent-1.md" in text
    assert store.get_report("r-ledger")["status"] == "completed"
    assert coordinator.researches["r-ledger"]["status"] == "completed"


def test_节化拼装按声明格式落盘_json报告章不得再写markdown(tmp_path):
    from app.orchestrator.sectioning import _assemble

    plan = _plan(report_format="json", path="goals/goal-3/comparative-analysis.json")
    agent = plan.goals[2].agents[0]
    output_path = tmp_path / "comparative-analysis.json"
    section_root = tmp_path / "sections"
    section_root.mkdir()
    (section_root / "sec-1.md").write_text("## 结论\n\n- 一节正文 [S01]\n", encoding="utf-8")

    _assemble(
        plan=plan,
        agent=agent,
        output_path=output_path,
        output_format="json",
        section_root=section_root,
        sections=[{
            "section_id": "ch-1/sec-1", "filename": "sec-1.md",
            "title": "阶段 1 证据产物", "goal_id": "goal-1",
        }, {
            "section_id": "ch-1/sec-2", "filename": "sec-2.md",
            "title": "阶段 2 证据产物", "goal_id": "goal-2",
        }],
        rows=[{
            "goal_id": "goal-2", "chapter_id": "ch-1/sec-2", "status": "missing",
            "reason": "retry_exhausted",
        }],
    )

    document = json.loads(output_path.read_text(encoding="utf-8"))
    assert [item["section_id"] for item in document["sections"]] == [
        "ch-1/sec-1", "ch-1/sec-2",
    ]
    assert "一节正文" in document["sections"][0]["markdown"]
    assert document["缺失清单"] == [{
        "goal_id": "goal-2", "chapter_id": "ch-1/sec-2", "reason": "retry_exhausted",
        "text": "此处缺失：goal-2/ch-1/sec-2；原因：retry_exhausted",
    }]


def test_markdown报告章拼装仍是markdown(tmp_path):
    from app.orchestrator.sectioning import _assemble

    plan = _plan(report_format="markdown")
    agent = plan.goals[2].agents[0]
    output_path = tmp_path / "report.md"
    section_root = tmp_path / "sections"
    section_root.mkdir()
    (section_root / "sec-1.md").write_text("## 结论\n\n- 一节正文 [S01]\n", encoding="utf-8")

    _assemble(
        plan=plan,
        agent=agent,
        output_path=output_path,
        output_format="markdown",
        section_root=section_root,
        sections=[{
            "section_id": "ch-1/sec-1", "filename": "sec-1.md",
            "title": "阶段 1 证据产物", "goal_id": "goal-1",
        }],
        rows=[],
    )

    text = output_path.read_text(encoding="utf-8")
    assert text.startswith(f"# {plan.title}")
    assert "## 缺失清单" in text and "- 无。" in text
