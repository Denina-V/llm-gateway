"""Token-bucket rate limiting, one bucket per API key.

A bucket refills continuously at `rate` tokens/second up to `burst`, which lets
a caller spend a short burst and then settle to the sustained rate -- the shape
you actually want in front of a paid upstream.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class _Bucket:
    tokens: float
    updated_at: float


class TokenBucketLimiter:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def acquire(
        self, key: str, *, rate: float, burst: int, cost: float = 1.0
    ) -> tuple[bool, float]:
        """Try to spend `cost` tokens.

        Returns (allowed, retry_after_s). retry_after_s is 0.0 when allowed.
        """
        if rate <= 0:
            raise ValueError("rate must be > 0")
        now = self._clock()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(tokens=float(burst), updated_at=now)
                self._buckets[key] = bucket

            elapsed = max(0.0, now - bucket.updated_at)
            bucket.tokens = min(float(burst), bucket.tokens + elapsed * rate)
            bucket.updated_at = now

            if bucket.tokens >= cost:
                bucket.tokens -= cost
                return True, 0.0
            deficit = cost - bucket.tokens
            return False, deficit / rate

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._buckets.clear()
            else:
                self._buckets.pop(key, None)
