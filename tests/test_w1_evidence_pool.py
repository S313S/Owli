from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.adapters import validation
from app.adapters.capability import Capability, FileSystemScope
from app.adapters.contracts import EngineRunResult, EngineTask, OwliResult
from app.orchestrator.sectioning import run_sectioned_task
from app.plan.model import Plan
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
    platform: str = "web_search",
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
        platform=platform,
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
    mutate_during_run=None,
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
            if mutate_during_run is not None:
                mutate_during_run(store, section_task)
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

    result, store, bodies, events, _, _ = _run_sectioned(
        tmp_path,
        goal_ids=["goal-1"],
        declared_paths=[],
        seed=seed,
    )

    assert result.succeeded is True
    pool = _pool_from_body(bodies["sec-1.md"])
    assert len(pool["items"]) == 30
    assert pool["omitted_count"] == 71
    assert pool["items"][-1]["citation"] == "[S30]"
    assert pool["items"][0]["content_excerpt"] == "甲" * 120
    assert pool["items"][0]["content_excerpt_truncated"] is True
    assert pool["items"][0]["score_authority"] == 2
    assert pool["items"][0]["rating_notes"]
    truncations = [event for event in events if event["type"] == "evidence_pool_truncated"]
    assert len(truncations) == 1
    assert truncations[0]["data"]["omitted_count"] == 71
    assert truncations[0]["data"]["limit"] == 30
    assert "本节可见角标池已裁剪至 30 条" in bodies["sec-1.md"]
    assert "裁剪不缩小本 research 全量 evidence permalink 的 URL 判定面" in bodies[
        "sec-1.md"
    ]
    assert validation._CITATION.fullmatch("[S99]")
    assert validation._CITATION.fullmatch("[S100]") is None

    from app.orchestrator.sectioning import _evidence_index

    _, citations = _evidence_index(store_rows := store.list_evidence("r-ledger"), {"goal-1"})
    assert len(store_rows) == 101
    assert len(citations) == 99
    assert citations["https://evidence.example/099"] == 99
    assert "https://evidence.example/100" not in citations


def test_81条证据单节只喂30条并记录裁剪事件(tmp_path):
    seeded = False

    def seed(store, goal_id):
        nonlocal seeded
        if seeded:
            return
        seeded = True
        for index in range(1, 82):
            _add_evidence(
                store,
                evidence_id=f"ev-{index:03d}",
                goal_id=goal_id,
                permalink=f"https://evidence.example/{index:03d}",
            )

    result, _, bodies, events, _, _ = _run_sectioned(
        tmp_path,
        goal_ids=["goal-1"],
        declared_paths=[],
        seed=seed,
    )

    assert result.succeeded is True
    pool = _pool_from_body(bodies["sec-1.md"])
    assert len(pool["items"]) == 30
    assert pool["omitted_count"] == 51
    truncations = [event for event in events if event["type"] == "evidence_pool_truncated"]
    assert len(truncations) == 1
    assert truncations[0]["data"]["omitted_count"] == 51
    assert truncations[0]["data"]["limit"] == 30


def test_按平台轮转裁剪且同输入结果逐字节确定(tmp_path):
    def seed(store, goal_id):
        if store.list_evidence("r-ledger"):
            return
        for index in range(1, 76):
            _add_evidence(
                store,
                evidence_id=f"xhs-{index:03d}",
                goal_id=goal_id,
                permalink=f"https://xhs.example/{index:03d}",
                platform="xhs",
            )
        for index in range(1, 7):
            _add_evidence(
                store,
                evidence_id=f"web-{index:03d}",
                goal_id=goal_id,
                permalink=f"https://web.example/{index:03d}",
                platform="web_search",
            )

    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first = _run_sectioned(
        first_root,
        goal_ids=["goal-1"],
        declared_paths=[],
        seed=seed,
    )[2]
    second = _run_sectioned(
        second_root,
        goal_ids=["goal-1"],
        declared_paths=[],
        seed=seed,
    )[2]
    first_pool = _pool_from_body(first["sec-1.md"])
    second_pool = _pool_from_body(second["sec-1.md"])

    assert len(first_pool["items"]) == 30
    assert {item["platform"] for item in first_pool["items"]} == {"xhs", "web_search"}
    assert json.dumps(first_pool, ensure_ascii=False) == json.dumps(
        second_pool, ensure_ascii=False,
    )


def test_同一证据在不同节裁剪后仍沿用全局_S_编号():
    from app.orchestrator.sectioning import _evidence_index

    rows = [
        {
            "id": "shared",
            "goal_id": "goal-1",
            "platform": "shared_platform",
            "permalink": "https://evidence.example/shared",
        },
        *[
            {
                "id": f"a-{index:02d}",
                "goal_id": "goal-1",
                "platform": "xhs",
                "permalink": f"https://evidence.example/a-{index:02d}",
            }
            for index in range(20)
        ],
        *[
            {
                "id": f"b-{index:02d}",
                "goal_id": "goal-2",
                "platform": "web_search",
                "permalink": f"https://evidence.example/b-{index:02d}",
            }
            for index in range(20)
        ],
    ]
    first, _ = _evidence_index(
        rows, {"goal-1", "goal-2"}, section_goal_id="goal-1",
    )
    second, _ = _evidence_index(
        rows, {"goal-1", "goal-2"}, section_goal_id="goal-2",
    )

    first_mark = next(
        item["citation"] for item in first["items"] if item["evidence_id"] == "shared"
    )
    second_mark = next(
        item["citation"] for item in second["items"] if item["evidence_id"] == "shared"
    )
    assert first_mark == second_mark == "[S21]"


def test_done链中撰写章传递覆盖其上游_goal(tmp_path):
    from app.orchestrator.sectioning import _declared_done_goal_closure

    research_root = tmp_path / "runs/r-ledger"
    collector_path = research_root / "goals/goal-1/data.json"
    writer_path = research_root / "goals/goal-3/consistency-check.md"
    rows = [
        {
            "goal_id": "goal-1", "chapter_id": "ch-data", "status": "done",
            "actual_output_path": str(collector_path), "actual_count": 1,
        },
        {
            "goal_id": "goal-3", "chapter_id": "ch-2", "status": "done",
            "actual_output_path": str(writer_path), "actual_count": 1,
        },
    ]
    plan = SimpleNamespace(goals=[
        SimpleNamespace(goal_id="goal-1", agents=[SimpleNamespace(
            chapter={"chapter_id": "ch-data", "opening": {"inputs": []}},
        )]),
        SimpleNamespace(goal_id="goal-3", agents=[SimpleNamespace(
            chapter={
                "chapter_id": "ch-2",
                "opening": {"inputs": [{"path": "goals/goal-1/data.json"}]},
            },
        )]),
    ])
    inputs = {"done": [{
        "goal_id": "goal-3", "chapter_id": "ch-2",
        "path": str(writer_path), "actual_count": 1,
    }], "missing": []}

    assert _declared_done_goal_closure(
        plan, rows, inputs, research_root=research_root,
    ) == {"goal-1", "goal-3"}


def test_evidence非空且done只有撰写章产物时本节池仍非空(tmp_path):
    runs_root = tmp_path / "runs"
    artifact_store = _store(tmp_path)
    writer_path = _mark_done(
        artifact_store, runs_root, "goal-3", "consistency-check",
    )
    (tmp_path / "owli.db").unlink()

    def seed(store, _goal_id):
        if store.list_evidence("r-ledger"):
            return
        _add_evidence(
            store,
            evidence_id="ev-upstream",
            goal_id="goal-1",
            permalink="https://evidence.example/upstream",
            platform="xhs",
        )

    def render(pool, section_task):
        del section_task
        assert pool["items"]
        item = pool["items"][0]
        return (
            f"## 结论\n\n- 回退池可引用 {item['citation']}\n\n"
            f"## 信息源\n\n- {item['citation']} [{item['title']}]({item['permalink']})\n"
        )

    result, _, bodies, _, _, _ = _run_sectioned(
        tmp_path,
        goal_ids=["goal-3"],
        declared_paths=[writer_path],
        seed=seed,
        render=render,
    )

    assert result.succeeded is True
    assert _pool_from_body(bodies["sec-1.md"])["items"][0]["goal_id"] == "goal-1"


def test_evidence只有无goal归属行时回退池仍非空():
    from app.orchestrator.sectioning import _evidence_index

    pool, citations = _evidence_index(
        [{
            "id": "ev-orphan",
            "goal_id": None,
            "platform": "web_search",
            "permalink": "https://evidence.example/orphan",
        }],
        {"goal-3"},
        section_goal_id="goal-3",
    )

    assert len(pool["items"]) == 1
    assert pool["items"][0]["citation"] == "[S01]"
    assert pool["items"][0]["goal_id"] is None
    assert citations == {"https://evidence.example/orphan": 1}


def test_库内但被本节裁掉的_URL_出现在正文则节校验通过(tmp_path):
    real_urls = [
        "https://ai.36kr.com/note-detail/3568010593718096",
        "https://apps.apple.com/cn/app/%E8%85%BE%E8%AE%AF%E4%BC%9A%E8%AE%AE-%E5%A4%9A%E4%BA%BA%E5%AE%9E%E6%97%B6%E8%A7%86%E9%A2%91%E4%BC%9A%E8%AE%AE%E8%BD%AF%E4%BB%B6/id1484048379?platform=mac",
        "https://apps.apple.com/cn/app/%E8%AE%AF%E9%A3%9E%E5%90%AC%E8%A7%81-ai%E5%BD%95%E9%9F%B3%E8%BD%AC%E6%96%87%E5%AD%97%E8%AF%AD%E9%9F%B3%E7%BF%BB%E8%AF%91/id6468032133?mt=12",
        "https://apps.apple.com/cn/app/%E9%80%9A%E4%B9%89%E5%90%AC%E6%82%9F-%E4%BC%9A%E8%AE%AE%E8%AE%B0%E5%BD%95-%E8%AF%AD%E9%9F%B3%E8%BD%AC%E6%96%87%E5%AD%97-%E4%BC%9A%E8%AE%AE%E7%BA%AA%E8%A6%81%E7%A5%9E%E5%99%A8-ai%E5%8A%A9%E6%89%8B/id6779209228",
        "https://apps.apple.com/cn/app/%E9%A3%9E%E4%B9%A6-%E5%AD%97%E8%8A%82%E8%B7%B3%E5%8A%A8%E6%97%97%E4%B8%8B-ai-%E5%B7%A5%E4%BD%9C%E5%B9%B3%E5%8F%B0/id1401729613",
        "https://apps.apple.com/cn/app/%E9%A3%9E%E4%B9%A6-%E5%AD%97%E8%8A%82%E8%B7%B3%E5%8A%A8%E6%97%97%E4%B8%8B-ai-%E5%B7%A5%E4%BD%9C%E5%B9%B3%E5%8F%B0/id1401729613?platform=mac",
        "https://developer.aliyun.com/note/256265125",
        "https://help.aliyun.com/zh/tingwu/release-notes",
        "https://m.app.mi.com/details?id=com.iflyrec.tjapp",
        "https://meeting.tencent.com/support/topic/2082/index.html",
        "https://www.feishu.cn/content/article/7578773484596153570",
        "https://www.feishu.cn/content/article/7600354931119311830",
        "https://www.feishu.cn/hc/zh-CN/articles/360043073734-%E9%A3%9E%E4%B9%A6%E5%8A%9F%E8%83%BD%E5%8F%98%E5%8C%96%E8%B7%AF%E5%BE%84",
        "https://www.iflyrec.com/zhuanxie/697c1750.html",
        "https://www.tingwu.cn",
    ]

    def seed(store, goal_id):
        if store.list_evidence("r-ledger"):
            return
        for index in range(1, 31):
            _add_evidence(
                store,
                evidence_id=f"ev-{index:03d}",
                goal_id=goal_id,
                permalink=f"https://evidence.example/{index:03d}",
            )
        for index, permalink in enumerate(real_urls, start=31):
            _add_evidence(
                store,
                evidence_id=f"ev-{index:03d}",
                goal_id=goal_id,
                permalink=permalink,
            )

    def render(pool, section_task):
        del section_task
        assert len(pool["items"]) == 30
        assert not ({item["permalink"] for item in pool["items"]} & set(real_urls))
        item = pool["items"][0]
        return (
            "## 结论\n\n"
            + "\n".join(
                f"- 库内裁剪外链接 {url} {item['citation']}"
                for url in real_urls
            )
            + f"\n\n## 信息源\n\n- {item['citation']} "
            f"[{item['title']}]({item['permalink']})\n"
        )

    result, store, _, _, _, _ = _run_sectioned(
        tmp_path,
        goal_ids=["goal-1"],
        declared_paths=[],
        seed=seed,
        render=render,
    )

    assert result.succeeded is True
    assert store.list_chapters("r-ledger")[0]["status"] == "done"


def test_claims_permalink_用_research_全量证据池而非本节裁剪子集(tmp_path):
    from app.orchestrator.sectioning import _section_evidence_pool_result

    visible_url = "https://evidence.example/visible"
    cropped_url = "https://evidence.example/cropped"
    section_path = tmp_path / "sec-1.md"
    section_path.write_text(json.dumps({
        "markdown": (
            "## 结论\n\n- 裁剪外证据仍在 research 池内 [S01]。\n\n"
            f"## 信息源\n\n- [S01] [可见证据]({visible_url})\n"
        ),
        "claims": [{
            "id": "c-0101",
            "text": "裁剪外证据仍可联接",
            "evidence": [{"permalink": cropped_url}],
        }],
    }, ensure_ascii=False), encoding="utf-8")
    pool = {"items": [{
        "citation": "[S01]",
        "permalink": visible_url,
    }]}

    result = _section_evidence_pool_result(
        section_path, pool, {visible_url, cropped_url},
    )

    assert result.verdict is validation.Verdict.PASS


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


@pytest.mark.parametrize(
    "foreign_url",
    [
        "https://EVIDENCE.example/allowed/",
        "HTTPS://outside.example/not-in-pool",
    ],
)
def test_正文_URL_必须与证据池逐字一致(tmp_path, foreign_url):
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
            f"## 结论\n\n- 引用 {item['citation']} 但写入 {foreign_url}\n\n"
            "## 信息源\n\n"
            f"- {item['citation']} [{item['title']}]({foreign_url})\n"
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


def test_指定历史_plan_snapshot_的跨_goal_done_与证据池集合对齐():
    """本地验收直接读指定整跑快照；其他环境没有私有跑数据时跳过。"""

    database = Path("../Owli-fix/var/owli.db").resolve()
    if not database.is_file():
        pytest.skip("本机历史整跑数据库不可用")
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        plan_row = connection.execute(
            "SELECT plan_snapshot FROM reports WHERE id = ?",
            ("r-0f92790bfe37",),
        ).fetchone()
        assert plan_row is not None
        plan = Plan.from_json(plan_row["plan_snapshot"])
        rows = [dict(row) for row in connection.execute(
            "SELECT * FROM chapter_progress WHERE research_id = ?",
            (plan.research_id,),
        )]
        evidence_goal_ids = {
            str(row[0]) for row in connection.execute(
                "SELECT DISTINCT goal_id FROM evidence WHERE report_id = ? ",
                (plan.research_id,),
            )
        }

    writer = next(
        agent for agent in plan.goals[0].agents
        if agent.agent_id == "report-writing"
    )
    declared = writer.chapter["opening"]["inputs"]
    from app.orchestrator.sectioning import (
        _ledger_inputs,
        _merge_declared_done_inputs,
    )

    inputs = _merge_declared_done_inputs(
        _ledger_inputs(rows, "goal-1"),
        rows,
        declared,
        research_root=database.parent.parent / "runs" / plan.research_id,
    )
    done_goal_ids = {str(item["goal_id"]) for item in inputs["done"]}

    assert done_goal_ids == {"goal-1", "goal-2", "goal-3"}
    assert evidence_goal_ids == done_goal_ids


def test_父章写节期间直写新证据不改变已冻结_S_编号(tmp_path):
    seeded: set[str] = set()

    def seed(store, goal_id):
        if goal_id in seeded:
            return
        seeded.add(goal_id)
        _add_evidence(
            store,
            evidence_id=f"ev-z-{goal_id}",
            goal_id=goal_id,
            permalink=f"https://evidence.example/z-{goal_id}",
        )

    mutated = False

    def mutate(store, section_task):
        nonlocal mutated
        if mutated or section_task.output_path.name != "sec-1.md":
            return
        mutated = True
        _add_evidence(
            store,
            evidence_id="ev-a",
            goal_id="goal-1",
            permalink="https://evidence.example/a",
        )

    result, _, bodies, _, _, _ = _run_sectioned(
        tmp_path,
        goal_ids=["goal-1", "goal-2"],
        declared_paths=[],
        seed=seed,
        mutate_during_run=mutate,
    )

    assert result.succeeded is True
    first = _pool_from_body(bodies["sec-1.md"])
    second = _pool_from_body(bodies["sec-2.md"])
    assert [(item["evidence_id"], item["citation"]) for item in first["items"]] == [
        ("ev-z-goal-1", "[S01]"),
    ]
    assert [(item["evidence_id"], item["citation"]) for item in second["items"]] == [
        ("ev-z-goal-2", "[S02]"),
    ]


def test_恢复时新证据使旧_S_号失效则复位_done_节重写(tmp_path):
    store = _store(tmp_path)
    runs_root = tmp_path / "runs"
    section_path = runs_root / "r-ledger/goals/goal-1/report/sec-1.md"
    section_path.parent.mkdir(parents=True, exist_ok=True)
    section_path.write_text(
        "## 结论\n\n- 旧编号 [S01]\n\n"
        "## 信息源\n\n- [S01] [旧证据](https://evidence.example/z)\n",
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
    _add_evidence(
        store, evidence_id="ev-z", goal_id="goal-1",
        permalink="https://evidence.example/z",
    )
    _add_evidence(
        store, evidence_id="ev-a", goal_id="goal-1",
        permalink="https://evidence.example/a",
    )
    plan = SimpleNamespace(
        research_id="r-ledger", title="恢复报告",
        goals=[SimpleNamespace(goal_id="goal-1", title="goal-1")],
    )
    agent = SimpleNamespace(
        output={"shape": "object"},
        chapter={"chapter_id": "ch-report", "opening": {"inputs": []}},
    )
    output = runs_root / "r-ledger/goals/goal-1/report.md"
    task = EngineTask(
        body="恢复拼装", output_path=output, output_format="markdown",
        research_id="r-ledger", goal_id="goal-1",
        agent_id="report-writing", agent_kind="report_writing",
        validators=["file_exists", "citation_marks_resolvable", "no_orphan_citation"],
        capability=Capability(
            tools=("fs.write",),
            fs=FileSystemScope(write=("goals/goal-1/**",)),
        ),
    )
    calls: list[str] = []

    class Adapter:
        async def run(self, section_task, ctx, on_event=None):
            del on_event
            calls.append(section_task.output_path.name)
            pool = _pool_from_body(section_task.body)
            item = next(row for row in pool["items"] if row["evidence_id"] == "ev-z")
            section_task.output_path.write_text(
                f"## 结论\n\n- 新编号 {item['citation']}\n\n"
                f"## 信息源\n\n- {item['citation']} [旧证据]({item['permalink']})\n",
                encoding="utf-8",
            )
            return EngineRunResult(
                conclusion=OwliResult(
                    "done", str(section_task.output_path), "完成", [], [], [], None,
                ),
                conclusion_error=None,
                validation=validation.validate(ctx, section_task.validators),
                events=[], permission_denials=[],
            )

    result = asyncio.run(run_sectioned_task(
        plan=plan, agent=agent,
        context=SimpleNamespace(goal_id="goal-1", engine="claude"),
        base_task=task, adapter=Adapter(), store=store, runs_root=runs_root,
        now_iso=lambda: "2026-08-27T00:00:02Z",
        on_event=lambda event: asyncio.sleep(0),
        persist_goal_evidence=lambda _plan, _goal: None,
    ))

    assert result.succeeded is True
    assert calls == ["sec-1.md"]
    assert "新编号 [S02]" in output.read_text(encoding="utf-8")


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
