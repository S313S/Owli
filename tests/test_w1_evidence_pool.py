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


def _section_pool(bodies: dict[str, str], section: str) -> dict:
    """§D-031：节可能被切成片下发，本节池要把各片的 items 按片序并回来。

    没分片时就是 `sec-N.md` 那一份，与分片前逐字相同；分片时按
    `sec-N.part.1.md`、`.part.2.md`… 的片序拼——池的组成、顺序、omitted_count
    这些本用例真正要守的东西，一样都没放松。
    """
    single = bodies.get(f"{section}.md")
    if single is not None:
        return _pool_from_body(single)
    names = sorted(
        (name for name in bodies if name.startswith(f"{section}.part.")),
        key=lambda name: int(name.split(".part.")[1].split(".")[0]),
    )
    assert names, f"{section} 既没有整节 body 也没有片 body：{sorted(bodies)}"
    pools = [_pool_from_body(bodies[name]) for name in names]
    merged = dict(pools[0])
    merged["items"] = [item for pool in pools for item in pool["items"]]
    return merged


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
    agent_kind="report_writing",
    raw_render=None,
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
        agent_kind=agent_kind,
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
                stem = section_task.output_path.stem.split(".part.")[0]
                index = int(stem.removeprefix("sec-")) - 1
                item = pool["items"][-1 if index == 0 else 0]
                text = (
                    f"## 结论\n\n- 本节判断 {item['citation']}\n\n"
                    f"## 信息源\n\n- {item['citation']} "
                    f"[{item['title']}]({item['permalink']})\n"
                )
            else:
                text = render(pool, section_task)
            if raw_render is not None:
                # D-025 货 4：raw_render 原样落盘，不再由助手包信封。
                section_task.output_path.write_text(
                    raw_render(pool, section_task), encoding="utf-8",
                )
            else:
                section_task.output_path.write_text(
                    json.dumps(
                        {"markdown": text, "claims": []}, ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
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
    pools = [_section_pool(bodies, f"sec-{index}") for index in range(1, 4)]
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
    pool = _section_pool(bodies, "sec-1")
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
    assert truncations[0]["data"]["goal_quotas"] == {"goal-1": 99}
    assert truncations[0]["data"]["goal_selected_counts"] == {"goal-1": 99}
    assert truncations[0]["data"]["goal_floor_degraded"] is False
    # §D-031：告示按整节全池算（片只换 items），逐片下发时每片都带着它。
    assert all(
        "本节可见角标池已裁剪至 30 条" in body
        for name, body in bodies.items() if name.startswith("sec-1")
    )
    assert all(
        "裁剪不缩小本 research 全量 evidence permalink 的 URL 判定面" in body
        for name, body in bodies.items() if name.startswith("sec-1")
    )
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
    pool = _section_pool(bodies, "sec-1")
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
    first_pool = _section_pool(first, "sec-1")
    second_pool = _section_pool(second, "sec-1")

    assert len(first_pool["items"]) == 30
    assert {item["platform"] for item in first_pool["items"]} == {"xhs", "web_search"}
    assert json.dumps(first_pool, ensure_ascii=False) == json.dumps(
        second_pool, ensure_ascii=False,
    )


def test_同一证据在不同节不同章仍沿用全局_S_编号():
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
    # 章 ID 不参与全局编号；两次调用代表两个父章里 goal 不同的报告节。
    first_chapter, _ = _evidence_index(
        rows, {"goal-1", "goal-2"}, section_goal_id="goal-1",
    )
    second_chapter, _ = _evidence_index(
        rows, {"goal-1", "goal-2"}, section_goal_id="goal-2",
    )

    first_mark = next(
        item["citation"]
        for item in first_chapter["items"]
        if item["evidence_id"] == "shared"
    )
    second_mark = next(
        item["citation"]
        for item in second_chapter["items"]
        if item["evidence_id"] == "shared"
    )
    assert first_mark == second_mark == "[S21]"


def test_全局99号按goal配额避免467条吃光136条且空goal不占额():
    from app.orchestrator.sectioning import _evidence_index

    rows = [
        {
            "id": f"g1-{index:03d}",
            "goal_id": "goal-1",
            "platform": "xhs" if index % 2 else "web_search",
            "permalink": f"https://goal-1.example/{index:03d}",
        }
        for index in range(467)
    ] + [
        {
            "id": f"g2-{index:03d}",
            "goal_id": "goal-2",
            "platform": "douyin" if index % 2 else "web_search",
            "permalink": f"https://goal-2.example/{index:03d}",
        }
        for index in range(136)
    ]

    pool, citations = _evidence_index(
        rows,
        {"goal-1", "goal-2", "goal-3"},
        section_goal_id="goal-1",
    )

    selected_counts = {
        goal_id: sum(goal_id in permalink for permalink in citations)
        for goal_id in ("goal-1", "goal-2", "goal-3")
    }
    assert len(citations) <= 99
    assert selected_counts["goal-2"] > 0
    assert selected_counts["goal-3"] == 0
    assert pool["goal_quotas"] == {"goal-1": 66, "goal-2": 33}
    assert pool["goal_selected_counts"] == {"goal-1": 66, "goal-2": 33}
    assert pool["goal_floor_degraded"] is False


def test_同一证据输入顺序变化后全局编号逐条相同():
    from app.orchestrator.sectioning import _evidence_index

    rows = [
        {
            "id": f"{goal_id}-{index:03d}",
            "goal_id": goal_id,
            "platform": ("xhs", "web_search", "douyin")[index % 3],
            "permalink": f"https://{goal_id}.example/{index:03d}",
        }
        for goal_id, count in (("goal-1", 120), ("goal-2", 80))
        for index in range(count)
    ]

    first_pool, first_citations = _evidence_index(
        rows,
        {"goal-1", "goal-2"},
        section_goal_id="goal-1",
    )
    second_pool, second_citations = _evidence_index(
        list(reversed(rows)),
        {"goal-1", "goal-2"},
        section_goal_id="goal-1",
    )

    assert first_citations == second_citations
    assert first_pool == second_pool


def test_非空goal超过四个时退化为比例配额并记账():
    from app.orchestrator.sectioning import _evidence_index

    rows = [
        {
            "id": f"{goal_id}-{index:03d}",
            "goal_id": goal_id,
            "platform": "web_search",
            "permalink": f"https://{goal_id}.example/{index:03d}",
        }
        for goal_id, count in (
            ("goal-1", 50),
            ("goal-2", 40),
            ("goal-3", 30),
            ("goal-4", 20),
            ("goal-5", 10),
        )
        for index in range(count)
    ]

    pool, citations = _evidence_index(
        rows,
        {f"goal-{index}" for index in range(1, 6)},
        section_goal_id="goal-1",
    )

    assert len(citations) == 99
    assert sum(pool["goal_quotas"].values()) == 99
    assert pool["goal_selected_counts"] == pool["goal_quotas"]
    assert pool["goal_floor_degraded"] is True


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
    assert _section_pool(bodies, "sec-1")["items"][0]["goal_id"] == "goal-1"


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
        # §D-031：本节池按片下发，这里看到的是本片那几条；本用例守的是
        # 「被裁掉的库内 URL 不在池里」，整节 30 条另在下面按片并回后断言。
        assert pool["items"]
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

    result, store, bodies, _, _, _ = _run_sectioned(
        tmp_path,
        goal_ids=["goal-1"],
        declared_paths=[],
        seed=seed,
        render=render,
    )

    assert result.succeeded is True
    assert store.list_chapters("r-ledger")[0]["status"] == "done"
    # 整节仍是 30 条、且一条库内被裁 URL 都没混进来（片并回来后判）。
    section_pool = _section_pool(bodies, "sec-1")
    assert len(section_pool["items"]) == 30
    assert not (
        {item["permalink"] for item in section_pool["items"]} & set(real_urls)
    )


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


def _pool_validation_case(tmp_path: Path, markdown: str, allowed_urls: set[str]):
    from app.orchestrator.sectioning import _section_evidence_pool_result

    visible_url = "https://evidence.example/visible"
    tmp_path.mkdir(parents=True, exist_ok=True)
    section_path = tmp_path / "sec.md"
    section_path.write_text(
        json.dumps({"markdown": markdown, "claims": []}, ensure_ascii=False),
        encoding="utf-8",
    )
    pool = {"items": [{"citation": "[S01]", "permalink": visible_url}]}
    return _section_evidence_pool_result(section_path, pool, allowed_urls)


def test_只有格式违规时不得误报证据池契约(tmp_path):
    visible_url = "https://evidence.example/visible"
    result = _pool_validation_case(
        tmp_path,
        (
            "## 结论\n\n- 结论列表项没有角标\n\n"
            f"## 信息源\n\n- [S01] [可见证据]({visible_url})\n"
        ),
        {visible_url},
    )

    assert result.verdict is validation.Verdict.FAIL
    assert "证据池唯一引用源契约" not in result.message
    assert "节正文违反撰写格式契约，共 2 处" in result.message
    assert "citation_marks_resolvable" in result.message
    assert "no_orphan_citation" in result.message


def test_只有池外URL时只报证据池契约且计数正确(tmp_path):
    visible_url = "https://evidence.example/visible"
    outside_url = "https://outside.example/not-in-pool"
    result = _pool_validation_case(
        tmp_path,
        (
            f"## 结论\n\n- 可校验结论 [S01]，但夹带 {outside_url}\n\n"
            f"## 信息源\n\n- [S01] [可见证据]({visible_url})\n"
        ),
        {visible_url},
    )

    assert result.verdict is validation.Verdict.FAIL
    assert result.message == "节正文违反证据池唯一引用源契约，共 1 处"
    assert "撰写格式契约" not in result.message


def test_池外URL与格式违规同现时两类文案和计数分开(tmp_path):
    visible_url = "https://evidence.example/visible"
    outside_url = "https://outside.example/not-in-pool"
    result = _pool_validation_case(
        tmp_path,
        (
            "## 结论\n\n"
            "- 有角标结论 [S01]\n"
            f"- 无角标结论并夹带 {outside_url}\n\n"
            f"## 信息源\n\n- [S01] [可见证据]({visible_url})\n"
        ),
        {visible_url},
    )

    assert result.verdict is validation.Verdict.FAIL
    assert "节正文违反证据池唯一引用源契约，共 1 处" in result.message
    assert "节正文违反撰写格式契约，共 1 处" in result.message
    assert "citation_marks_resolvable" in result.message


def test_撰写提示词含四条口径且证据缺口独立成段不判红(tmp_path):
    def seed(store, goal_id):
        if store.list_evidence("r-ledger"):
            return
        _add_evidence(
            store,
            evidence_id="ev-1",
            goal_id=goal_id,
            permalink="https://evidence.example/visible",
            platform="xhs",
        )

    result, _, bodies, _, _, _ = _run_sectioned(
        tmp_path,
        goal_ids=["goal-1"],
        declared_paths=[],
        seed=seed,
        render=lambda pool, task: (
            "## 结论\n\n- 有证据的判断 [S01]\n\n"
            "## 证据缺口\n\n本节可见证据不足，未覆盖 X。\n\n"
            "## 信息源\n\n- [S01] [可见证据]"
            f"({pool['items'][0]['permalink']})\n"
        ),
    )

    assert result.succeeded is True
    body = bodies["sec-1.md"]
    assert "缺口/限制性陈述" in body and "独立的『证据缺口』段" in body
    assert "『结论』列表每一项必须带至少一个 [Sxx]" in body
    assert "权威来源（官网 / 媒体 / 评测站）：可以直陈，逐条当事实引" in body
    assert "在国内小红书平台上，大多数……情况是……" in body
    assert "claims 的 stance / firsthand" in body
    assert "JSON 信封" in body and "markdown" in body and "claims" in body
    assert "国内社媒优先" not in body


def test_结论每项带角标可过且换措辞的无角标项仍判红(tmp_path):
    visible_url = "https://evidence.example/visible"
    valid = _pool_validation_case(
        tmp_path / "valid",
        (
            "## 结论\n\n- 第一项 [S01]\n- 第二项也有角标 [S01]\n\n"
            f"## 信息源\n\n- [S01] [可见证据]({visible_url})\n"
        ),
        {visible_url},
    )
    assert valid.verdict is validation.Verdict.PASS

    for index, wording in enumerate(("仍需观察", "换一种完全不同的表述"), start=1):
        case_root = tmp_path / f"invalid-{index}"
        case_root.mkdir()
        invalid = _pool_validation_case(
            case_root,
            (
                f"## 结论\n\n- 已引用项 [S01]\n- {wording}\n\n"
                f"## 信息源\n\n- [S01] [可见证据]({visible_url})\n"
            ),
            {visible_url},
        )
        assert invalid.verdict is validation.Verdict.FAIL
        assert "citation_marks_resolvable" in invalid.message


def test_裸markdown节产物即使引用闭合也判红(tmp_path):
    from app.orchestrator.sectioning import _section_evidence_pool_result

    visible_url = "https://evidence.example/visible"
    section_path = tmp_path / "sec.md"
    section_path.write_text(
        (
            "## 结论\n\n- 引用闭合 [S01]\n\n"
            f"## 信息源\n\n- [S01] [可见证据]({visible_url})\n"
        ),
        encoding="utf-8",
    )
    result = _section_evidence_pool_result(
        section_path,
        {"items": [{"citation": "[S01]", "permalink": visible_url}]},
        {visible_url},
    )

    assert result.verdict is validation.Verdict.FAIL
    assert "JSON 信封" in result.message


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
    first = _section_pool(bodies, "sec-1")
    second = _section_pool(bodies, "sec-2")
    assert [(item["evidence_id"], item["citation"]) for item in first["items"]] == [
        ("ev-z-goal-1", "[S01]"),
    ]
    assert [(item["evidence_id"], item["citation"]) for item in second["items"]] == [
        ("ev-z-goal-2", "[S02]"),
    ]


def test_恢复时新证据使旧_S_号失效_不重写节_合并期按permalink重排(tmp_path):
    """§D-046 语义替换：原用例（`..._则复位_done_节重写`）要求整节复位重写。

    那条判据在 §D-031 把「合并期按 permalink 查全局编号重排角标」做进
    `_merge_shard_structures` 之后就失去了理由：节内部的 [SNN] 只是局部别名，
    成稿里的编号是按 permalink 现查现排的，旧号根本传不到成稿。为这点编号
    漂移重写整节，代价是把一节好稿推倒重来——真机两次现场（D-044 / D-046）
    正是这么把 19 637 B 与 24 142 B 的好稿写成一百多字节占位的。

    W-1 真正要保的产品性质原样保留并在下面断得更硬：**ev-z 在成稿里必须拿到
    全局正确的 [S02]**。区别只是这个正确编号现在由合并期给出，不必付一轮引擎。
    """
    store = _store(tmp_path)
    runs_root = tmp_path / "runs"
    section_path = runs_root / "r-ledger/goals/goal-1/report/sec-1.md"
    section_path.parent.mkdir(parents=True, exist_ok=True)
    section_path.write_text(
        json.dumps({
            "markdown": (
                "## 结论\n\n- 旧编号 [S01]\n\n"
                "## 信息源\n\n- [S01] [旧证据](https://evidence.example/z)\n"
            ),
            "claims": [],
        }, ensure_ascii=False),
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
                json.dumps({
                    "markdown": (
                        f"## 结论\n\n- 新编号 {item['citation']}\n\n"
                        f"## 信息源\n\n- {item['citation']} "
                        f"[旧证据]({item['permalink']})\n"
                    ),
                    "claims": [],
                }, ensure_ascii=False),
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
    assert calls == [], "已写完的节不该为了编号漂移被重写一遍"
    merged = output.read_text(encoding="utf-8")
    # 节里写的是旧号 [S01]，成稿按 permalink 重排成全局正确的 [S02]。
    assert "旧编号 [S02]" in merged, merged
    assert "[S02] [旧证据](https://evidence.example/z)" in merged, merged
    assert "[S01]" not in merged, merged


def test_恢复态全部节已_done_仍按证据池全局编号拼装(tmp_path):
    store = _store(tmp_path)
    runs_root = tmp_path / "runs"
    section_path = runs_root / "r-ledger/goals/goal-1/report/sec-1.md"
    section_path.parent.mkdir(parents=True, exist_ok=True)
    section_path.write_text(
        json.dumps({
            "markdown": (
                "## 结论\n\n- 使用第三条证据 [S03]\n\n"
                "## 信息源\n\n- [S03] [第三条](https://evidence.example/3)\n"
            ),
            "claims": [],
        }, ensure_ascii=False),
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


def test_交叉验证与汇总章同样下发JSON信封指令且信封产物可过(tmp_path) -> None:
    """D-025 货 2：信封指令扩到 SECTIONED_KINDS 全部节化章，不再只发 report 类。"""

    def seed(store, goal_id):
        if store.list_evidence("r-ledger"):
            return
        _add_evidence(
            store,
            evidence_id="ev-1",
            goal_id=goal_id,
            permalink="https://evidence.example/visible",
            platform="xhs",
        )

    def render(pool, task):
        # 助手 Adapter 会把返回值包进 JSON 信封，这里只回裸 markdown 正文。
        return (
            "## 结论\n\n- 有证据的判断 [S01]\n\n"
            "## 信息源\n\n- [S01] [可见证据]"
            f"({pool['items'][0]['permalink']})\n"
        )

    for kind in ("cross_validation", "summary"):
        case_root = tmp_path / kind
        case_root.mkdir()
        result, _, bodies, _, _, _ = _run_sectioned(
            case_root,
            goal_ids=["goal-1"],
            declared_paths=[],
            seed=seed,
            render=render,
            agent_kind=kind,
        )
        body = bodies["sec-1.md"]
        assert "JSON 信封" in body and "claims" in body, kind
        assert "输出骨架示例" in body, kind
        assert "第一个字符必须是" in body and "代码围栏" in body, kind
        assert result.succeeded is True, kind


def _coerce_case(tmp_path, name, content):
    from app.orchestrator.sectioning import (
        _coerce_section_envelope, _section_evidence_pool_result,
    )
    visible_url = "https://evidence.example/visible"
    section_path = tmp_path / f"{name}.md"
    section_path.write_text(content, encoding="utf-8")
    note = _coerce_section_envelope(section_path)
    result = _section_evidence_pool_result(
        section_path,
        {"items": [{"citation": "[S01]", "permalink": visible_url}]},
        {visible_url},
    )
    return note, result


_ENVELOPE_BODY = (
    "## 结论\n\n- 引用闭合 [S01]\n\n"
    "## 信息源\n\n- [S01] [可见证据](https://evidence.example/visible)\n"
)


def test_信封兜底_围栏与裸markdown救回_语法坏不救且带错误位置(tmp_path) -> None:
    """D-025 货 3：_coerce_section_envelope 四形状各一条。"""
    import json as _json

    envelope = _json.dumps(
        {"markdown": _ENVELOPE_BODY, "claims": []}, ensure_ascii=False,
    )
    # 1) 合法信封：不兜底、直接过
    note, result = _coerce_case(tmp_path, "valid", envelope)
    assert note is None
    assert result.verdict is validation.Verdict.PASS
    # 2) ```json 围栏包信封：剥围栏救回
    note, result = _coerce_case(
        tmp_path, "fenced", f"```json\n{envelope}\n```",
    )
    assert note == "fence_stripped"
    assert result.verdict is validation.Verdict.PASS
    # 3) 裸 Markdown：包成 claims 空数组的信封
    note, result = _coerce_case(tmp_path, "bare", f"# 标题\n\n{_ENVELOPE_BODY}")
    assert note == "bare_markdown_wrapped"
    assert result.verdict is validation.Verdict.PASS
    payload = _json.loads((tmp_path / "bare.md").read_text(encoding="utf-8"))
    assert payload["claims"] == [] and payload["markdown"].startswith("# 标题")
    # 4) JSON 语法坏（F 类）：不救，仍 FAIL 且 conclusion_error 带错误位置
    broken = '{"markdown": "## 结论\\n\\n- x [S01]", "claims": [}'
    note, result = _coerce_case(tmp_path, "broken", broken)
    assert note is None
    assert result.verdict is validation.Verdict.FAIL
    assert result.message.startswith("节产物必须使用 JSON 信封")
    assert "JSON 解析失败" in result.message and "char" in result.message


_BROKEN_ENVELOPE = '{"markdown": "## 结论\\n\\n- x [S01]", "claims": [}'


def _envelope_retry_case(tmp_path, raw_render, seed):
    result, store, _, events, _, _ = _run_sectioned(
        tmp_path,
        goal_ids=["goal-1"],
        declared_paths=[],
        seed=seed,
        raw_render=raw_render,
    )
    retries = [e for e in events if e["type"] == "section_envelope_retry"]
    rows = [
        row for row in store.list_chapters("r-ledger")
        if "/sec-" in row["chapter_id"]
    ]
    return result, retries, rows


def test_信封失败定向重试一次_补包成功则done(tmp_path) -> None:
    """D-025 货 4：offenders 仅 json_envelope 时给一次「只包信封」重试。"""

    def seed(store, goal_id):
        if store.list_evidence("r-ledger"):
            return
        _add_evidence(
            store, evidence_id="ev-1", goal_id=goal_id,
            permalink="https://evidence.example/visible", platform="xhs",
        )

    seen: set[str] = set()

    def raw_render(pool, task):
        name = task.output_path.name
        if name not in seen:
            seen.add(name)
            return _BROKEN_ENVELOPE
        return json.dumps({
            "markdown": (
                "## 结论\n\n- 有证据的判断 [S01]\n\n"
                "## 信息源\n\n- [S01] [可见证据]"
                f"({pool['items'][0]['permalink']})\n"
            ),
            "claims": [],
        }, ensure_ascii=False)

    result, retries, rows = _envelope_retry_case(tmp_path, raw_render, seed)
    assert result.succeeded is True
    assert rows and all(row["status"] == "done" for row in rows)
    assert len(retries) == len(rows)  # 每节恰好一次定向重试


def test_信封重试仍失败则missing且不给第二次(tmp_path) -> None:
    def seed(store, goal_id):
        if store.list_evidence("r-ledger"):
            return
        _add_evidence(
            store, evidence_id="ev-1", goal_id=goal_id,
            permalink="https://evidence.example/visible", platform="xhs",
        )

    result, retries, rows = _envelope_retry_case(
        tmp_path, lambda pool, task: _BROKEN_ENVELOPE, seed,
    )
    assert result.succeeded is False
    assert rows and all(row["status"] == "missing" for row in rows)
    assert all(row["reason"] == "conclusion_invalid" for row in rows)
    assert all(
        str(row["conclusion_error"]).startswith("节产物必须使用 JSON 信封")
        and "JSON 解析失败" in str(row["conclusion_error"])
        for row in rows
    )
    assert len(retries) == len(rows)  # 一节只给一次，不给第二次
