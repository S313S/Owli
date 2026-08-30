"""§RATE-3 货 1：切片发生在物化那一刻——每片 ≤50 行写成 `x.rows.<n>.json`。

RATE-2 实测：fast 档 330 s 墙钟下 130 行的评级章算术上过不去（4.4 s/条），
本机代理还把单次流式响应掐在 5 分钟——「一次调用评 130 条」这条路本身是死的。
判据：135 行 → 3 片（50/50/35）；片数与每片行数进 `rating_rows_materialized` 事件。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from tests.test_c1_claims import make_store
from tests.test_rate2_materialize_rows import _collector, _rating


def test_按批大小切片_135行切成50_50_35() -> None:
    from app.plan.model import rating_batch_output_path, rating_batch_path, rating_batches

    assert rating_batches(135) == [50, 50, 35]
    assert rating_batches(50) == [50]
    assert rating_batches(25) == [25]
    assert rating_batches(0) == []
    assert rating_batches(130, 30) == [30, 30, 30, 30, 10]
    assert rating_batch_path("goals/goal-1/data-collection.rows.json", 2) == (
        "goals/goal-1/data-collection.rows.2.json"
    )
    assert rating_batch_output_path("goals/goal-1/reliability-audit.json", 3) == (
        "goals/goal-1/reliability-audit.part.3.json"
    )


def _fixture(tmp_path: Path, rows: int, collector_id: str = "data-collection-2"):
    from app.orchestrator.runtime import RuntimeCoordinator

    store = make_store(tmp_path, "r-rate3")
    for index in range(rows):
        store.add_evidence(
            id=f"ev-{index}", report_id="r-rate3", goal_id="goal-1",
            agent_name=collector_id, platform="web_search",
            permalink=f"https://example.com/{index}",
            fetched_at="2026-08-31T00:00:00+00:00",
            published_at="2026-08-20T00:00:00+00:00",
            title=f"标题{index}", content_excerpt="可复核正文",
            author_name=f"作者{index}", fetch_method="search_index",
        )
    events: list[dict] = []

    async def publish(research_id, payload):
        events.append(payload)

    coordinator = RuntimeCoordinator(
        store=store, event_buffer=SimpleNamespace(publish=publish), researches={},
        cards={}, runs_root=tmp_path / "runs",
        routing_utc_clock=lambda: datetime(2026, 8, 31, tzinfo=timezone.utc),
    )
    collector, rating = _collector(collector_id), _rating(collector_id)
    # `_task` 要用到的最小字段：goal.objective / agent.prompt / model / chapter。
    rating.prompt = {"body": "逐条评级"}
    rating.model = None
    rating.chapter = None
    goal = SimpleNamespace(
        goal_id="goal-1", agents=[collector, rating], objective="口碑调研",
        acceptance=[], deliverable={"path": "goals/goal-1/result.md"},
    )
    plan = SimpleNamespace(research_id="r-rate3", goals=[goal], scale="fast")
    artifact = tmp_path / "runs" / "r-rate3" / "goals" / "goal-1"
    return coordinator, plan, rating, events, artifact


def test_物化时切片_135行写成3片_事件带片数与每片行数(tmp_path: Path) -> None:
    coordinator, plan, rating, events, artifact = _fixture(tmp_path, 135)

    written = asyncio.run(coordinator._materialize_rating_rows(plan, "goal-1", rating))

    assert written == 135
    sizes = [
        len(json.loads((artifact / f"data-collection-2.rows.{n}.json").read_text("utf-8")))
        for n in (1, 2, 3)
    ]
    assert sizes == [50, 50, 35]
    assert not (artifact / "data-collection-2.rows.4.json").exists()
    # 片是整文件的顺序切分：拼回去就是整文件，不去重、不改写。
    whole = json.loads((artifact / "data-collection-2.rows.json").read_text("utf-8"))
    pieces = [
        item for n in (1, 2, 3)
        for item in json.loads(
            (artifact / f"data-collection-2.rows.{n}.json").read_text("utf-8")
        )
    ]
    assert pieces == whole
    data = next(e["data"] for e in events if e["type"] == "rating_rows_materialized")
    assert data["batches"] == 3 and data["batch_rows"] == [50, 50, 35]
    assert data["batch_size"] == 50 and data["rows"] == 135


def test_重物化时行数变少_多出来的旧片要删掉(tmp_path: Path) -> None:
    coordinator, plan, rating, _, artifact = _fixture(tmp_path, 60)
    artifact.mkdir(parents=True, exist_ok=True)
    for n in (3, 4):
        (artifact / f"data-collection-2.rows.{n}.json").write_text("[]", "utf-8")
        (artifact / f"reliability-audit.part.{n}.json").write_text("[]", "utf-8")

    asyncio.run(coordinator._materialize_rating_rows(plan, "goal-1", rating))

    assert (artifact / "data-collection-2.rows.2.json").is_file()
    for n in (3, 4):
        assert not (artifact / f"data-collection-2.rows.{n}.json").exists()
        assert not (artifact / f"reliability-audit.part.{n}.json").exists()


def test_批大小可由环境变量下调_非法值回默认(monkeypatch) -> None:
    from app.orchestrator.runtime import RuntimeCoordinator

    monkeypatch.setenv("OWLI_RATING_BATCH_ROWS", "30")
    assert RuntimeCoordinator._rating_batch_rows() == 30
    monkeypatch.setenv("OWLI_RATING_BATCH_ROWS", "abc")
    assert RuntimeCoordinator._rating_batch_rows() == 50
    monkeypatch.setenv("OWLI_RATING_BATCH_ROWS", "0")
    assert RuntimeCoordinator._rating_batch_rows() == 50


# ---------------------------------------------------------------- 货 2：按片跑


class _BatchAdapter:
    """假引擎：每次会话只读它被指到的那一片，逐条回评写到片产物路径。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.fail_batches: set[int] = set()   # 每次都传输断连
        self.fail_once: set[int] = set()      # 第一次传输断连、第二次正常
        self.hang: set[int] = set()           # 睡过片墙钟
        self.cancel_on: set[int] = set()      # 模拟章墙钟 / stop 取消

    async def run(self, task, ctx, on_event=None):
        piece_in = next(
            line for line in task.body.splitlines() if "【本批】" in line
        )
        index = int(piece_in.split("这是第 ")[1].split("/")[0])
        src = task.output_path.parent / (
            task.output_path.name.split(".part.")[0].replace(
                "reliability-audit", "data-collection-2"
            )
        )
        rows = json.loads(
            (src.with_name(f"data-collection-2.rows.{index}.json")).read_text("utf-8")
        )
        self.calls.append({
            "index": index, "output": task.output_path.name, "rows": len(rows),
            "validators": list(task.validators),
        })
        if index in self.cancel_on:
            raise asyncio.CancelledError
        if index in self.hang:
            await asyncio.sleep(0.5)
        transport_fail = index in self.fail_batches or index in self.fail_once
        self.fail_once.discard(index)
        if transport_fail:
            return SimpleNamespace(
                succeeded=False, conclusion=None, events=[],
                engine_error="socket hang up", conclusion_error=None,
                validation=SimpleNamespace(results=[]),
            )
        notes = ("权威2:平台原帖 · 时效2:时间窗内 · 交叉1:弱交叉 · "
                 "完整2:字段齐全 · 无关2:无利益关系")
        task.output_path.write_text(json.dumps([
            {"permalink": row["permalink"], "score_authority": 2,
             "score_freshness": 2, "score_crossref": 1, "score_completeness": 2,
             "score_independence": 2, "rating_notes": notes,
             "rated_by": "reliability-audit"}
            for row in rows
        ], ensure_ascii=False), encoding="utf-8")
        return SimpleNamespace(
            succeeded=True, events=[], engine_error=None, conclusion_error=None,
            conclusion=SimpleNamespace(
                reason=None, status="done", unmet=[], output_path=str(task.output_path),
            ),
            validation=SimpleNamespace(results=[]),
        )


def _run_chapter(coordinator, plan, rating, adapter, *, attempt=1, wall=330.0):
    coordinator._adapters[plan.research_id] = adapter

    async def sink(event):
        return None

    return asyncio.run(coordinator._run_task(
        plan, rating,
        SimpleNamespace(goal_id="goal-1", attempt=attempt, engine="claude",
                        failure_feedback=None, on_event=sink,
                        section_deadline_seconds=wall, deadline_at=None),
    ))


def test_评级章按片跑_3片3次会话_合并后条数135_全部入库(tmp_path: Path) -> None:
    coordinator, plan, rating, events, artifact = _fixture(tmp_path, 135)
    adapter = _BatchAdapter()

    result = _run_chapter(coordinator, plan, rating, adapter)

    assert result.succeeded is True and result.actual_count == 135
    assert [c["index"] for c in adapter.calls] == [1, 2, 3]
    assert [c["rows"] for c in adapter.calls] == [50, 50, 35]
    assert [c["output"] for c in adapter.calls] == [
        f"reliability-audit.part.{n}.json" for n in (1, 2, 3)
    ]
    # 每片走的是本章原封不动的那套验证器（D-028 闸在入库时逐条走，见下）。
    assert adapter.calls[0]["validators"] == rating.output["validators"]
    merged = json.loads((artifact / "reliability-audit.json").read_text("utf-8"))
    whole = json.loads((artifact / "data-collection-2.rows.json").read_text("utf-8"))
    assert [item["permalink"] for item in merged] == [
        row["permalink"] for row in whole
    ], "按片序合并、按 permalink 原样回带、不去重不改写——顺序就是物化文件的顺序"
    assert len(set(item["permalink"] for item in merged)) == 135
    rated = [
        row for row in coordinator.store.list_evidence("r-rate3")
        if row["rated_by"] == "agent:reliability-audit"
    ]
    assert len(rated) == 135
    started = [e["data"] for e in events if e["type"] == "rating_batch_started"]
    assert [s["wall_clock_seconds"] for s in started] == [330.0] * 3, "每片一份自己的墙钟"
    finished = [e["data"] for e in events if e["type"] == "rating_batch_finished"]
    assert [f["succeeded"] for f in finished] == [True, True, True]
    merged_event = next(e["data"] for e in events if e["type"] == "rating_batches_merged")
    assert merged_event == {
        "goal_id": "goal-1", "agent_id": "reliability-audit",
        "batches": 3, "done": [1, 2, 3], "items": 135, "failed": None,
    }


def test_单片章_25行只跑1次会话_行为与分片前一致(tmp_path: Path) -> None:
    coordinator, plan, rating, _, artifact = _fixture(tmp_path, 25)
    adapter = _BatchAdapter()

    result = _run_chapter(coordinator, plan, rating, adapter)

    assert result.succeeded is True and result.actual_count == 25
    assert [c["index"] for c in adapter.calls] == [1]
    assert (artifact / "reliability-audit.json").is_file()


def test_调度器按片给墙钟_每片330s_章预算330x片数_口径同节化章() -> None:
    """§FIX「墙钟按节计」的同一口径：section_deadline_seconds = 章墙钟，
    章预算 = 章墙钟 × 片数；片数由 runtime 在派活前回答（物化那一刻才知道行数）。"""
    from datetime import timedelta

    from app.orchestrator.scheduler import Scheduler, TaskRunResult
    from app.plan.model import Plan
    from tests.plan_factory import make_plan_dict

    raw = make_plan_dict()
    raw["goals"] = raw["goals"][:1]
    raw["goals"][0]["agents"][0]["agent_id"] = "reliability-audit"
    raw["goals"][0]["retry_policy"]["chapter_deadline_seconds"] = 330
    plan = Plan.from_dict(raw)
    seen: list = []
    asked: list = []
    now = datetime(2026, 8, 31, tzinfo=timezone.utc)

    async def run_task(agent, context):
        seen.append(context)
        return TaskRunResult(True, engine="claude")

    async def batch_count(agent):
        asked.append(agent.agent_id)
        return 3

    scheduler = Scheduler(
        plan, run_task, lambda event: None, lambda: now,
        lambda delay, callback: None, batch_count=batch_count,
    )
    goal = plan.goals[0]
    scheduler.goal_statuses[goal.goal_id] = "running"
    asyncio.run(scheduler._execute_agent(goal, goal.agents[0]))

    assert asked == ["reliability-audit"]
    context = seen[0]
    assert context.section_deadline_seconds == 330
    assert context.deadline_at == now + timedelta(seconds=330 * 3)


def test_调度器_非评级章或零片时墙钟原样不变() -> None:
    from datetime import timedelta

    from app.orchestrator.scheduler import Scheduler, TaskRunResult
    from app.plan.model import Plan
    from tests.plan_factory import make_plan_dict

    raw = make_plan_dict()
    raw["goals"] = raw["goals"][:1]
    raw["goals"][0]["agents"][0]["agent_id"] = "reliability-audit"
    raw["goals"][0]["retry_policy"]["chapter_deadline_seconds"] = 330
    plan = Plan.from_dict(raw)
    seen: list = []
    now = datetime(2026, 8, 31, tzinfo=timezone.utc)

    async def run_task(agent, context):
        seen.append(context)
        return TaskRunResult(True, engine="claude")

    scheduler = Scheduler(
        plan, run_task, lambda event: None, lambda: now,
        lambda delay, callback: None, batch_count=lambda agent: 0,
    )
    goal = plan.goals[0]
    scheduler.goal_statuses[goal.goal_id] = "running"
    asyncio.run(scheduler._execute_agent(goal, goal.agents[0]))

    assert seen[0].section_deadline_seconds is None
    assert seen[0].deadline_at == now + timedelta(seconds=330)


# ------------------------------------------------- 货 3：片级失败与重试语义（本包拍）


def test_片内传输断连在本片墙钟内重试一次即成功_整章仍done(tmp_path: Path) -> None:
    coordinator, plan, rating, events, _ = _fixture(tmp_path, 135)
    plan.scale = "unit"  # 退避间隔表里没有 → 0 s，夹具不用真等
    adapter = _BatchAdapter()
    adapter.fail_once = {2}

    result = _run_chapter(coordinator, plan, rating, adapter)

    assert result.succeeded is True and result.actual_count == 135
    assert [c["index"] for c in adapter.calls] == [1, 2, 2, 3], "只重跑断连那一片"
    started = [e["data"] for e in events if e["type"] == "rating_batch_started"]
    assert [(s["batch"], s["attempt"]) for s in started] == [(1, 1), (2, 1), (2, 2), (3, 1)]


def test_一片超出本片墙钟_其余片照跑并入库_本次章尝试失败(tmp_path: Path) -> None:
    coordinator, plan, rating, events, artifact = _fixture(tmp_path, 135)
    adapter = _BatchAdapter()
    adapter.hang = {2}

    result = _run_chapter(coordinator, plan, rating, adapter, wall=0.05)

    assert result.succeeded is False
    assert str(result.engine_error).startswith("timeout: 评级第 2/3 批超出批墙钟")
    assert [c["index"] for c in adapter.calls] == [1, 2, 3], "一片失败不作废整章，后面的片照跑"
    merged = json.loads((artifact / "reliability-audit.json").read_text("utf-8"))
    assert len(merged) == 85, "成功的两片先合并"
    rated = [r for r in coordinator.store.list_evidence("r-rate3")
             if r["rated_by"] == "agent:reliability-audit"]
    assert len(rated) == 85, "成功的片先入库，不等整章"
    finished = [e["data"] for e in events if e["type"] == "rating_batch_finished"]
    assert [(f["batch"], f["succeeded"], f["reason"]) for f in finished] == [
        (1, True, None), (2, False, "timeout"), (3, True, None),
    ]
    merged_event = next(e["data"] for e in events if e["type"] == "rating_batches_merged")
    assert merged_event["done"] == [1, 3] and merged_event["failed"] == 1


def test_章级重试只重跑失败片_已成功的片不重评(tmp_path: Path) -> None:
    coordinator, plan, rating, events, artifact = _fixture(tmp_path, 135)
    first = _BatchAdapter()
    first.hang = {2}
    assert _run_chapter(coordinator, plan, rating, first, wall=0.05).succeeded is False

    second = _BatchAdapter()
    result = _run_chapter(coordinator, plan, rating, second, attempt=2)

    assert result.succeeded is True and result.actual_count == 135
    assert [c["index"] for c in second.calls] == [2], "第 2 轮只跑第 2 片"
    skipped = [e["data"]["batch"] for e in events if e["type"] == "rating_batch_skipped"]
    assert skipped == [1, 3]
    rated = [r for r in coordinator.store.list_evidence("r-rate3")
             if r["rated_by"] == "agent:reliability-audit"]
    assert len(rated) == 135


def test_章墙钟取消掐在片中_已成功的片合并入库不白丢(tmp_path: Path) -> None:
    import pytest

    coordinator, plan, rating, _, artifact = _fixture(tmp_path, 135)
    adapter = _BatchAdapter()
    adapter.cancel_on = {2}

    with pytest.raises(asyncio.CancelledError):
        _run_chapter(coordinator, plan, rating, adapter)

    merged = json.loads((artifact / "reliability-audit.json").read_text("utf-8"))
    assert len(merged) == 50
    rated = [r for r in coordinator.store.list_evidence("r-rate3")
             if r["rated_by"] == "agent:reliability-audit"]
    assert len(rated) == 50
