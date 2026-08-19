import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import claude_agent_sdk as claude_sdk


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _rate_limit_event(
    *,
    status: str,
    rate_limit_type: str = "seven_day_opus",
    utilization: float | None = None,
    overage_status: str | None = None,
    overage_disabled_reason: str | None = None,
):
    raw_info = {
        "status": status,
        "rateLimitType": rate_limit_type,
        "utilization": utilization,
        "overageStatus": overage_status,
        "overageDisabledReason": overage_disabled_reason,
    }
    return claude_sdk.RateLimitEvent(
        rate_limit_info=claude_sdk.RateLimitInfo(
            status=status,
            rate_limit_type=rate_limit_type,
            utilization=utilization,
            overage_status=overage_status,
            overage_disabled_reason=overage_disabled_reason,
            raw=raw_info,
        ),
        uuid="turn-rate-limit",
        session_id="thread-rate-limit",
    )


def _result_message(
    *,
    is_error: bool,
    api_error_status: int | None = None,
    subtype: str = "error_during_execution",
):
    return claude_sdk.ResultMessage(
        subtype=subtype,
        duration_ms=100,
        duration_api_ms=90,
        is_error=is_error,
        num_turns=1,
        session_id="thread-result",
        result="引擎返回",
        api_error_status=api_error_status,
        uuid="turn-result",
    )


def test_rejected_且_overage_allowed_进入_warn_等待计费确认():
    from app.adapters.ratelimit import RouteState, route

    message = _rate_limit_event(
        status="rejected",
        overage_status="allowed",
    )

    decision = route(message)

    assert decision.state is RouteState.WARN
    assert "继续跑会计费" in decision.reason
    assert decision.raw is message
    assert decision.failover_target is None
    assert decision.no_fallback_left is False


def test_rejected_且_overage_allowed_warning_仍进入计费确认():
    from app.adapters.ratelimit import RouteState, route

    decision = route(_rate_limit_event(
        status="rejected",
        overage_status="allowed_warning",
    ))

    assert decision.state is RouteState.WARN
    assert "继续跑会计费" in decision.reason
    assert "overage 也接近上限" in decision.reason


def test_rejected_且_overage_disabled_切到_codex_并标记无退路():
    from app.adapters.ratelimit import RouteState, route

    message = _rate_limit_event(
        status="rejected",
        overage_status="rejected",
        overage_disabled_reason="spend_limit_reached",
    )

    decision = route(message)

    assert decision.state is RouteState.FAILOVER
    assert "spend_limit_reached" in decision.reason
    assert decision.failover_target == "codex"
    assert decision.no_fallback_left is True


def test_allowed_warning_利用率_85_产生新任务让路信号():
    from app.adapters.ratelimit import RouteState, route

    decision = route(_rate_limit_event(
        status="allowed_warning",
        rate_limit_type="five_hour",
        utilization=0.85,
    ))

    assert decision.state is RouteState.WARN
    assert "85%" in decision.reason
    assert "新任务让路" in decision.reason


def test_api_429_进入_backoff():
    from app.adapters.ratelimit import RouteState, route

    decision = route(_result_message(is_error=True, api_error_status=429))

    assert decision.state is RouteState.BACKOFF
    assert "429" in decision.reason


def test_api_500_进入_backoff():
    from app.adapters.ratelimit import RouteState, route

    decision = route(_result_message(is_error=True, api_error_status=500))

    assert decision.state is RouteState.BACKOFF
    assert "500" in decision.reason


def test_api_529_进入_backoff():
    from app.adapters.ratelimit import RouteState, route

    decision = route(_result_message(is_error=True, api_error_status=529))

    assert decision.state is RouteState.BACKOFF
    assert "529" in decision.reason


def test_非限流_is_error_进入_failover():
    from app.adapters.ratelimit import RouteState, route

    decision = route(_result_message(is_error=True))

    assert decision.state is RouteState.FAILOVER
    assert "error_during_execution" in decision.reason
    assert decision.failover_target == "codex"
    assert decision.no_fallback_left is True


def test_subtype_success_但_api_error_status_429_绝不_continue():
    from app.adapters.ratelimit import RouteState, route

    decision = route(_result_message(
        is_error=False,
        api_error_status=429,
        subtype="success",
    ))

    assert decision.state is RouteState.BACKOFF


def test_codex_撞墙文案采用宽容匹配():
    from app.adapters.ratelimit import classify_codex_error

    samples = [
        "You've hit your usage limit. Visit settings to purchase more credits",
        "Goal hit usage limits (/goal resume)",
        "unknown rate limit reached type: weekly",
        "RATE_LIMIT reached; try again later",
    ]

    assert all(classify_codex_error(text) for text in samples)
    assert classify_codex_error("工具执行失败，请检查参数") is False


def test_codex_普通命令输出提到_ratelimit_文件名不算撞墙():
    from app.adapters.ratelimit import RouteState, route

    # 真实误报样本（2026-08-19 M2-e 验收实录）：agent 读 architecture.md，
    # 正文含文件名 `adapters/ratelimit.py`，曾被整串 JSON 宽容匹配误判 BACKOFF。
    decision = route(
        {
            "type": "item.completed",
            "item": {
                "id": "item_3",
                "type": "command_execution",
                "command": "/bin/zsh -lc \"sed -n '1,260p' .docs-ref/architecture.md\"",
                "aggregated_output": "L4 adapters/ratelimit.py 限流四态路由；rate limits 见 R8。",
            },
        },
        engine="Codex",
    )

    assert decision.state is RouteState.CONTINUE


def test_codex_错误载荷撞墙文案仍判_BACKOFF(tmp_path):
    from app.adapters.ratelimit import RouteState, route

    decision = route(
        {
            "type": "item.completed",
            "item": {
                "id": "item_0",
                "type": "error",
                "message": "You've hit your usage limit. Visit settings to purchase more credits",
            },
        },
        engine="Codex",
        log_root=tmp_path,
    )

    assert decision.state is RouteState.BACKOFF
    assert decision.suspend_new_tasks is True


def test_每个非_continue_决策只产一条_normalized_event_且透传_raw(tmp_path):
    from app.adapters.events import ItemKind, NormalizedEvent
    from app.adapters.ratelimit import route

    messages = [
        _rate_limit_event(status="allowed_warning", utilization=0.85),
        _result_message(is_error=True, api_error_status=429),
        _result_message(is_error=True),
    ]
    events: list[NormalizedEvent] = []

    for message in messages:
        before = len(events)
        route(
            message,
            on_event=events.append,
            log_root=tmp_path,
            log_clock=lambda: datetime(2026, 8, 18, tzinfo=timezone.utc),
        )
        assert len(events) == before + 1
        assert events[-1].raw is message

    assert events[0].item_kind is ItemKind.THINKING
    assert events[0].is_error is False
    assert all(event.item_kind is ItemKind.ERROR for event in events[1:])
    assert all(event.is_error for event in events[1:])


def test_错误类决策经_logging_逐字节落原始载荷(tmp_path):
    from app.adapters.ratelimit import route

    message = _result_message(is_error=True, api_error_status=529)
    now = datetime(2026, 8, 18, 10, 30, tzinfo=timezone.utc)

    route(message, log_root=tmp_path, log_clock=lambda: now)

    path = tmp_path / "claude-2026-08-18.jsonl"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["api_error_status"] == 529
    assert payload["uuid"] == "turn-result"


def test_continue_决策不产_event_也不写错误日志(tmp_path):
    from app.adapters.ratelimit import RouteState, route

    events = []
    message = _rate_limit_event(status="allowed", utilization=0.30)

    decision = route(message, on_event=events.append, log_root=tmp_path)

    assert decision.state is RouteState.CONTINUE
    assert events == []
    assert list(tmp_path.iterdir()) == []


def test_公共_on_rate_limited_只接收事后硬限流(tmp_path):
    from app.adapters.ratelimit import route

    notices = []
    messages = [
        _rate_limit_event(status="allowed_warning", utilization=0.85),
        _rate_limit_event(status="rejected", overage_status="allowed"),
        _result_message(is_error=True, api_error_status=429),
        _result_message(is_error=True, api_error_status=529),
        _result_message(is_error=True),
    ]

    for message in messages:
        route(
            message,
            on_rate_limited=lambda engine, detail: notices.append((engine, detail)),
            log_root=tmp_path,
        )

    assert len(notices) == 2
    assert all(engine == "Claude" for engine, _ in notices)
    assert notices[0][1].raw is messages[1]
    assert notices[1][1].raw is messages[2]


def test_r8_经过_15_分钟未确认自动切到_codex(tmp_path):
    from app.adapters.ratelimit import R8Confirm, RouteState, route

    current = [datetime(2042, 3, 4, 9, 0, tzinfo=timezone.utc)]
    machine = R8Confirm(clock=lambda: current[0], log_root=tmp_path)
    waiting = route(_rate_limit_event(
        status="rejected",
        overage_status="allowed",
    ))

    machine.wait(waiting)
    assert machine.suspend_new_tasks is True
    assert machine.deadline == current[0] + timedelta(minutes=15)
    current[0] += timedelta(minutes=14, seconds=59)
    assert machine.check_timeout() is None

    current[0] += timedelta(seconds=1)
    decision = machine.check_timeout()

    assert decision is not None
    assert decision.state is RouteState.FAILOVER
    assert decision.failover_target == "codex"
    assert decision.no_fallback_left is True
    assert machine.suspend_new_tasks is False
    assert (tmp_path / "claude-2042-03-04.jsonl").is_file()


def test_r8_解决时不重复触发公共限流回调(tmp_path):
    from app.adapters.ratelimit import R8Confirm, route

    current = [datetime(2026, 8, 18, 9, 0, tzinfo=timezone.utc)]
    notices = []
    callback = lambda engine, detail: notices.append((engine, detail))
    waiting = route(
        _rate_limit_event(status="rejected", overage_status="allowed"),
        on_rate_limited=callback,
        log_root=tmp_path,
    )
    machine = R8Confirm(
        clock=lambda: current[0],
        log_root=tmp_path,
    )
    machine.wait(waiting)
    current[0] += timedelta(minutes=15)

    machine.check_timeout()

    assert len(notices) == 1


def test_r8_到点前确认接受计费则继续且解除挂起():
    from app.adapters.ratelimit import R8Confirm, RouteState, route

    current = [datetime(2026, 8, 18, 9, 0, tzinfo=timezone.utc)]
    machine = R8Confirm(clock=lambda: current[0])
    waiting = route(_rate_limit_event(
        status="rejected",
        overage_status="allowed",
    ))
    machine.wait(waiting)
    current[0] += timedelta(minutes=14)

    decision = machine.confirm(continue_with_overage=True)

    assert decision.state is RouteState.CONTINUE
    assert "用户确认计费" in decision.reason
    assert decision.raw is waiting.raw
    assert machine.suspend_new_tasks is False
    assert machine.check_timeout() is None


def test_r8_到点前确认不接受计费则切到_codex():
    from app.adapters.ratelimit import R8Confirm, RouteState, route

    current = [datetime(2026, 8, 18, 9, 0, tzinfo=timezone.utc)]
    machine = R8Confirm(clock=lambda: current[0])
    machine.wait(route(_rate_limit_event(
        status="rejected",
        overage_status="allowed",
    )))

    decision = machine.confirm(continue_with_overage=False)

    assert decision.state is RouteState.FAILOVER
    assert decision.failover_target == "codex"
    assert decision.no_fallback_left is True
    assert machine.suspend_new_tasks is False
