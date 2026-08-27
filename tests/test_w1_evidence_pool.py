from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from app.adapters import validation
from app.adapters.capability import Capability, FileSystemScope
from app.adapters.contracts import EngineRunResult, EngineTask, OwliResult
from app.orchestrator.sectioning import run_sectioned_task
from tests.test_m3h_ledger import _store


POOL_MARKER = "本节可引用证据池 JSON（唯一引用源）：\n"


def _pool_from_body(body: str) -> dict:
    raw = body.split(POOL_MARKER, 1)[1]
    decoder = json.JSONDecoder()
    pool, _ = decoder.raw_decode(raw)
    return pool


def _add_evidence(
    store,
    *,
    evidence_id: str,
    goal_id: str,
    permalink: str,
    excerpt: str = "可复核正文",
    scored: bool = False,
) -> None:
    scores = {
        "score_authority": 2,
        "score_freshness": 1,
        "score_crossref": 2,
        "score_completeness": 1,
        "score_independence": 2,
        "rating_notes": "权威2:具名原文 · 时效1:日期可查 · 交叉2:跨源一致 · 完整1:摘要充分 · 无关2:独立用户",
    } if scored else {}
    store.add_evidence(
        id=evidence_id,
        report_id="r-ledger",
        goal_id=goal_id,
        platform="web_search",
        permalink=permalink,
        fetched_at="2026-08-27T00:00:00+00:00",
        title=f"标题 {evidence_id}",
        content_excerpt=excerpt,
        author_name=f"作者 {evidence_id}",
        **scores,
    )


def _mark_done(store, runs_root: Path, goal_id: str, chapter_id: str) -> Path:
    path = runs_root / "r-ledger" / f"goals/{goal_id}/{chapter_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([{
            "platform": "web_search",
            "permalink": f"https://artifact.example/{goal_id}",
            "fetched_at": "2026-08-27T00:00:00+00:00",
            "title": f"{goal_id} 产物",
        }], ensure_ascii=False),
        encoding="utf-8",
    )
    store.ensure_chapters(
        "r-ledger", [{"goal_id": goal_id, "chapter_id": chapter_id}],
        updated_at="2026-08-27T00:00:00Z",
    )
    store.finish_chapter(
        "r-ledger", goal_id, chapter_id,
        status="done", reason=None, actual_output_path=str(path), actual_count=1,
        updated_at="2026-08-27T00:00:01Z",
    )
    return path


def _run_sectioned(
    tmp_path: Path,
    *,
    goal_ids: list[str],
    declared_paths: list[Path],
    seed=None,
    render=None,
):
    store = _store(tmp_path)
    runs_root = tmp_path / "runs"
    for path in declared_paths:
        relative = path.relative_to(runs_root / "r-ledger")
        goal_id = relative.parts[1]
        chapter_id = path.stem
        store.ensure_chapters(
            "r-ledger", [{"goal_id": goal_id, "chapter_id": chapter_id}],
            updated_at="2026-08-27T00:00:00Z",
        )
        store.finish_chapter(
            "r-ledger", goal_id, chapter_id,
            status="done", reason=None, actual_output_path=str(path), actual_count=1,
            updated_at="2026-08-27T00:00:01Z",
        )
    goals = [SimpleNamespace(goal_id=goal_id, title=goal_id) for goal_id in goal_ids]
    plan = SimpleNamespace(
        research_id="r-ledger", title="证据池报告", goals=goals,
    )
    agent = SimpleNamespace(
        output={"shape": "object"},
        chapter={
            "chapter_id": "ch-report",
            "opening": {
                "inputs": [
                    {"path": str(path.relative_to(runs_root / "r-ledger"))}
                    for path in declared_paths
                ],
            },
        },
    )
    context = SimpleNamespace(goal_id=goal_ids[0], engine="claude")
    output = runs_root / "r-ledger" / f"goals/{goal_ids[0]}/report.md"
    task = EngineTask(
        body="写报告",
        output_path=output,
        output_format="markdown",
        research_id="r-ledger",
        goal_id=goal_ids[0],
        agent_id="report-writing",
        agent_kind="report_writing",
        validators=[
            "file_exists",
            "sections_exist:结论,信息源",
            "citation_marks_resolvable",
            "no_orphan_citation",
        ],
        capability=Capability(
            tools=("fs.write",),
            fs=FileSystemScope(write=(f"goals/{goal_ids[0]}/**",)),
        ),
    )
    bodies: dict[str, str] = {}
    events: list[dict] = []

    class Adapter:
        async def run(self, section_task, ctx, on_event=None):
            del on_event
            bodies[section_task.output_path.name] = section_task.body
            pool = _pool_from_body(section_task.body)
            section_task.output_path.parent.mkdir(parents=True, exist_ok=True)
            if render is None:
                index = int(section_task.output_path.stem.removeprefix("sec-")) - 1
                item = pool["items"][-1 if index == 0 else 0]
                text = (
                    f"## 结论\n\n- 本节判断 {item['citation']}\n\n"
                    f"## 信息源\n\n- {item['citation']} "
                    f"[{item['title']}]({item['permalink']})\n"
                )
            else:
                text = render(pool, section_task)
            section_task.output_path.write_text(text, encoding="utf-8")
            return EngineRunResult(
                conclusion=OwliResult(
                    "done", str(section_task.output_path), "完成", [], [], [], None,
                ),
                conclusion_error=None,
                validation=validation.validate(ctx, section_task.validators),
                events=[],
                permission_denials=[],
            )

    projected: list[str] = []

    def persist_goal_evidence(_plan, goal):
        projected.append(goal.goal_id)
        if seed is not None:
            seed(store, goal.goal_id)

    async def on_event(event):
        events.append(event)

    result = asyncio.run(run_sectioned_task(
        plan=plan,
        agent=agent,
        context=context,
        base_task=task,
        adapter=Adapter(),
        store=store,
        runs_root=runs_root,
        now_iso=lambda: "2026-08-27T00:00:02Z",
        on_event=on_event,
        persist_goal_evidence=persist_goal_evidence,
    ))
    return result, store, bodies, events, projected, output


def test_跨_goal_done_覆盖的证据先投影再按全局稳定角标进入每节(tmp_path):
    runs_root = tmp_path / "runs"
    artifact_store = _store(tmp_path)
    paths = [
        _mark_done(artifact_store, runs_root, goal_id, f"data-{index}")
        for index, goal_id in enumerate(("goal-1", "goal-2", "goal-3"), start=1)
    ]
    # 重建数据库但保留产物文件；runner 按真实路径重新登记完整账本行。
    (tmp_path / "owli.db").unlink()

    seeded: set[str] = set()

    def seed(target_store, goal_id):
        if goal_id in seeded:
            return
        seeded.add(goal_id)
        _add_evidence(
            target_store,
            evidence_id=f"ev-{goal_id[-1]}",
            goal_id=goal_id,
            permalink=f"https://evidence.example/{goal_id}",
        )

    result, _, bodies, _, projected, output = _run_sectioned(
        tmp_path,
        goal_ids=["goal-1", "goal-2", "goal-3"],
        declared_paths=paths,
        seed=seed,
    )

    assert result.succeeded is True
    assert projected == ["goal-1", "goal-2", "goal-3"]
    pools = [_pool_from_body(bodies[f"sec-{index}.md"]) for index in range(1, 4)]
    assert [item["goal_id"] for item in pools[0]["items"]] == [
        "goal-1", "goal-2", "goal-3",
    ]
    assert [item["citation"] for item in pools[0]["items"]] == [
        "[S01]", "[S02]", "[S03]",
    ]
    assert pools[0] == pools[1] == pools[2]
    report = output.read_text(encoding="utf-8")
    assert "本节判断 [S03]" in report
    assert "本节判断 [S01]" in report


def test_证据池超过99条截断并发一次事件且摘要标注截断(tmp_path):
    seeded = False

    def seed(store, goal_id):
        nonlocal seeded
        if seeded:
            return
        seeded = True
        for index in range(1, 102):
            _add_evidence(
                store,
                evidence_id=f"ev-{index:03d}",
                goal_id=goal_id,
                permalink=f"https://evidence.example/{index:03d}",
                excerpt="甲" * 121 if index == 1 else "正文",
                scored=index == 1,
            )

    result, _, bodies, events, _, _ = _run_sectioned(
        tmp_path,
        goal_ids=["goal-1"],
        declared_paths=[],
        seed=seed,
    )

    assert result.succeeded is True
    pool = _pool_from_body(bodies["sec-1.md"])
    assert len(pool["items"]) == 99
    assert pool["omitted_count"] == 2
    assert pool["items"][-1]["citation"] == "[S99]"
    assert pool["items"][0]["content_excerpt"] == "甲" * 120
    assert pool["items"][0]["content_excerpt_truncated"] is True
    assert pool["items"][0]["score_authority"] == 2
    assert pool["items"][0]["rating_notes"]
    truncations = [event for event in events if event["type"] == "evidence_pool_truncated"]
    assert len(truncations) == 1
    assert truncations[0]["data"]["omitted_count"] == 2
    assert "已截断 2 条，未列出的不得引用" in bodies["sec-1.md"]
    assert validation._CITATION.fullmatch("[S99]")
    assert validation._CITATION.fullmatch("[S100]") is None


def test_证据池为空不回退_done_产物_URL_且整章判红(tmp_path):
    fallback = "https://artifact.example/goal-1"
    runs_root = tmp_path / "runs"
    artifact_store = _store(tmp_path)
    done_path = _mark_done(artifact_store, runs_root, "goal-1", "data-1")
    (tmp_path / "owli.db").unlink()

    def render(pool, section_task):
        del section_task
        assert pool["items"] == []
        return (
            "## 结论\n\n- 错误回退到产物链接 [S01]\n\n"
            f"## 信息源\n\n- [S01] [产物链接]({fallback})\n"
        )

    result, _, bodies, _, _, _ = _run_sectioned(
        tmp_path,
        goal_ids=["goal-1"],
        declared_paths=[done_path],
        render=render,
    )

    assert result.succeeded is False
    assert result.chapter_status == "missing"
    assert "本节无可引用证据" in bodies["sec-1.md"]
    assert "不得回退到 done 产物里的 URL" in bodies["sec-1.md"]


def test_正文出现池外裸链接即使角标闭合也判红(tmp_path):
    def seed(store, goal_id):
        if store.list_evidence("r-ledger"):
            return
        _add_evidence(
            store,
            evidence_id="ev-1",
            goal_id=goal_id,
            permalink="https://evidence.example/allowed",
        )

    def render(pool, section_task):
        del section_task
        item = pool["items"][0]
        return (
            "## 结论\n\n"
            f"- 角标本身闭合 {item['citation']}，但散文夹带 "
            "https://outside.example/not-in-pool\n\n"
            "## 信息源\n\n"
            f"- {item['citation']} [{item['title']}]({item['permalink']})\n"
        )

    result, _, _, _, _, _ = _run_sectioned(
        tmp_path,
        goal_ids=["goal-1"],
        declared_paths=[],
        seed=seed,
        render=render,
    )

    assert result.succeeded is False
    assert result.chapter_status == "missing"


def test_恢复态全部节已_done_仍按证据池全局编号拼装(tmp_path):
    store = _store(tmp_path)
    runs_root = tmp_path / "runs"
    section_path = runs_root / "r-ledger/goals/goal-1/report/sec-1.md"
    section_path.parent.mkdir(parents=True, exist_ok=True)
    section_path.write_text(
        "## 结论\n\n- 使用第三条证据 [S03]\n\n"
        "## 信息源\n\n- [S03] [第三条](https://evidence.example/3)\n",
        encoding="utf-8",
    )
    store.ensure_chapters(
        "r-ledger", [{"goal_id": "goal-1", "chapter_id": "ch-report/sec-1"}],
        updated_at="2026-08-27T00:00:00Z",
    )
    store.finish_chapter(
        "r-ledger", "goal-1", "ch-report/sec-1",
        status="done", reason=None, actual_output_path=str(section_path), actual_count=1,
        updated_at="2026-08-27T00:00:01Z",
    )
    plan = SimpleNamespace(
        research_id="r-ledger",
        title="恢复报告",
        goals=[SimpleNamespace(goal_id="goal-1", title="goal-1")],
    )
    agent = SimpleNamespace(
        output={"shape": "object"},
        chapter={"chapter_id": "ch-report", "opening": {"inputs": []}},
    )
    output = runs_root / "r-ledger/goals/goal-1/report.md"
    task = EngineTask(
        body="恢复拼装",
        output_path=output,
        output_format="markdown",
        research_id="r-ledger",
        goal_id="goal-1",
        agent_id="report-writing",
        agent_kind="report_writing",
        validators=["file_exists", "citation_marks_resolvable", "no_orphan_citation"],
        capability=Capability(
            tools=("fs.write",),
            fs=FileSystemScope(write=("goals/goal-1/**",)),
        ),
    )

    class NeverAdapter:
        async def run(self, *args, **kwargs):
            raise AssertionError("全部节已 done 时不应重跑 Adapter")

    seeded = False

    def persist(_plan, goal):
        nonlocal seeded
        if seeded:
            return
        seeded = True
        for index in range(1, 4):
            _add_evidence(
                store,
                evidence_id=f"ev-{index}",
                goal_id=goal.goal_id,
                permalink=f"https://evidence.example/{index}",
            )

    result = asyncio.run(run_sectioned_task(
        plan=plan,
        agent=agent,
        context=SimpleNamespace(goal_id="goal-1", engine="claude"),
        base_task=task,
        adapter=NeverAdapter(),
        store=store,
        runs_root=runs_root,
        now_iso=lambda: "2026-08-27T00:00:02Z",
        on_event=lambda event: asyncio.sleep(0),
        persist_goal_evidence=persist,
    ))

    assert result.succeeded is True
    report = output.read_text(encoding="utf-8")
    assert "使用第三条证据 [S03]" in report
    assert "- [S03] [第三条](https://evidence.example/3)" in report
