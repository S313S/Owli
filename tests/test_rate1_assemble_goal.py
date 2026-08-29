"""§RATE-1 货 1 / D-026：报告章组装必须按 (goal_id, chapter_id) 找账本行。

三个 goal 的报告章同名（都叫 `ch-4/sec-N`）。只按 chapter_id 找行时，goal-3 明明
`done`，却先命中 goal-1 那行 `missing/timeout`——写占位文、整节 claims 丢光，
这正是交叉验证维度恒空的直接原因（X-1 整跑 `r-aa886bbee722` 实证）。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from app.orchestrator.sectioning import _assemble

_CLAIMS = [{"claim_id": "c-01", "text": "goal-3 的断言", "refs": [
    {"url": "https://example.com/a", "quote": "原文"},
]}]


def _rows() -> list[dict[str, object]]:
    """账本行按撰写章自己的 goal 记；同名节在三个 goal 下各一行（X-1 底料形状）。"""
    return [
        {"goal_id": "goal-1", "chapter_id": "ch-4/sec-1", "status": "missing", "reason": "timeout"},
        {"goal_id": "goal-2", "chapter_id": "ch-4/sec-1", "status": "missing", "reason": "timeout"},
        {"goal_id": "goal-3", "chapter_id": "ch-4/sec-1", "status": "done", "reason": None},
    ]


def _run(tmp_path: Path, goal_id: str) -> dict:
    section_root = tmp_path / goal_id / "sections"
    section_root.mkdir(parents=True)
    (section_root / "sec-1.md").write_text(json.dumps({
        "markdown": "## 结论\n\n- goal-3 真正文 [S01]\n",
        "claims": _CLAIMS,
    }, ensure_ascii=False), encoding="utf-8")
    output = tmp_path / f"{goal_id}.json"
    agent = SimpleNamespace(
        agent_id="report-writing", chapter={"chapter_id": "ch-4"},
        output={"shape": "object"},
    )
    _assemble(
        plan=SimpleNamespace(title="夹具"), agent=agent, goal_id=goal_id,
        output_path=output, output_format="json", section_root=section_root,
        sections=[{
            "section_id": "ch-4/sec-1", "filename": "sec-1.md",
            "title": "范围一", "goal_id": "goal-1",
        }],
        rows=_rows(),
    )
    return json.loads(output.read_text(encoding="utf-8"))


def test_同名节跨goal不再互相踩_goal3成稿带claims且正文非占位(tmp_path: Path) -> None:
    document = _run(tmp_path, "goal-3")
    assert document.get("claims") == _CLAIMS, "命中 goal-1 的 missing 行会把整节 claims 丢光"
    assert "goal-3 真正文" in document["sections"][0]["markdown"]
    assert "此处缺失" not in document["sections"][0]["markdown"]


def test_本goal自己判missing时仍写占位_不被别的goal的done救回(tmp_path: Path) -> None:
    document = _run(tmp_path, "goal-1")
    assert "claims" not in document
    assert "此处缺失" in document["sections"][0]["markdown"]
    assert "原因：timeout" in document["sections"][0]["markdown"]
