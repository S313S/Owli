from __future__ import annotations


def _breaker(threshold: int = 3):
    from app.adapters.circuitbreaker import ResearchCircuitBreaker
    from app.config import ResilienceConfig

    return ResearchCircuitBreaker(
        "r-circuit",
        ResilienceConfig(threshold, 3, 60, 900, 300),
    )


def test_执行期连续三次传输故障才请求断路() -> None:
    from app.adapters.circuitbreaker import CircuitEvent

    breaker = _breaker()

    assert breaker.record_transport_failure("claude", planning=False) is None
    assert breaker.record_transport_failure("claude", planning=False) is None
    transition = breaker.record_transport_failure("claude", planning=False)

    assert transition is not None
    assert transition.event is CircuitEvent.ENGINE_DOWN
    assert transition.engine == "claude"
    assert breaker.failure_count("claude") == 3
    assert breaker.route_override is None


def test_规划期传输故障永不累计或断路() -> None:
    breaker = _breaker(threshold=2)

    for _ in range(5):
        assert breaker.record_transport_failure("claude", planning=True) is None

    assert breaker.failure_count("claude") == 0
    assert breaker.route_override is None


def test_限流与普通失败不计数并打断连续传输序列() -> None:
    breaker = _breaker()

    breaker.record_transport_failure("claude", planning=False)
    breaker.record_transport_failure("claude", planning=False)
    breaker.record_non_transport("claude")

    assert breaker.failure_count("claude") == 0
    assert breaker.record_transport_failure("claude", planning=False) is None


def test_成功运行复位连续传输计数() -> None:
    breaker = _breaker()
    breaker.record_transport_failure("codex", planning=False)
    breaker.record_success("codex")

    assert breaker.failure_count("codex") == 0


def test_候选健康后才激活让路且探活通过闭环复位() -> None:
    from app.adapters.circuitbreaker import CircuitEvent

    breaker = _breaker(threshold=1)
    transition = breaker.record_transport_failure("claude", planning=False)
    assert transition is not None

    breaker.reject_failover("claude")
    assert breaker.route_override is None
    assert breaker.is_down("claude") is False

    transition = breaker.record_transport_failure("claude", planning=False)
    assert transition is not None
    down = breaker.activate_failover("claude", "codex")

    assert down.event is CircuitEvent.ENGINE_DOWN
    assert breaker.route_override == "codex"
    assert breaker.is_down("claude") is True

    assert breaker.record_probe("claude", healthy=False) == ()
    recovered = breaker.record_probe("claude", healthy=True)
    assert [item.event for item in recovered] == [
        CircuitEvent.PROBE_OK,
        CircuitEvent.RESET,
    ]
    assert breaker.route_override is None
    assert breaker.is_down("claude") is False
    assert breaker.failure_count("claude") == 0
