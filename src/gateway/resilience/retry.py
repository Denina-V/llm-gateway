"""Retry with exponential backoff and full jitter.

Full jitter (sleep uniformly in [0, backoff]) rather than fixed backoff: when a
provider recovers from an outage, every stalled client retries at once, and
un-jittered backoff synchronises them into a thundering herd that knocks the
provider straight back over.

Only errors that declare themselves retryable are retried -- a 400 is not going
to become a 200 on the third attempt, and retrying it just burns the budget.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

from ..errors import GatewayError

T = TypeVar("T")


def backoff_delay(
    attempt: int,
    *,
    base_s: float,
    max_s: float,
    rand: Callable[[], float] = random.random,
) -> float:
    """Delay before `attempt` (1-indexed: the wait *after* attempt 1 failed)."""
    ceiling = min(max_s, base_s * (2 ** (attempt - 1)))
    return rand() * ceiling


async def retry_async(
    fn: Callable[[], Awaitable[T]],
    *,
    max_attempts: int,
    base_s: float,
    max_s: float,
    on_retry: Callable[[int, BaseException], None] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rand: Callable[[], float] = random.random,
) -> tuple[T, int]:
    """Run `fn`, retrying retryable failures. Returns (result, attempts_used)."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")

    last: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await fn(), attempt
        except GatewayError as exc:
            last = exc
            if not exc.retryable or attempt == max_attempts:
                raise
            if on_retry:
                on_retry(attempt, exc)
            await sleep(backoff_delay(attempt, base_s=base_s, max_s=max_s, rand=rand))

    assert last is not None  # unreachable: the loop either returns or raises
    raise last
