from __future__ import annotations

import asyncio
import json
from pathlib import Path


def _payload(result, events, event_path: Path, byte_limit: int):
    from app.adapters.source_mcp import _build_tool_payload

    return _build_tool_payload(
        result=result,
        events=events,
        error=None,
        event_path=event_path,
        byte_limit=byte_limit,
    )


def test_超限回灌按完整条目丢尾且仍是合法_JSON(tmp_path: Path) -> None:
    items = [{"id": index, "body": "证据" * 80} for index in range(8)]
    event_path = tmp_path / "source-events.jsonl"

    payload, text = _payload(items, [], event_path, byte_limit=1200)
    decoded = json.loads(text)

    assert decoded == payload
    assert len(text.encode("utf-8")) <= 1200
    assert 0 < len(decoded["result"]) < len(items)
    omitted = len(items) - len(decoded["result"])
    assert decoded["truncation"]["omitted_items"] == omitted
    archive_path = Path(decoded["truncation"]["full_payload_path"])
    assert decoded["truncation"]["message"] == (
        f"已截断 {omitted} 条 / 全量见落盘文件 {archive_path}"
    )
    assert decoded["result"] == items[: len(decoded["result"])]
    assert archive_path == Path(f"{event_path}.payload.json")
    assert json.loads(archive_path.read_text(encoding="utf-8"))["result"] == items


def test_X_evidence_容器超限时同样按条丢尾(tmp_path: Path) -> None:
    result = {
        "evidence": [{"id": index, "body": "长推" * 80} for index in range(6)],
        "conclusion": {"status": "completed"},
    }

    payload, text = _payload(
        result, [], tmp_path / "source-events.jsonl", byte_limit=1600
    )

    decoded = json.loads(text)
    assert decoded == payload
    assert 0 < len(decoded["result"]["evidence"]) < 6
    assert decoded["result"]["conclusion"] == {"status": "completed"}


def test_未超限时限流器不改变序列化文本(tmp_path: Path) -> None:
    from app.adapters.source_mcp import _event_summary, _jsonable

    event_path = tmp_path / "source-events.jsonl"
    result = [{"id": 1, "body": "短文本"}]
    events = [{"type": "progress", "data": {"step": 1}}]
    expected = {
        "result": _jsonable(result),
        "events": _event_summary(events, event_path),
        "error": None,
    }
    expected_text = json.dumps(
        expected, ensure_ascii=False, separators=(",", ":")
    )

    payload, text = _payload(result, events, event_path, byte_limit=4096)

    assert payload == expected
    assert text == expected_text
    assert "truncation" not in payload


def test_events_只回灌摘要且结构化内容与文本同源(tmp_path: Path) -> None:
    events = [
        {"type": "progress", "data": {"raw_marker": "不得回灌"}},
        {"type": "source_unavailable", "data": {"raw_marker": "不得回灌"}},
        {"type": "provider_error", "data": {"raw_marker": "不得回灌"}},
    ]
    event_path = tmp_path / "source-events.jsonl"

    payload, text = _payload([], events, event_path, byte_limit=4096)

    assert payload["events"] == {
        "count": 3,
        "error_count": 2,
        "path": str(event_path),
    }
    assert "raw_marker" not in text
    assert json.loads(text) == payload


def test_events_摘要识别真实_NormalizedEvent_错误(tmp_path: Path) -> None:
    from app.adapters.events import ItemKind, NormalizedEvent

    error_event = NormalizedEvent(
        engine="source",
        thread_id=None,
        turn_id=None,
        item_kind=ItemKind.ERROR,
        text="provider failover",
        is_error=True,
        raw={"type": "provider_failure"},
    )

    payload, _ = _payload(
        [], [error_event], tmp_path / "source-events.jsonl", byte_limit=4096
    )

    assert payload["events"]["error_count"] == 1


def test_非数组异常超限仍返回受控合法_JSON(tmp_path: Path) -> None:
    from app.adapters.source_mcp import _build_tool_payload

    event_path = tmp_path / "source-events.jsonl"
    payload, text = _build_tool_payload(
        result=None,
        events=[],
        error=RuntimeError("错误详情" * 2000),
        event_path=event_path,
        byte_limit=1024,
    )

    decoded = json.loads(text)
    assert decoded == payload
    assert len(text.encode("utf-8")) <= 1024
    assert decoded["error"]["type"] == "RuntimeError"
    assert "错误详情" * 2 not in decoded["error"]["message"]
    assert decoded["truncation"]["omitted_items"] == 1
    archive_path = Path(decoded["truncation"]["full_payload_path"])
    assert "错误详情" in archive_path.read_text(encoding="utf-8")


def test_MCP_子进程透传采集响应字节上限配置() -> None:
    from app.adapters.source_mcp import stdio_server_config

    config = stdio_server_config(
        ("hacker_news",),
        environ={"OWLI_SOURCE_PAYLOAD_BYTE_LIMIT": "4096"},
    )

    assert config["env"]["OWLI_SOURCE_PAYLOAD_BYTE_LIMIT"] == "4096"


def test_事件重放完成后清理同调用的完整_payload_归档(
    tmp_path: Path, monkeypatch,
) -> None:
    from app.adapters import source_mcp

    event_path = tmp_path / "source-events.jsonl"
    archive_path = Path(f"{event_path}.payload.json")
    event_path.write_text('{"type":"progress"}\n', encoding="utf-8")
    archive_path.write_text('{"result":[1]}', encoding="utf-8")
    monkeypatch.setattr(source_mcp, "source_event_path", lambda _task: event_path)

    asyncio.run(source_mcp.replay_source_events(object()))

    assert not event_path.exists()
    assert not archive_path.exists()


def test_无事件文件时重放仍清理完整_payload_归档(
    tmp_path: Path, monkeypatch,
) -> None:
    from app.adapters import source_mcp

    event_path = tmp_path / "source-events.jsonl"
    archive_path = Path(f"{event_path}.payload.json")
    archive_path.write_text('{"result":[1]}', encoding="utf-8")
    monkeypatch.setattr(source_mcp, "source_event_path", lambda _task: event_path)

    asyncio.run(source_mcp.replay_source_events(object()))

    assert not archive_path.exists()
