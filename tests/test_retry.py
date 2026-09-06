import pytest

from gateway.errors import ProviderError, ProviderTimeoutError, UnknownModelError
from gateway.resilience.retry import backoff_delay, retry_async


async def noop_sleep(_seconds: float) -> None:
    return None


def test_backoff_doubles_and_is_capped():
    # rand pinned to 1.0 so the ceiling itself is observable.
    d = [backoff_delay(i, base_s=0.2, max_s=1.0, rand=lambda: 1.0) for i in (1, 2, 3, 4, 5)]
    assert d == [0.2, 0.4, 0.8, 1.0, 1.0]


def test_full_jitter_spreads_retries_across_the_window():
    """Un-jittered backoff synchronises every stalled client onto the same
    instant, so a recovering provider is hit by the whole herd at once."""
    lo = backoff_delay(3, base_s=0.2, max_s=10.0, rand=lambda: 0.0)
    hi = backoff_delay(3, base_s=0.2, max_s=10.0, rand=lambda: 1.0)
    assert lo == 0.0 and hi == 0.8


@pytest.mark.asyncio
async def test_returns_on_first_success_without_retrying():
    calls = 0

    async def fn():
        nonlocal calls
        calls += 1
        return "ok"

    result, attempts = await retry_async(fn, max_attempts=3, base_s=0, max_s=0, sleep=noop_sleep)
    assert (result, attempts, calls) == ("ok", 1, 1)


@pytest.mark.asyncio
async def test_retries_a_retryable_failure_then_succeeds():
    calls = 0

    async def fn():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise ProviderTimeoutError("upstream timed out")
        return "ok"

    result, attempts = await retry_async(fn, max_attempts=5, base_s=0, max_s=0, sleep=noop_sleep)
    assert (result, attempts, calls) == ("ok", 3, 3)


@pytest.mark.asyncio
async def test_a_non_retryable_error_is_raised_on_the_first_attempt():
    """A 400 will be exactly as malformed on attempt three; retrying it only
    spends money and delays the error the caller needs to see."""
    calls = 0

    async def fn():
        nonlocal calls
        calls += 1
        raise UnknownModelError("no such model")

    with pytest.raises(UnknownModelError):
        await retry_async(fn, max_attempts=5, base_s=0, max_s=0, sleep=noop_sleep)
    assert calls == 1


@pytest.mark.asyncio
async def test_gives_up_after_max_attempts_and_reraises():
    calls = 0

    async def fn():
        nonlocal calls
        calls += 1
        raise ProviderError("503", retryable=True)

    with pytest.raises(ProviderError):
        await retry_async(fn, max_attempts=3, base_s=0, max_s=0, sleep=noop_sleep)
    assert calls == 3


@pytest.mark.asyncio
async def test_on_retry_fires_once_per_retry_not_per_attempt():
    seen = []

    async def fn():
        raise ProviderError("503", retryable=True)

    with pytest.raises(ProviderError):
        await retry_async(
            fn, max_attempts=3, base_s=0, max_s=0, sleep=noop_sleep,
            on_retry=lambda attempt, exc: seen.append(attempt),
        )
    assert seen == [1, 2], "3 attempts means 2 retries"
