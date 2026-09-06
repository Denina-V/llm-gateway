"""Per-provider circuit breaker.

Without one, a provider outage turns into a queue of requests all waiting the
full timeout, and the gateway's own latency collapses along with the upstream.
The breaker converts a slow failure into a fast one.

States: CLOSED -> (threshold consecutive failures) -> OPEN -> (reset timeout)
-> HALF_OPEN -> (one success) -> CLOSED, or (one failure) -> OPEN again.

The clock is injected so the tests can advance time instead of sleeping.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from enum import StrEnum


class State(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        reset_timeout_s: float = 20.0,
        half_open_max: int = 1,
        clock: Callable[[], float] = time.monotonic,
        name: str = "default",
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        self.name = name
        self._threshold = failure_threshold
        self._reset_timeout = reset_timeout_s
        self._half_open_max = half_open_max
        self._clock = clock
        self._lock = threading.Lock()

        self._state = State.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._half_open_inflight = 0

    @property
    def state(self) -> State:
        with self._lock:
            self._maybe_half_open()
            return self._state

    def _maybe_half_open(self) -> None:
        """Caller must hold the lock."""
        if self._state is State.OPEN and self._clock() - self._opened_at >= self._reset_timeout:
            self._state = State.HALF_OPEN
            self._half_open_inflight = 0

    def allow(self) -> bool:
        """Reserve a slot. A HALF_OPEN breaker admits a bounded number of trials."""
        with self._lock:
            self._maybe_half_open()
            if self._state is State.CLOSED:
                return True
            if self._state is State.OPEN:
                return False
            if self._half_open_inflight < self._half_open_max:
                self._half_open_inflight += 1
                return True
            return False

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._half_open_inflight = 0
            self._state = State.CLOSED

    def record_failure(self) -> None:
        with self._lock:
            self._half_open_inflight = 0
            if self._state is State.HALF_OPEN:
                # The trial request failed: straight back to OPEN, and the
                # reset window restarts from now rather than from the first trip.
                self._state = State.OPEN
                self._opened_at = self._clock()
                return
            self._failures += 1
            if self._failures >= self._threshold:
                self._state = State.OPEN
                self._opened_at = self._clock()

    def snapshot(self) -> dict:
        with self._lock:
            self._maybe_half_open()
            return {
                "name": self.name,
                "state": self._state.value,
                "consecutive_failures": self._failures,
            }
