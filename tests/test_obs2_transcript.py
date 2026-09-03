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


def _task(runs_root: Path, output: Path) -> EngineTask:
    return EngineTask(
        body="写", output_path=output, output_format="markdown",
        research_id="r-obs2", goal_id="goal-1", agent_id="report-writing",
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
