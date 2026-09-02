from __future__ import annotations

from tests.test_w1_evidence_pool import _pool_from_body

from pathlib import Path


def _item(index: int, excerpt: str = "正文") -> dict:
    return {
        "citation": f"[S{index:02d}]",
        "permalink": f"https://example.com/{index}",
        "title": f"来源 {index}",
        "content_excerpt": excerpt,
        "platform": "xhs",
        "goal_id": "goal-1",
    }


def test_d031_池按条数切片_30条切3片():
    from app.orchestrator.sectioning import write_shard_sizes

    assert write_shard_sizes([_item(i) for i in range(1, 31)]) == [10, 10, 10]


def test_d031_池不超过一片时走原路():
    """≤10 条 = 一片 = 与分片前逐字一致，这是本包的回归锚。"""
    from app.orchestrator.sectioning import write_shard_sizes

    assert write_shard_sizes([_item(i) for i in range(1, 10)]) == [9]
    assert write_shard_sizes([_item(1)]) == [1]
    assert write_shard_sizes([]) == []


def test_d031_重条目按字节先封片():
    from app.orchestrator.sectioning import write_shard_sizes

    heavy = [_item(i, "正" * 900) for i in range(1, 7)]
    sizes = write_shard_sizes(heavy)
    assert sizes == [2, 2, 2], sizes
    # 单条就超预算也不丢条：自成一片。
    assert write_shard_sizes([_item(1, "正" * 5000)]) == [1]


def test_d031_片数上限把多出来的条目并进最后一片():
    from app.orchestrator.sectioning import WRITE_SHARD_MAX, write_shard_sizes

    sizes = write_shard_sizes([_item(i) for i in range(1, 61)])
    assert len(sizes) == WRITE_SHARD_MAX
    assert sum(sizes) == 60
    assert sizes == [10, 10, 10, 30]


def test_d031_片产物路径不是声明产物路径():
    from app.orchestrator.sectioning import write_shard_path

    assert write_shard_path(Path("/x/report/sec-1.md"), 2) == Path(
        "/x/report/sec-1.part.2.md"
    )


def test_d031_片合并_单结论单信息源且按链接去重():
    from app.report.markdown import merge_section_shards

    shard_1 = (
        "## 结论\n\n- 甲结论 [S03]\n\n"
        "## 信息源\n\n- [S03] [来源三](https://example.com/3)\n"
    )
    shard_2 = (
        "## 结论\n\n- 乙结论 [S03][S07]\n\n"
        "## 信息源\n\n- [S03] [来源三](https://example.com/3)\n"
        "- [S07] [来源七](https://example.com/7)\n"
    )
    merged = merge_section_shards(
        [shard_1, shard_2],
        citation_numbers={"https://example.com/3": 3, "https://example.com/7": 7},
    )
    assert merged.count("## 结论") == 1
    assert merged.count("## 信息源") == 1
    # 两片都引了 S03，信息源只留一条——重复条目会让 source_citations 直接抛。
    assert merged.count("[来源三](https://example.com/3)") == 1
    assert "- 甲结论 [S03]" in merged and "- 乙结论 [S03][S07]" in merged
    # 角标是证据的全局属性，合并不重排。
    assert "[S07]" in merged
    # 片级合并产出的是节不是整卷：不写缺失清单。
    assert "缺失清单" not in merged


def test_d031_片合并_解析得出的角标与信息源一一对上():
    from app.report.markdown import merge_section_shards, source_citations

    shards = [
        "## 结论\n\n- 甲 [S01]\n\n## 信息源\n\n- [S01] [一](https://example.com/1)\n",
        "## 结论\n\n- 乙 [S02]\n\n## 信息源\n\n- [S02] [二](https://example.com/2)\n",
    ]
    merged = merge_section_shards(shards)
    assert source_citations(merged) == {
        "https://example.com/1": 1, "https://example.com/2": 2,
    }


def test_d031_片合并_保留结论信息源之外的正文():
    from app.report.markdown import merge_section_shards

    shards = [
        "# 节标题\n\n## 证据缺口\n\n- 未覆盖 X\n\n"
        "## 结论\n\n- 甲 [S01]\n\n## 信息源\n\n- [S01] [一](https://example.com/1)\n",
        "## 结论\n\n- 乙 [S02]\n\n## 信息源\n\n- [S02] [二](https://example.com/2)\n",
    ]
    merged = merge_section_shards(shards)
    # 证据缺口只有第 1 片写，合并后仍在结论之前。
    assert merged.index("## 证据缺口") < merged.index("## 结论")
    assert "# 节标题" in merged


def _shard_run(
    tmp_path, *, evidence: int, fail_shards=(), seed_parts=None, wall_clock=None,
    flaky_shards=(), stale_done=False,
):
    """一节切片跑一趟：假引擎按片写产物，fail_shards 里的片故意不落盘。"""
    import asyncio
    import json
    from types import SimpleNamespace

    from app.adapters import validation
    from app.adapters.capability import Capability, FileSystemScope
    from app.adapters.contracts import EngineRunResult, EngineTask, OwliResult
    from app.orchestrator.sectioning import run_sectioned_task
    from tests.test_m3h_ledger import _store

    store = _store(tmp_path)
    runs_root = tmp_path / "runs"
    for index in range(1, evidence + 1):
        store.add_evidence(
            id=f"ev-{index:03d}", report_id="r-ledger", goal_id="goal-1",
            platform="xhs", permalink=f"https://example.com/{index:03d}",
            fetched_at="2026-09-02T00:00:00Z", title=f"来源 {index}",
            content_excerpt="可复核正文",
        )
    plan = SimpleNamespace(
        research_id="r-ledger", title="分片报告",
        goals=[SimpleNamespace(goal_id="goal-1", title="goal-1")],
    )
    agent = SimpleNamespace(
        output={"shape": "object"},
        chapter={"chapter_id": "ch-report", "opening": {"inputs": []}},
    )
    output = runs_root / "r-ledger" / "goals/goal-1/report.md"
    if seed_parts:
        section_root = output.parent / output.stem
        section_root.mkdir(parents=True, exist_ok=True)
        for index, markdown in seed_parts.items():
            (section_root / f"sec-1.part.{index}.md").write_text(
                json.dumps({"markdown": markdown, "claims": []}, ensure_ascii=False),
                encoding="utf-8",
            )
    if stale_done:
        # 已 done 的节 + 一份引用了库外链接的片产物：角标解析不了，
        # run_sectioned_task 会把这一节复位重写。
        section_root = output.parent / output.stem
        section_root.mkdir(parents=True, exist_ok=True)
        body = json.dumps({
            "markdown": "## 结论\n\n- 过期正文 [S01]\n\n"
                        "## 信息源\n\n- [S01] [旧](https://gone.example/1)\n",
            "claims": [],
        }, ensure_ascii=False)
        (section_root / "sec-1.part.1.md").write_text(body, encoding="utf-8")
        (section_root / "sec-1.md").write_text(body, encoding="utf-8")
        store.ensure_chapters(
            "r-ledger", [{"goal_id": "goal-1", "chapter_id": "ch-report/sec-1"}],
            updated_at="2026-09-02T00:00:00Z",
        )
        store.finish_chapter(
            "r-ledger", "goal-1", "ch-report/sec-1", status="done", reason=None,
            actual_output_path=str(section_root / "sec-1.md"), actual_count=1,
            updated_at="2026-09-02T00:00:01Z",
        )
    task = EngineTask(
        body="写报告", output_path=output, output_format="markdown",
        research_id="r-ledger", goal_id="goal-1", agent_id="report-writing",
        agent_kind="report_writing",
        validators=["file_exists", "sections_exist:结论,信息源",
                    "citation_marks_resolvable", "no_orphan_citation"],
        capability=Capability(
            tools=("fs.write",),
            fs=FileSystemScope(write=("goals/goal-1/**",)),
        ),
    )
    bodies: dict[str, str] = {}
    events: list[dict] = []
    seen: dict[int, int] = {}

    class Adapter:
        async def run(self, shard_task, ctx, on_event=None):
            del on_event
            name = shard_task.output_path.name
            bodies[name] = shard_task.body
            pool = _pool_from_body(shard_task.body)
            shard = int(name.split(".part.")[1].split(".")[0]) if ".part." in name else 1
            seen[shard] = seen.get(shard, 0) + 1
            if shard in fail_shards or (
                shard in flaky_shards and seen[shard] == 1
            ):
                return EngineRunResult(
                    conclusion=None, conclusion_error="socket closed",
                    validation=validation.ValidationReport(
                        validation.Verdict.FAIL,
                        [validation.Result(
                            validation.Verdict.FAIL, "file_exists", "missing", [],
                        )],
                    ),
                    events=[], permission_denials=[],
                    engine_error="socket connection was closed unexpectedly",
                )
            item = pool["items"][0]
            markdown = (
                f"## 结论\n\n- 第 {shard} 片判断 {item['citation']}\n\n"
                f"## 信息源\n\n- {item['citation']} "
                f"[{item['title']}]({item['permalink']})\n"
            )
            shard_task.output_path.parent.mkdir(parents=True, exist_ok=True)
            shard_task.output_path.write_text(
                json.dumps(
                    {"markdown": markdown, "claims": [{"id": f"c-{shard:02d}01"}]},
                    ensure_ascii=False,
                ),
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
            goal_id="goal-1", engine="claude",
            section_deadline_seconds=wall_clock,
        ),
        base_task=task, adapter=Adapter(), store=store, runs_root=runs_root,
        now_iso=lambda: "2026-09-02T00:00:02Z", on_event=on_event,
        persist_goal_evidence=lambda _plan, _goal: None,
    ))
    return result, store, bodies, events, output


def test_d031_三十条池切三片_每片只喂十条_合并成一节(tmp_path):
    import json

    result, store, bodies, events, output = _shard_run(tmp_path, evidence=30)

    assert result.succeeded is True
    # 三次会话，各喂 10 条，池按原序不重不漏。
    shard_names = sorted(name for name in bodies if ".part." in name)
    assert shard_names == [f"sec-1.part.{k}.md" for k in (1, 2, 3)]
    citations = []
    for name in shard_names:
        pool = _pool_from_body(bodies[name])
        assert len(pool["items"]) == 10
        citations.extend(item["citation"] for item in pool["items"])
    assert citations == [f"[S{index:02d}]" for index in range(1, 31)]
    # 合并成一节：单结论、单信息源、三片的 claims 按片序拼上。
    section = json.loads(
        (output.parent / output.stem / "sec-1.md").read_text(encoding="utf-8")
    )
    assert section["markdown"].count("## 结论") == 1
    assert section["markdown"].count("## 信息源") == 1
    assert [claim["id"] for claim in section["claims"]] == [
        "c-0101", "c-0201", "c-0301",
    ]
    assert store.list_chapters("r-ledger")[0]["status"] == "done"
    merged = [event for event in events if event["type"] == "write_shards_merged"]
    assert merged and merged[0]["data"] == {
        "goal_id": "goal-1", "chapter_id": "ch-report/sec-1",
        "shards": 3, "done": 3,
    }


def test_d031_片提示词把本片口径讲死(tmp_path):
    _, _, bodies, _, _ = _shard_run(tmp_path, evidence=30)

    first = bodies["sec-1.part.1.md"]
    second = bodies["sec-1.part.2.md"]
    assert "你现在写第 1/3 片" in first and "你现在写第 2/3 片" in second
    assert "**只就这几条写**" in first
    assert "本片不写节标题行、不写总起或收束段落" in first
    # claims id 区间按片走，跨片不撞号。
    assert "c-0101、c-0102" in first and "c-0201、c-0202" in second
    # 全节口径的段落只让第 1 片写。
    assert "只在本片（第 1 片）写一次" in first
    assert "已由第 1 片写过" in second
    # 片间衔接：后续片拿到前面片已写条目，明说别重复。
    assert "不要重复、不要再引它们的角标" in second
    assert "- 第 1 片判断 [S01]" in second


def test_d031_池不足一片时不分片_提示词不出现本片段(tmp_path):
    """回归锚：≤10 条走分片前那条路，正文里连【本片】两个字都没有。"""
    result, _, bodies, events, _ = _shard_run(tmp_path, evidence=8)

    assert result.succeeded is True
    assert list(bodies) == ["sec-1.md"]
    assert "【本片】" not in bodies["sec-1.md"]
    assert not [e for e in events if e["type"].startswith("write_shard")]


def test_d031_一片失败_其余片照跑并合并落盘_节尝试判失败(tmp_path):
    """M6-e 那轮的病：跑满墙钟然后什么都没留下。分片后 2/3 的字要留在盘上。"""
    import json

    result, store, bodies, events, output = _shard_run(
        tmp_path, evidence=30, fail_shards=(2,),
    )

    # 第 2 片断了，第 3 片照跑——不是「一片失败作废整节」。
    assert sorted(name for name in bodies if ".part." in name) == [
        f"sec-1.part.{k}.md" for k in (1, 2, 3)
    ]
    finished = [e["data"] for e in events if e["type"] == "write_shard_finished"]
    # 第一次节尝试跑满三片；断连属节级可重试，后两次尝试只补第 2 片
    # （1、3 直接跳过），这正是分片要换来的东西：已写的字不重写。
    assert [
        (item["shard"], item["succeeded"], item["attempts"]) for item in finished
    ] == [
        (1, True, 1), (2, False, 3), (3, True, 1),
        (2, False, 3), (2, False, 3),
    ]
    assert [
        e["data"]["shard"] for e in events if e["type"] == "write_shard_skipped"
    ] == [1, 3, 1, 3]
    assert finished[1]["reason"] is not None
    assert all(item["elapsed_seconds"] >= 0 for item in finished)
    # 成功的两片已合并落盘，供下一次节尝试跳过。
    section_root = output.parent / output.stem
    assert {
        path.name for path in section_root.glob("sec-1.part.*.md")
    } == {"sec-1.part.1.md", "sec-1.part.3.md"}
    merged = [e["data"] for e in events if e["type"] == "write_shards_merged"]
    assert [item["done"] for item in merged] == [2, 2, 2]
    # 节最终判失败后，节产物按既有口径落占位（_finish 那段不动）；
    # 但两片真正写出来的字仍在片产物里，下一次节尝试拿它跳过、不重写。
    assert "第 1 片判断" in json.loads(
        (section_root / "sec-1.part.1.md").read_text(encoding="utf-8")
    )["markdown"]
    # 节仍判失败：有片没成，本次节尝试就不算成。
    assert result.succeeded is False
    row = store.list_chapters("r-ledger")[0]
    assert row["status"] == "missing"


def test_d031_节级重试只重跑失败片(tmp_path):
    """盘上已成的片直接跳过，不重写、不重复烧会话。"""
    seeded = {
        1: "## 结论\n\n- 上一轮第 1 片 [S01]\n\n"
           "## 信息源\n\n- [S01] [来源 1](https://example.com/001)\n",
        3: "## 结论\n\n- 上一轮第 3 片 [S21]\n\n"
           "## 信息源\n\n- [S21] [来源 21](https://example.com/021)\n",
    }
    result, _, bodies, events, _ = _shard_run(
        tmp_path, evidence=30, seed_parts=seeded,
    )

    assert result.succeeded is True
    # 只有第 2 片起了会话。
    assert list(bodies) == ["sec-1.part.2.md"]
    skipped = [e["data"]["shard"] for e in events if e["type"] == "write_shard_skipped"]
    assert skipped == [1, 3]
    # 跳过的片仍参与合并，上一轮写的字没丢。
    merged = [e["data"] for e in events if e["type"] == "write_shards_merged"]
    assert merged[0]["done"] == 3


def test_d031_片提示词把产物路径指到本片而不是节(tmp_path):
    """第一次重放实测的坑：路径句还写着节路径，写手把整份信封写进了 sec-1.md。"""
    _, _, bodies, _, output = _shard_run(tmp_path, evidence=30)

    section_root = output.parent / output.stem
    for shard in (1, 2, 3):
        body = bodies[f"sec-1.part.{shard}.md"]
        assert str(section_root / f"sec-1.part.{shard}.md") in body
        assert f"{section_root / 'sec-1.md'}\n" not in body


def test_d031_片提示词把结论条数钉在本片证据条数上_且硬约束在最末(tmp_path):
    """第三轮重放实证：同一句话写在正文中段会被无视——5 条证据的片照写
    10 条结论、5.3 KB、288 s（离 300 s 只剩 12 s）。硬约束要放在最末。"""
    _, _, bodies, _, _ = _shard_run(tmp_path, evidence=30)

    first = bodies["sec-1.part.1.md"]
    assert "**不超过 10 条**" in first
    assert "【本片硬约束，写之前再读一遍】" in first
    assert "`## 结论` 列表项**最多 10 条**" in first
    assert "全节口径的三段合计不超过 6 行" in first
    # 硬约束必须是正文最后一段，别被信封示例挤到中间去。
    assert first.rstrip().endswith("全节口径的三段合计不超过 6 行。")
    assert "全节口径的三段" not in bodies["sec-1.part.2.md"].split("【本片硬约束")[1]


def test_d031_每片一份自己的墙钟_不共用节那一个绝对时刻(tmp_path, monkeypatch):
    """共用的话第 1 片跑掉大半、后面几片分残额必全灭。"""
    import app.orchestrator.sectioning as sectioning

    deadlines: list[float] = []
    original = sectioning._run_before_section_deadline

    async def spy(adapter, task, ctx, on_event, deadline):
        deadlines.append(deadline)
        return await original(adapter, task, ctx, on_event, deadline)

    monkeypatch.setattr(sectioning, "_run_before_section_deadline", spy)
    _shard_run(tmp_path, evidence=30, wall_clock=330.0)

    assert len(deadlines) == 3
    # 三片各拿一个自己的绝对时刻，且逐片后移——不是同一个数。
    assert len(set(deadlines)) == 3
    assert deadlines == sorted(deadlines)


def test_d031_断连片在本片墙钟内重试一次即成功_不占节级重试(tmp_path):
    """断连水位约 0.3 次/分钟，一节四片跑近十分钟必撞好几次——
    掉到节级重试去会把 3 次节尝试烧光（第二轮重放实证）。"""
    result, store, bodies, events, _ = _shard_run(
        tmp_path, evidence=30, flaky_shards=(2,), wall_clock=330.0,
    )

    assert result.succeeded is True
    retries = [e["data"] for e in events if e["type"] == "write_shard_retry"]
    assert [(item["shard"], item["attempt"]) for item in retries] == [(2, 2)]
    finished = [e["data"] for e in events if e["type"] == "write_shard_finished"]
    # 三片各一条 finished：断连那次在片内消化掉，没冒到节级。
    assert [(item["shard"], item["succeeded"], item["attempts"]) for item in finished] == [
        (1, True, 1), (2, True, 2), (3, True, 1),
    ]
    assert not [e for e in events if e["type"] == "section_retry"]
    assert store.list_chapters("r-ledger")[0]["status"] == "done"


def test_d031_角标失效复位时片产物一起删_不把过期正文合并回来(tmp_path):
    """节级重试留着片产物是为了不重写；但「已 done 的节角标失效」这一支
    正文本身过期了，片留着只会被原样合并回来。"""
    result, _, _, _, output = _shard_run(
        tmp_path, evidence=30, stale_done=True,
    )

    section_root = output.parent / output.stem
    stale = section_root / "sec-1.part.1.md"
    assert result.succeeded is True
    # 过期片被删掉后重跑，正文里不会再有那句过期话。
    import json

    assert "过期正文" not in stale.read_text(encoding="utf-8")
    assert "过期正文" not in json.dumps(
        json.load((section_root / "sec-1.md").open(encoding="utf-8")),
        ensure_ascii=False,
    )
