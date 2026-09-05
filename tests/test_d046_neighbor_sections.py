"""§D-046：点名补一节，同章已完成的邻节被连带重写。

两次现场（缺陷卡 docs/acceptance/loop/defects/D-046.md）：补 `ch-6/sec-2` 毁了
`sec-1`；补 `ch-6/sec-1` 把 `sec-2`（19 637 B）与 `sec-3`（24 142 B）双双打回
一百来字节的占位。

**根因是两层，且只有第二层坏**：
① 复位边界是好的——`_is_replayed` 对 `only_chapters=["ch-6/sec-1"]` 只认
   `ch-6` 与 `ch-6/sec-1`，邻节行按 done 原样搬过去、盘上产物一个字没动。
   本文件第一组用例把这条钉住，防以后有人「顺手」把同章节一起复位。
② 复位父章会把 `ch-6` 从「账本已 done」集合里拿掉，于是节化撰写重算邻节的
   可见证据池时跨 goal 那一截够不着了（sec-2 底料池 30 条 weibo+web_search+reddit，
   补节轮只剩 15 条 weibo）→ 老角标一律判越池 → `stale_done` 把整节复位、
   连片产物一起删、从头重写。**补一节 = 整章 N 节重新抽签。**
   第二组用例要求：已 done 且盘上产物还在的节跳过、不调引擎、字节不变。
"""

from __future__ import annotations


# ---------- ① 复位边界只含点名节与父章 ----------

def _rows() -> list[dict]:
    """一章三节，sec-1 是靶子，sec-2 / sec-3 是已写好的邻节。"""

    return [
        {"goal_id": "goal-3", "chapter_id": "ch-6", "status": "done"},
        {"goal_id": "goal-3", "chapter_id": "ch-6/sec-1", "status": "done"},
        {"goal_id": "goal-3", "chapter_id": "ch-6/sec-2", "status": "done"},
        {"goal_id": "goal-3", "chapter_id": "ch-6/sec-3", "status": "done"},
        {"goal_id": "goal-3", "chapter_id": "ch-5", "status": "done"},
    ]


def _reset_set(only_chapters: list[str]) -> set[str]:
    from app.replay.import_research import _is_replayed, _replay_chapters

    wanted = _replay_chapters(only_chapters)
    return {
        str(row["chapter_id"])
        for row in _rows()
        if _is_replayed(row, {"goal-3"}, wanted, reset_done=False)
    }


def test_d046_点名一节只复位它和父章_同章邻节不在复位集合里() -> None:
    assert _reset_set(["ch-6/sec-1"]) == {"ch-6", "ch-6/sec-1"}


def test_d046_点名整章才连子节一起复位() -> None:
    """点名 `ch-6`（不带 `/sec-N`）是「这章整个重做」，与补一节是两回事。"""

    assert _reset_set(["ch-6"]) == {
        "ch-6", "ch-6/sec-1", "ch-6/sec-2", "ch-6/sec-3",
    }


def test_d046_点名的节不牵连同一个goal里别的章() -> None:
    assert "ch-5" not in _reset_set(["ch-6/sec-1"])


# ---------- ② 已写完的邻节跳过、不调引擎、字节不变 ----------

def _envelope(mark: str, url: str, title: str) -> str:
    import json

    return json.dumps({
        "markdown": (
            f"## 结论\n\n- 上一轮写好的判断 {mark}\n\n"
            f"## 信息源\n\n- {mark} [{title}]({url})\n"
        ),
        "claims": [],
    }, ensure_ascii=False)


def _three_section_run(tmp_path, *, done_sections=(1, 2)):
    """一章三节（每 goal 一节），`done_sections` 里的节已经写好了。

    两节的正文刻意用**过期编号**：角标 `[S01]` 底下挂的是第 45 条证据的链接。
    这正是真机现场那 14 处「未逐字映射到证据池 permalink」——评级回填重排过
    编号，好稿的角标在今天的池子里对不上了。旧尺子据此判它们作废重写；
    新尺子只问「这个链接还在不在本轮证据库里」，答案是在，于是原样保留。
    """
    import asyncio
    from types import SimpleNamespace

    from app.adapters import validation
    from app.adapters.capability import Capability, FileSystemScope
    from app.adapters.contracts import EngineRunResult, EngineTask, OwliResult
    from app.orchestrator.sectioning import run_sectioned_task
    from tests.test_m3h_ledger import _store
    from tests.test_w1_evidence_pool import _pool_from_body

    store = _store(tmp_path)
    runs_root = tmp_path / "runs"
    goals = ["goal-1", "goal-2", "goal-3"]
    for index in range(1, 61):
        store.add_evidence(
            id=f"ev-{index:03d}", report_id="r-ledger",
            goal_id=goals[(index - 1) % 3],
            platform=("xhs", "weibo", "reddit")[(index - 1) % 3],
            permalink=f"https://example.com/{index:03d}",
            fetched_at="2026-09-05T00:00:00Z", title=f"来源 {index}",
            content_excerpt="可复核正文",
        )
    plan = SimpleNamespace(
        research_id="r-ledger", title="最终页",
        goals=[SimpleNamespace(goal_id=g, title=g) for g in goals],
    )
    agent = SimpleNamespace(
        output={"shape": "object"},
        chapter={"chapter_id": "ch-6", "opening": {"inputs": []}},
    )
    output = runs_root / "r-ledger" / "goals/goal-3/goal-3-report.md"
    section_root = output.parent / output.stem
    section_root.mkdir(parents=True, exist_ok=True)
    seeded: dict[str, int] = {}
    for number in done_sections:
        body = _envelope("[S01]", f"https://example.com/{40 + number:03d}",
                         f"来源 {40 + number}")
        path = section_root / f"sec-{number}.md"
        path.write_text(body, encoding="utf-8")
        seeded[f"sec-{number}.md"] = path.stat().st_size
        store.ensure_chapters(
            "r-ledger", [{"goal_id": "goal-3", "chapter_id": f"ch-6/sec-{number}"}],
            updated_at="2026-09-05T00:00:00Z",
        )
        store.finish_chapter(
            "r-ledger", "goal-3", f"ch-6/sec-{number}", status="done", reason=None,
            actual_output_path=str(path), actual_count=1,
            updated_at="2026-09-05T00:00:01Z",
        )

    task = EngineTask(
        body="写最终页", output_path=output, output_format="markdown",
        research_id="r-ledger", goal_id="goal-3", agent_id="report-writing",
        agent_kind="report_writing",
        validators=["file_exists", "sections_exist:结论,信息源",
                    "citation_marks_resolvable", "no_orphan_citation"],
        capability=Capability(
            tools=("fs.write",),
            fs=FileSystemScope(write=("goals/goal-3/**",)),
        ),
    )
    engine_calls: list[str] = []
    events: list[dict] = []

    class Adapter:
        async def run(self, shard_task, ctx, on_event=None):
            del on_event
            engine_calls.append(shard_task.output_path.name)
            item = _pool_from_body(shard_task.body)["items"][0]
            markdown = (
                f"## 结论\n\n- 本轮新写 {item['citation']}\n\n"
                f"## 信息源\n\n- {item['citation']} "
                f"[{item['title']}]({item['permalink']})\n"
            )
            import json as _json

            shard_task.output_path.parent.mkdir(parents=True, exist_ok=True)
            shard_task.output_path.write_text(
                _json.dumps({"markdown": markdown, "claims": []},
                            ensure_ascii=False),
                encoding="utf-8",
            )
            return EngineRunResult(
                conclusion=OwliResult(
                    "done", str(shard_task.output_path), "完成", [], [], [], None,
                ),
                conclusion_error=None,
                validation=validation.validate(ctx, shard_task.validators),
                events=[], permission_denials=[],
            )

    async def on_event(event):
        events.append(event)

    result = asyncio.run(run_sectioned_task(
        plan=plan, agent=agent,
        context=SimpleNamespace(
            goal_id="goal-3", engine="claude", section_deadline_seconds=None,
        ),
        base_task=task, adapter=Adapter(), store=store, runs_root=runs_root,
        now_iso=lambda: "2026-09-05T00:00:02Z", on_event=on_event,
        persist_goal_evidence=lambda _plan, _goal: None,
    ))
    return result, store, engine_calls, events, section_root, seeded


def test_d046_点名补一节_同章两节已写好的只调一次引擎(tmp_path) -> None:
    _, _, engine_calls, _, _, _ = _three_section_run(tmp_path)

    assert engine_calls, "靶子节该被写"
    # 引擎可能把靶子节切成几片写；要害是它一次都没碰到邻节。
    assert all(name.startswith("sec-3") for name in engine_calls), engine_calls


def test_d046_两节已写好的各发一条跳过事件并带字节数(tmp_path) -> None:
    _, _, _, events, section_root, seeded = _three_section_run(tmp_path)

    skipped = [e for e in events if e["type"] == "write_section_skipped"]
    assert [e["data"]["chapter_id"] for e in skipped] == [
        "ch-6/sec-1", "ch-6/sec-2",
    ], skipped
    assert all(e["data"]["goal_id"] == "goal-3" for e in skipped)
    assert [e["data"]["bytes"] for e in skipped] == [
        seeded["sec-1.md"], seeded["sec-2.md"],
    ]


def test_d046_已写好的节产物字节一个不变(tmp_path) -> None:
    """本包的核心判据：好稿原样保留，不是「重写成差不多的东西」。"""

    _, _, _, _, section_root, seeded = _three_section_run(tmp_path)

    for name, size in seeded.items():
        assert (section_root / name).stat().st_size == size, name
    assert not list(section_root.glob("sec-1.part.*.md")), "跳过的节不该被重写"
    assert not list(section_root.glob("sec-2.part.*.md")), "跳过的节不该被重写"


def test_d046_已写好的节账本仍是done_没被复位重跑(tmp_path) -> None:
    _, store, _, _, _, _ = _three_section_run(tmp_path)

    rows = {
        row["chapter_id"]: row
        for row in store.list_chapters("r-ledger")
        if row["goal_id"] == "goal-3"
    }
    assert rows["ch-6/sec-1"]["status"] == "done"
    assert rows["ch-6/sec-2"]["status"] == "done"
    assert rows["ch-6/sec-1"]["attempts"] == 0, "跳过的节不许 attempts +1"


def test_d046_旧尺子确实判这两节作废_本用例才有意义(tmp_path) -> None:
    """守住用例本身：若哪天夹具退化成「旧尺子也放行」，上面几条就不再证明什么。"""

    from app.orchestrator.sectioning import (
        _evidence_index, _section_evidence_pool_result, _written_section_result,
    )

    _, store, _, _, section_root, _ = _three_section_run(tmp_path)
    rows = store.list_evidence("r-ledger")
    urls = {str(r["permalink"]) for r in rows if r.get("permalink")}
    pool, _numbers = _evidence_index(
        rows, {"goal-1", "goal-2", "goal-3"}, section_goal_id="goal-1",
    )
    path = section_root / "sec-1.md"
    assert _section_evidence_pool_result(
        path, pool, urls,
    ).verdict is not __import__(
        "app.adapters.validation", fromlist=["x"],
    ).Verdict.PASS, "旧尺子应当判它越池"
    assert _written_section_result(path, urls).verdict is __import__(
        "app.adapters.validation", fromlist=["x"],
    ).Verdict.PASS, "新尺子应当放行"
