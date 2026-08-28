"""§X-1 货 3：源对账进缺失清单——计划有、入库 0 的源要在成稿里大声说；判据落成稿文本与 events 表。"""

from __future__ import annotations

import json
from pathlib import Path

from tests.test_c1_claims import add_evidence
from tests.test_m3h_finalize import _finalize, _plan, _write


def _prepare(store) -> None:
    add_evidence(store, "r-ledger", "ev-xhs", platform="xhs",
                 permalink="https://xhs.example/a", author="甲")
    for source in ("douyin", "xhs"):
        store.append_event("r-ledger", event_type="source_unavailable", payload={
            "type": "source_unavailable",
            "data": {"source": source, "reason": "tool_unavailable",
                     "closed_reason": "tikhub_request_failed", "provider": "tikhub"},
        }, created_at="2026-08-29T00:00:00Z")


def _plan_with_sources(report_format: str, path: str):
    plan = _plan(report_format=report_format, path=path)
    plan.goals[0].agents[0].capability["sources"] = ["douyin"]
    plan.goals[1].agents[0].capability["sources"] = ["xhs"]
    return plan


def test_markdown成稿缺失清单含未取到的源与曾不可用的源(tmp_path: Path, monkeypatch):
    plan = _plan_with_sources("markdown", "goals/goal-3/report.md")
    report = tmp_path / "runs" / "r-ledger" / "goals" / "goal-3" / "report.md"
    _write(report, "# 结论\n\n- 正文。\n\n# 信息源\n\n- 无。\n")

    _, store, events = _finalize(tmp_path, plan, monkeypatch, prepare=_prepare)

    text = report.read_text(encoding="utf-8")
    assert "## 缺失清单" in text
    assert "未取到的源：douyin（计划 1 章引用，入库 0 条；失败事件 1 次，原因：tikhub_request_failed）" in text
    assert "信息源曾不可用：xhs（失败事件 1 次，原因：tikhub_request_failed；仍入库 1 条）" in text
    summary = [e["data"] for e in events if e.get("type") == "source_yield_summary"]
    assert len(summary) == 1
    assert summary[0]["missing"] == ["douyin"] and summary[0]["degraded"] == ["xhs"]
    assert summary[0]["planned"] == {"douyin": 1, "xhs": 1} and summary[0]["yielded"] == {"xhs": 1}
    assert store.get_report("r-ledger")["status"] == "completed"


def test_json成稿缺失清单结构化带source条目(tmp_path: Path, monkeypatch):
    path = "goals/goal-3/report.json"
    plan = _plan_with_sources("json", path)
    artifact = tmp_path / "runs" / "r-ledger" / path
    _write(artifact, json.dumps({"title": "对账", "sections": []}, ensure_ascii=False))

    _finalize(tmp_path, plan, monkeypatch, prepare=_prepare)

    entries = json.loads(artifact.read_text(encoding="utf-8"))["收尾注释"]["缺失清单"]
    by_chapter = {item["chapter_id"]: item for item in entries}
    assert by_chapter["source/douyin"]["reason"] == "source_missing"
    assert by_chapter["source/douyin"]["goal_id"] == "goal-1"
    assert by_chapter["source/xhs"]["reason"] == "source_degraded"
    assert "未取到的源：douyin" in by_chapter["source/douyin"]["text"]
