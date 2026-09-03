"""§OBS-3 货 4：`/progress` 只读接口（进程栏），以及「日志栏一字未改」的锁。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from app.adapters.transcript import TranscriptWriter

ROOT = Path(__file__).resolve().parents[1]


class _Task:
    def __init__(self, runs_root: Path, output: Path, goal_id: str = "goal-1") -> None:
        self.research_id = "r-obs3"
        self.goal_id = goal_id
        self.agent_id = "report-writing"
        self.output_path = output
        self.runs_root = runs_root


def _endpoint(app, suffix: str):
    path = "/api/researches/{research_id}/sections/{goal_id}/{chapter}/" + suffix
    return next(route.endpoint for route in app.routes
                if getattr(route, "path", None) == path)


def _app(tmp_path: Path):
    from app.api.main import create_app

    runs_root = tmp_path / "runs"
    goal = runs_root / "r-obs3" / "goals" / "goal-1"
    writer = TranscriptWriter(_Task(runs_root, goal / "ch-3.md"), engine="Claude")
    writer.append({"subtype": "init", "data": {"type": "system", "tools": ["Write"]}})
    writer.append({"content": [{"thinking": "先读采集卡", "signature": "EsoqCqgB" * 300}]})
    writer.append({"content": [{"id": "t1", "name": "Write",
                                "input": {"file_path": str(goal / "sec-1.md")}}]})
    writer.append({"content": [{"tool_use_id": "t1", "is_error": None, "content": "ok"}],
                   "tool_use_result": {"type": "create", "filePath": str(goal / "sec-1.md"),
                                       "content": "正文" * 50}})
    writer.append({"subtype": "success", "duration_ms": 12_000, "is_error": False,
                   "structured_output": {"status": "done", "summary": "写完 sec-1"}})
    app = create_app(
        tmp_path / "owli.db", ROOT / "app" / "store" / "schema.sql",
        runs_root=runs_root, engine_probe=lambda: {"claude": {"status": "available"}},
    )
    return app


def _call(app, suffix: str, **kwargs):
    endpoint = _endpoint(app, suffix)

    async def run():
        async with app.router.lifespan_context(app):
            return await endpoint("r-obs3", "goal-1", "ch-3", **kwargs)

    return asyncio.run(run())


def test_progress_接口出人话行且零签名零JSON(tmp_path: Path) -> None:
    body = _call(tmp_path and _app(tmp_path), "progress", tail=200, after_seq=None)
    lines = body["data"]["lines"]
    assert body["ok"] and lines
    assert body["data"]["last_seq"] == 5
    stages = [line["stage"] for line in lines]
    assert stages == ["思考", "调用工具", "写入产物", "本节完成"]  # init 那条不出行
    texts = " ".join(line["text"] for line in lines)
    assert "Esoq" not in texts and "signature" not in texts and '"type"' not in texts
    assert "写好 sec-1.md，约 100 字" in texts and "写完 sec-1" in texts
    for line in lines:
        assert set(line) == {"ts", "seq", "stage", "text", "kind"}


def test_progress_增量与文件不存在(tmp_path: Path) -> None:
    app = _app(tmp_path)
    incremental = _call(app, "progress", tail=200, after_seq=4)
    assert [line["stage"] for line in incremental["data"]["lines"]] == ["本节完成"]
    assert incremental["data"]["last_seq"] == 5

    endpoint = _endpoint(app, "progress")

    async def missing():
        async with app.router.lifespan_context(app):
            return await endpoint("r-obs3", "goal-1", "ch-none", tail=200, after_seq=None)

    body = asyncio.run(missing())
    assert body["ok"] and body["data"] == {"lines": [], "last_seq": 0}


def test_日志栏一字未改_transcript接口仍原样倒出(tmp_path: Path) -> None:
    """判据 2：进程栏是新加的一栏，日志栏（`/transcript`）行为一个字都不动。"""

    app = _app(tmp_path)
    body = _call(app, "transcript", tail=200, after_seq=None)
    lines = body["data"]["lines"]
    assert len(lines) == 5 and body["data"]["size_bytes"] > 0
    assert lines[0]["event"]["subtype"] == "init"  # 系统块在日志栏里照旧
    signature = lines[1]["event"]["content"][0]["signature"]
    assert signature.startswith("Esoq") and len(signature) > 1000  # 签名串照旧原样
    assert [line["seq"] for line in lines] == [1, 2, 3, 4, 5]
    assert json.dumps(lines[4]["event"], ensure_ascii=False).count("structured_output") == 1


def test_角标往回找最近一句人话(tmp_path: Path) -> None:
    """§OBS-3 货 5 真机修：最后一条常是限额心跳，兜底文案不该长期占着角标。"""

    from app.api.events import ResearchEventBuffer, SectionHeartbeatPublisher

    runs_root = tmp_path / "runs"
    goal = runs_root / "r-obs3" / "goals" / "goal-1"
    writer = TranscriptWriter(_Task(runs_root, goal / "ch-3.md"), engine="Claude")
    writer.append({"content": [{"id": "t1", "name": "Write",
                               "input": {"file_path": "/x/sec-1.md"}}]})
    for _ in range(3):  # 之后连着来三条译不出人话的
        writer.append({"rate_limit_info": {"status": "allowed"}, "session_id": "s"})

    publisher = SectionHeartbeatPublisher(
        ResearchEventBuffer(), runs_root, lambda: ["r-obs3"], clock=lambda: 1.0,
    )
    (beat,) = asyncio.run(publisher.tick())
    assert beat["data"]["step_hint"] == "调用 Write（sec-1.md）"
    assert beat["data"]["last_seq"] == 4  # 回看不影响 seq 口径
