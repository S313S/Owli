import asyncio
import json
import sys
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

import claude_agent_sdk as claude_sdk


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "app" / "store" / "schema.sql"
sys.path.insert(0, str(ROOT))


def async_test(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return wrapper


def test_真实_claude_消息按_thread_turn_item_归一():
    from app.adapters.events import ItemKind, normalize_claude_event

    message = claude_sdk.AssistantMessage(
        content=[
            claude_sdk.ThinkingBlock("核对证据", "sig-1"),
            claude_sdk.ToolUseBlock("tool-1", "WebSearch", {"query": "Owli"}),
            claude_sdk.TextBlock("形成结论"),
        ],
        model="claude-sonnet-4-5",
        message_id="turn-7",
        session_id="thread-3",
    )

    events = normalize_claude_event(message, sdk=claude_sdk)

    assert [event.item_kind for event in events] == [
        ItemKind.THINKING,
        ItemKind.TOOL_CALL,
        ItemKind.OUTPUT,
    ]
    assert [event.text for event in events] == [
        "核对证据",
        '[WebSearch] {"query": "Owli"}',
        "形成结论",
    ]
    assert all(event.engine == "Claude" for event in events)
    assert all(event.thread_id == "thread-3" for event in events)
    assert all(event.turn_id == "turn-7" for event in events)
    assert all(event.raw is message for event in events)
    assert not any(event.is_error for event in events)


def test_subtype_success_但_api_error_status_429_仍归一为错误():
    from app.adapters.events import ItemKind, normalize_claude_event

    message = claude_sdk.ResultMessage(
        subtype="success",
        duration_ms=100,
        duration_api_ms=90,
        is_error=False,
        num_turns=1,
        session_id="thread-rate-limit",
        result="You've hit your usage limit",
        api_error_status=429,
        uuid="turn-rate-limit",
    )

    events = normalize_claude_event(message, sdk=claude_sdk)

    assert len(events) == 1
    assert events[0].item_kind is ItemKind.ERROR
    assert events[0].is_error is True
    assert events[0].thread_id == "thread-rate-limit"
    assert events[0].turn_id == "turn-rate-limit"
    assert events[0].raw is message


def test_同一次_claude_执行的所有消息共享_codex_口径_turn_id():
    from app.adapters.events import normalize_claude_event

    assistant = claude_sdk.AssistantMessage(
        content=[claude_sdk.TextBlock("中间输出")],
        model="claude-sonnet-4-5",
        message_id="native-message-1",
        session_id="thread-1",
    )
    result = claude_sdk.ResultMessage(
        subtype="success",
        duration_ms=100,
        duration_api_ms=90,
        is_error=False,
        num_turns=2,
        session_id="thread-1",
        result="完成",
        uuid="native-result-2",
    )

    events = [
        *normalize_claude_event(
            assistant, sdk=claude_sdk, turn_id="owli-turn-1"
        ),
        *normalize_claude_event(
            result, sdk=claude_sdk, turn_id="owli-turn-1"
        ),
    ]

    assert {event.turn_id for event in events} == {"owli-turn-1"}


def test_同一次_claude_执行含工具返回时共享_codex_口径_thread_id():
    from app.adapters.events import normalize_claude_event

    messages = [
        claude_sdk.AssistantMessage(
            content=[claude_sdk.TextBlock("调用工具")],
            model="claude-sonnet-4-5",
            message_id="native-message-1",
            session_id="native-session",
        ),
        claude_sdk.UserMessage(
            content=[claude_sdk.ToolResultBlock("tool-1", "工具结果")],
            uuid="native-user-2",
        ),
        claude_sdk.ResultMessage(
            subtype="success",
            duration_ms=100,
            duration_api_ms=90,
            is_error=False,
            num_turns=2,
            session_id="native-session",
            result="完成",
        ),
    ]

    events = [
        event
        for message in messages
        for event in normalize_claude_event(
            message,
            sdk=claude_sdk,
            thread_id="owli-thread-1",
            turn_id="owli-turn-1",
        )
    ]

    assert {event.thread_id for event in events} == {"owli-thread-1"}


def test_错误日志逐字节写入原始载荷(tmp_path):
    from app.adapters.events import ItemKind, NormalizedEvent
    from app.adapters.logging import append_engine_error

    raw = {
        "type": "result",
        "subtype": "success",
        "api_error_status": 429,
        "nested": ["原始错误", {"code": "rate_limit"}],
    }
    event = NormalizedEvent(
        engine="Claude",
        thread_id="thread-1",
        turn_id="turn-1",
        item_kind=ItemKind.ERROR,
        text="额度耗尽",
        is_error=True,
        raw=raw,
    )
    now = datetime(2026, 8, 18, 10, 30, tzinfo=timezone.utc)

    path = append_engine_error(event, log_root=tmp_path, clock=lambda: now)

    expected = json.dumps(
        raw, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8") + b"\n"
    assert path == tmp_path / "claude-2026-08-18.jsonl"
    assert path.read_bytes() == expected


def test_原始_type_含_error_即使归一标记为正常也落盘(tmp_path):
    from app.adapters.events import ItemKind, NormalizedEvent
    from app.adapters.logging import append_engine_error

    raw = {"type": "turn.error_notice", "message": "原始错误"}
    event = NormalizedEvent(
        engine="Codex",
        thread_id="thread-1",
        turn_id="turn-1",
        item_kind=ItemKind.THINKING,
        text="未分类事件",
        is_error=False,
        raw=raw,
    )

    path = append_engine_error(
        event,
        log_root=tmp_path,
        clock=lambda: datetime(2026, 8, 18, tzinfo=timezone.utc),
    )

    assert path is not None
    assert json.loads(path.read_text(encoding="utf-8")) == raw


class _DisconnectedRequest:
    async def is_disconnected(self) -> bool:
        return True


def _events_route(app):
    return next(
        route
        for route in app.routes
        if getattr(route, "path", None) == "/api/researches/{research_id}/events"
    )


@async_test
async def test_连续重连十次不增加_research_事件序号(tmp_path):
    from app.api.events import ResearchEventBuffer
    from app.api.main import create_app

    buffer = ResearchEventBuffer()
    app = create_app(tmp_path / "owli.db", SCHEMA_PATH, event_buffer=buffer)
    route = _events_route(app)
    first = await buffer.publish("r-reconnect", {"type": "progress"})
    assert first.sequence == 1

    for _ in range(10):
        response = await route.endpoint(
            "r-reconnect", _DisconnectedRequest(), None, None
        )
        iterator = response.body_iterator
        connected = await anext(iterator)
        assert "event: stream_connected\n" in connected
        assert not connected.startswith("id:")
        await iterator.aclose()

    second = await buffer.publish("r-reconnect", {"type": "progress"})
    replay = await buffer.replay_after("r-reconnect", 0)
    assert second.sequence == 2
    assert [event.sequence for event in replay.events] == [1, 2]
    assert all(event.payload["type"] != "stream_connected" for event in replay.events)


@async_test
async def test_连接确认不改写_Last_Event_ID_重放游标(tmp_path):
    from app.api.events import ResearchEventBuffer
    from app.api.main import create_app

    buffer = ResearchEventBuffer()
    app = create_app(tmp_path / "owli.db", SCHEMA_PATH, event_buffer=buffer)
    route = _events_route(app)
    await buffer.publish("r-replay", {"type": "progress", "data": {"number": 1}})
    await buffer.publish("r-replay", {"type": "progress", "data": {"number": 2}})

    response = await route.endpoint(
        "r-replay", _DisconnectedRequest(), "1", None
    )
    iterator = response.body_iterator

    connected = await anext(iterator)
    replayed = await anext(iterator)
    await iterator.aclose()

    assert "event: stream_connected\n" in connected
    assert "id: 2\n" in replayed
    assert '"number":2' in replayed
