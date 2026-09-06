import pytest

from gateway.resilience.breaker import CircuitBreaker, State
from helpers import FakeClock


def make(clock, **kw):
    return CircuitBreaker(failure_threshold=3, reset_timeout_s=10.0, clock=clock, **kw)


def test_starts_closed_and_allows():
    b = make(FakeClock())
    assert b.state is State.CLOSED
    assert b.allow()


def test_opens_only_at_the_threshold():
    b = make(FakeClock())
    b.record_failure()
    b.record_failure()
    assert b.state is State.CLOSED, "two failures with a threshold of three must not trip it"
    b.record_failure()
    assert b.state is State.OPEN
    assert not b.allow()


def test_success_resets_the_failure_run():
    """The counter is consecutive failures, not lifetime failures -- otherwise a
    long-lived process trips the breaker on unrelated blips hours apart."""
    b = make(FakeClock())
    b.record_failure()
    b.record_failure()
    b.record_success()
    b.record_failure()
    b.record_failure()
    assert b.state is State.CLOSED


def test_open_becomes_half_open_only_after_the_reset_window():
    clock = FakeClock()
    b = make(clock)
    for _ in range(3):
        b.record_failure()
    clock.advance(9.9)
    assert b.state is State.OPEN
    clock.advance(0.2)
    assert b.state is State.HALF_OPEN


def test_half_open_admits_one_trial_then_closes_on_success():
    clock = FakeClock()
    b = make(clock)
    for _ in range(3):
        b.record_failure()
    clock.advance(11)
    assert b.allow(), "the first trial request is admitted"
    assert not b.allow(), "a second concurrent trial is not"
    b.record_success()
    assert b.state is State.CLOSED
    assert b.allow()


def test_half_open_failure_reopens_and_restarts_the_window():
    clock = FakeClock()
    b = make(clock)
    for _ in range(3):
        b.record_failure()
    clock.advance(11)
    assert b.allow()
    b.record_failure()
    assert b.state is State.OPEN

    # The window restarts from the failed trial, not from the original trip --
    # otherwise a breaker that has been open a long time would re-probe
    # immediately and hammer an upstream that is still down.
    clock.advance(9.9)
    assert b.state is State.OPEN
    clock.advance(0.2)
    assert b.state is State.HALF_OPEN


def test_threshold_must_be_positive():
    with pytest.raises(ValueError):
        CircuitBreaker(failure_threshold=0)
