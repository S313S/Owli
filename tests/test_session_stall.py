from __future__ import annotations


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _event(*, outcome=None, cause=None, kind="thinking"):
    from app.adapters.events import ItemKind, NormalizedEvent

    return NormalizedEvent(
        engine="Claude",
        thread_id="r-stall",
        turn_id="turn-1",
        item_kind=ItemKind(kind),
        text=str(outcome or kind),
        is_error=False,
        raw={},
        outcome=outcome,
        cause=cause,
    )


def test_连续_api_retry_达到阈值只触发一次并保留取证字段():
    from app.adapters.session_stall import SessionStallDetector

    clock = FakeClock()
    detector = SessionStallDetector(timeout_seconds=600, clock=clock)

    assert detector.observe(_event(outcome="API_RETRY")) is None
    clock.advance(599)
    assert detector.observe(_event(outcome="API_RETRY")) is None
    clock.advance(1)
    evidence = detector.observe(_event(outcome="API_RETRY"))
    clock.advance(600)
    repeated = detector.observe(_event(outcome="API_RETRY"))

    assert evidence is not None
    assert evidence.elapsed_seconds == 600
    assert evidence.api_retry_count == 3
    assert repeated is None


def test_工具与输出活动会复位_api_retry_计时():
    from app.adapters.session_stall import SessionStallDetector

    for activity in ("tool_call", "output"):
        clock = FakeClock()
        detector = SessionStallDetector(timeout_seconds=600, clock=clock)
        detector.observe(_event(outcome="API_RETRY"))
        clock.advance(590)
        detector.observe(_event(kind=activity))
        clock.advance(20)
        assert detector.observe(_event(outcome="API_RETRY")) is None
        clock.advance(599)
        assert detector.observe(_event(outcome="API_RETRY")) is None
        clock.advance(1)
        assert detector.observe(_event(outcome="API_RETRY")) is not None


def test_限流_api_retry_退出计时且不触发停滞():
    from app.adapters.session_stall import SessionStallDetector

    clock = FakeClock()
    detector = SessionStallDetector(timeout_seconds=600, clock=clock)
    detector.observe(_event(outcome="API_RETRY"))
    clock.advance(590)
    detector.observe(_event(outcome="API_RETRY", cause="rate_limit"))
    clock.advance(600)

    assert detector.observe(
        _event(outcome="API_RETRY", cause="rate_limit")
    ) is None
    assert detector.observe(_event(outcome="API_RETRY")) is None


def test_真实_68_分钟_api_retry_事件重放在_600_秒处仅触发一次():
    from app.adapters.session_stall import SessionStallDetector

    clock = FakeClock()
    detector = SessionStallDetector(timeout_seconds=600, clock=clock)
    # 对应真实形态：每 1–3 分钟一条；第 600 秒恰有一条，之后继续到 68 分钟。
    event_times = [0, 60, 180, 300, 420, 600]
    event_times.extend(range(720, 68 * 60 + 1, 120))
    evidence = []
    previous = 0
    for current in event_times:
        clock.advance(current - previous)
        result = detector.observe(_event(outcome="API_RETRY"))
        if result is not None:
            evidence.append(result)
        previous = current

    assert len(evidence) == 1
    assert evidence[0].elapsed_seconds == 600
    assert evidence[0].api_retry_count == 6
