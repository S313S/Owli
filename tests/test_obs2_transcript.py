"""§OBS-2 货 1：引擎原始事件逐节落 `<chapter>.transcript.jsonl`。"""

import asyncio
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.adapters.capability import Capability, FileSystemScope  # noqa: E402
from app.adapters.contracts import EngineTask  # noqa: E402
from app.adapters.transcript import (  # noqa: E402
    TranscriptWriter,
    chapter_key,
    transcript_path,
)


def _task(runs_root: Path, output: Path, goal_id: str = "goal-1") -> EngineTask:
    return EngineTask(
        body="写", output_path=output, output_format="markdown",
        research_id="r-obs2", goal_id=goal_id, agent_id="report-writing",
        agent_kind="report_writing", validators=["file_exists"],
        capability=Capability(
            tools=("fs.write",),
            fs=FileSystemScope(write=("goals/goal-1/**",)),
        ),
        runs_root=runs_root,
    )


def test_章标识_节与片都归到同一章(tmp_path: Path) -> None:
    goal = tmp_path / "r-obs2" / "goals" / "goal-1"
    chapter = _task(tmp_path, goal / "ch-3.md")
    section = _task(tmp_path, goal / "ch-3" / "sec-2.md")
    shard = _task(tmp_path, goal / "ch-3" / "sec-2.part.1.md")

    assert chapter_key(chapter) == "ch-3"
    assert chapter_key(section) == "ch-3"
    assert chapter_key(shard) == "ch-3"
    assert transcript_path(shard) == goal / "ch-3.transcript.jsonl"


def test_落盘行数等于原始事件数且_seq_连续(tmp_path: Path) -> None:
    goal = tmp_path / "r-obs2" / "goals" / "goal-1"
    writer = TranscriptWriter(_task(tmp_path, goal / "ch-3.md"), engine="Codex")
    raws = [
        {"type": "thread.started", "thread_id": "t-1"},
        {"type": "item.completed", "item": {"type": "command_execution"}},
        "非 JSON 的裸行",
    ]
    for raw in raws:
        writer.append(raw)

    lines = (goal / "ch-3.transcript.jsonl").read_text("utf-8").splitlines()
    assert len(lines) == len(raws)
    records = [json.loads(line) for line in lines]
    assert [record["seq"] for record in records] == [1, 2, 3]
    assert [record["event"] for record in records] == raws
    assert records[0]["engine"] == "Codex"
    assert records[0]["output"] == "ch-3.md"
    assert all(isinstance(record["ts"], float) for record in records)


def test_同一章的第二只写手接着上一条_seq_续写(tmp_path: Path) -> None:
    goal = tmp_path / "r-obs2" / "goals" / "goal-1"
    first = TranscriptWriter(_task(tmp_path, goal / "ch-3" / "sec-1.md"), engine="Codex")
    first.append({"type": "turn.started"})
    first.append({"type": "turn.completed"})
    second = TranscriptWriter(_task(tmp_path, goal / "ch-3" / "sec-2.md"), engine="Codex")
    second.append({"type": "turn.started"})

    records = [
        json.loads(line)
        for line in (goal / "ch-3.transcript.jsonl").read_text("utf-8").splitlines()
    ]
    assert [record["seq"] for record in records] == [1, 2, 3]
    assert records[-1]["output"] == "sec-2.md"


def test_不可序列化的_SDK_对象落_repr_不抛(tmp_path: Path) -> None:
    goal = tmp_path / "r-obs2" / "goals" / "goal-1"
    writer = TranscriptWriter(_task(tmp_path, goal / "ch-3.md"), engine="Claude")

    class Message:
        def __repr__(self) -> str:
            return "<ResultMessage session_id=abc>"

    writer.append(Message())
    record = json.loads((goal / "ch-3.transcript.jsonl").read_text("utf-8").strip())
    assert record["event"] == "<ResultMessage session_id=abc>"


def _stream(lines: list[str]) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    for line in lines:
        reader.feed_data((line + "\n").encode("utf-8"))
    reader.feed_eof()
    return reader


_CODEX_LINES = [
    json.dumps({"type": "thread.started", "thread_id": "t-1"}),
    json.dumps({"type": "turn.started", "turn_id": "turn-1"}),
    json.dumps({
        "type": "item.completed",
        "item": {"type": "agent_message", "text": "写完了"},
    }),
    json.dumps({"type": "turn.completed"}),
]


def _consume_codex(task: EngineTask, lines: list[str]) -> list:
    from app.adapters.codex import CodexAdapter

    adapter = CodexAdapter()
    events: list = []

    async def drive() -> None:
        await adapter._consume(
            _stream(lines), events, None, TranscriptWriter(task, engine="Codex")
        )

    asyncio.run(drive())
    return events


def test_codex_原始流落盘行数不少于归一化事件数(tmp_path: Path) -> None:
    goal = tmp_path / "r-obs2" / "goals" / "goal-1"
    task = _task(tmp_path, goal / "ch-3.md")
    events = _consume_codex(task, _CODEX_LINES)

    lines = (goal / "ch-3.transcript.jsonl").read_text("utf-8").splitlines()
    assert len(lines) == len(_CODEX_LINES)
    # 原始比归一化只多不少（判据 1）。
    assert len(lines) >= len(events)
    assert json.loads(lines[0])["event"]["type"] == "thread.started"


def test_落盘失败不中断节_事件照常归一化(tmp_path: Path, monkeypatch) -> None:
    goal = tmp_path / "r-obs2" / "goals" / "goal-1"
    task = _task(tmp_path, goal / "ch-3.md")
    real_open = Path.open

    def refuse(self: Path, *args, **kwargs):
        if self.name.endswith(".transcript.jsonl"):
            raise OSError(28, "No space left on device")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", refuse)
    events = _consume_codex(task, _CODEX_LINES)

    assert not (goal / "ch-3.transcript.jsonl").exists()
    assert events, "写盘失败不许吃掉归一化事件"
    assert any(event.text == "写完了" for event in events)


def _transcript_endpoint(app):
    return next(
        route.endpoint for route in app.routes
        if getattr(route, "path", None)
        == "/api/researches/{research_id}/sections/{goal_id}/{chapter}/transcript"
    )


def _app_with_transcript(tmp_path: Path):
    from app.api.main import create_app

    runs_root = tmp_path / "runs"
    goal = runs_root / "r-obs2" / "goals" / "goal-1"
    writer = TranscriptWriter(_task(runs_root, goal / "ch-3.md"), engine="Codex")
    for index in range(1, 6):
        writer.append({"type": "item.completed", "n": index})
    app = create_app(
        tmp_path / "owli.db", ROOT / "app" / "store" / "schema.sql",
        runs_root=runs_root,
        engine_probe=lambda: {"claude": {"status": "available"}},
    )
    return app, goal


def test_transcript_接口_尾行与增量(tmp_path: Path) -> None:
    app, _ = _app_with_transcript(tmp_path)
    endpoint = _transcript_endpoint(app)

    async def call(**kwargs):
        async with app.router.lifespan_context(app):
            return await endpoint("r-obs2", "goal-1", "ch-3", **kwargs)

    body = asyncio.run(call(tail=2, after_seq=None))
    assert body["ok"] and body["data"]["last_seq"] == 5
    assert [line["event"]["n"] for line in body["data"]["lines"]] == [4, 5]
    assert body["data"]["size_bytes"] > 0

    incremental = asyncio.run(call(tail=200, after_seq=3))
    assert [line["seq"] for line in incremental["data"]["lines"]] == [4, 5]
    assert incremental["data"]["last_seq"] == 5

    caught_up = asyncio.run(call(tail=200, after_seq=5))
    assert caught_up["data"]["lines"] == []
    assert caught_up["data"]["last_seq"] == 5


def test_transcript_接口_文件不存在返回空数组而非404(tmp_path: Path) -> None:
    app, _ = _app_with_transcript(tmp_path)
    endpoint = _transcript_endpoint(app)

    async def call(chapter: str):
        async with app.router.lifespan_context(app):
            return await endpoint("r-obs2", "goal-1", chapter, tail=200, after_seq=None)

    body = asyncio.run(call("ch-never-ran"))
    assert body["ok"] and body["data"] == {"lines": [], "last_seq": 0, "size_bytes": 0}
    # 越界路径同样只回空，不许读到 runs 根之外。
    assert asyncio.run(call("..%2Fetc%2Fpasswd"))["data"]["lines"] == []


def _publisher(runs_root: Path, clock):
    from app.api.events import ResearchEventBuffer, SectionHeartbeatPublisher

    buffer = ResearchEventBuffer()
    publisher = SectionHeartbeatPublisher(
        buffer, runs_root, lambda: ["r-obs2"], clock=clock,
    )
    return buffer, publisher


def test_心跳_每20条或每15秒发一次且_elapsed_递增(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    goal = runs_root / "r-obs2" / "goals" / "goal-1"
    writer = TranscriptWriter(_task(runs_root, goal / "ch-3.md"), engine="Codex")
    ticks = [0.0]
    _, publisher = _publisher(runs_root, lambda: ticks[0])

    async def scenario() -> list[list[dict]]:
        rounds: list[list[dict]] = []
        writer.append({"type": "item.completed", "item": {"type": "web_search"}})
        rounds.append(await publisher.tick())          # 首条必发
        ticks[0] = 3.0
        writer.append({"type": "item.completed", "item": {"type": "web_search"}})
        rounds.append(await publisher.tick())          # 才多 1 条、才过 3 s：不发
        for _ in range(20):
            writer.append({"type": "item.completed", "item": {"type": "shell"}})
        ticks[0] = 6.0
        rounds.append(await publisher.tick())          # 多 20 条：发
        ticks[0] = 30.0
        writer.append({"type": "turn.completed"})
        rounds.append(await publisher.tick())          # 过 15 s：发
        ticks[0] = 33.0
        rounds.append(await publisher.tick())          # 没新事件：不发
        return rounds

    rounds = asyncio.run(scenario())
    assert [len(round_) for round_ in rounds] == [1, 0, 1, 1, 0]
    beats = [round_[0]["data"] for round_ in rounds if round_]
    assert [beat["elapsed_s"] for beat in beats] == [0.0, 6.0, 30.0]
    assert [beat["last_seq"] for beat in beats] == [1, 22, 23]
    assert [beat["step_hint"] for beat in beats] == ["web_search", "shell", "turn.completed"]
    assert beats[0]["goal"] == "goal-1" and beats[0]["chapter"] == "ch-3"
    assert beats[0]["engine"] == "Codex" and beats[0]["agent"] == "report-writing"


def test_心跳进_SSE_事件流(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    goal = runs_root / "r-obs2" / "goals" / "goal-2"
    writer = TranscriptWriter(
        _task(runs_root, goal / "ch-1.md", "goal-2"), engine="Claude"
    )
    writer.append({"type": "assistant", "subtype": "tool_use"})
    buffer, publisher = _publisher(runs_root, lambda: 1.0)

    async def scenario() -> str:
        buffer.bind_to_running_loop()
        await publisher.tick()
        batch = await buffer.replay_after("r-obs2", None)
        return batch.events[-1].to_sse()

    sse = asyncio.run(scenario())
    assert "event: section_heartbeat" in sse
    assert '"step_hint":"tool_use"' in sse


def test_只重跑选中的那一节_同名章不被连坐(tmp_path: Path) -> None:
    """§OBS-2 货 6：`only_chapters` 只复位那一节和它的父章。"""

    import sqlite3

    from app.replay.import_research import import_research
    from app.store.dao import Store
    from tests.test_rp1_stage_replay import SOURCE_ID, _seed

    database = tmp_path / "owli.db"
    runs = tmp_path / "runs"
    _seed(database, runs, statuses={f"goal-{n}": "done" for n in (1, 2, 3)})
    store = Store(database)
    store.ensure_chapters(
        SOURCE_ID, [{"goal_id": "goal-2", "chapter_id": "ch-1/sec-2"}],
        updated_at="2026-09-03T00:00:00+00:00",
    )
    connection = sqlite3.connect(database)
    connection.execute(
        "UPDATE chapter_progress SET status='done' WHERE research_id=? AND chapter_id=?",
        (SOURCE_ID, "ch-1/sec-2"),
    )
    connection.commit()
    connection.close()

    imported = import_research(
        store=store, source_database=database, source_runs=runs,
        source_research_id=SOURCE_ID, runs_root=runs,
        now_iso="2026-09-03T00:00:00+00:00",
        from_goal="goal-2", only_chapters=["ch-1/sec-2"], reset_done=True,
    )

    rows = {
        f"{row['goal_id']}/{row['chapter_id']}": row
        for row in store.list_chapters(imported.research_id)
    }
    assert set(imported.chapters_reset) == {"goal-2/ch-1", "goal-2/ch-1/sec-2"}
    assert rows["goal-2/ch-1/sec-2"]["status"] == "pending"
    assert rows["goal-2/ch-1"]["status"] == "pending", "父章不复位就压根走不到这一节"
    assert rows["goal-1/ch-1"]["status"] == "done"
    assert rows["goal-3/ch-1"]["status"] == "done", "同名章不许被连坐复位"
