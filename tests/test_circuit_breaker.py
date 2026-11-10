from app.core.circuit_breaker import BreakerRegistry, CircuitBreaker

_THRESHOLD = 3
_RESET_S = 30.0


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _state(breaker: CircuitBreaker) -> str:
    """Read state afresh; mypy would otherwise keep an earlier narrowing."""
    return breaker.state


def _breaker(clock: _Clock) -> CircuitBreaker:
    return CircuitBreaker(
        failure_threshold=_THRESHOLD, reset_timeout_s=_RESET_S, clock=clock
    )


def test_opens_after_consecutive_failures_and_rejects_calls() -> None:
    clock = _Clock()
    breaker = _breaker(clock)
    for _ in range(_THRESHOLD - 1):
        breaker.record_failure()
        assert breaker.allow()
    breaker.record_failure()

    assert breaker.state == "open"
    assert not breaker.allow()
    clock.now += 10
    assert breaker.retry_after_s == _RESET_S - 10


def test_success_resets_the_consecutive_failure_count() -> None:
    breaker = _breaker(_Clock())
    for _ in range(_THRESHOLD - 1):
        breaker.record_failure()
    breaker.record_success()
    for _ in range(_THRESHOLD - 1):
        breaker.record_failure()

    assert breaker.state == "closed"
    assert breaker.allow()


def test_half_open_admits_one_probe_and_closes_on_success() -> None:
    clock = _Clock()
    breaker = _breaker(clock)
    for _ in range(_THRESHOLD):
        breaker.record_failure()
    clock.now += _RESET_S

    assert breaker.allow()
    assert _state(breaker) == "half_open"
    assert not breaker.allow()
    breaker.record_success()
    assert _state(breaker) == "closed"
    assert breaker.allow()


def test_failed_probe_reopens_for_a_full_reset_period() -> None:
    clock = _Clock()
    breaker = _breaker(clock)
    for _ in range(_THRESHOLD):
        breaker.record_failure()
    clock.now += _RESET_S
    assert breaker.allow()
    breaker.record_failure()

    assert breaker.state == "open"
    assert not breaker.allow()
    assert breaker.retry_after_s == _RESET_S


def test_abandoned_probe_is_replaced_after_another_reset_period() -> None:
    clock = _Clock()
    breaker = _breaker(clock)
    for _ in range(_THRESHOLD):
        breaker.record_failure()
    clock.now += _RESET_S
    assert breaker.allow()  # probe outcome is never recorded

    clock.now += _RESET_S
    assert breaker.allow()


def test_registry_shares_one_breaker_per_key() -> None:
    registry = BreakerRegistry(failure_threshold=1, reset_timeout_s=_RESET_S)

    registry.get("eurorouter").record_failure()

    assert registry.get("eurorouter").state == "open"
    assert registry.get("ollama").state == "closed"
