from gateway.resilience.ratelimit import TokenBucketLimiter
from helpers import FakeClock


def test_burst_is_spendable_immediately():
    lim = TokenBucketLimiter(clock=FakeClock())
    for _ in range(5):
        assert lim.acquire("k", rate=1.0, burst=5)[0]
    allowed, retry_after = lim.acquire("k", rate=1.0, burst=5)
    assert not allowed
    assert retry_after == 1.0, "one more token at 1/s is one second away"


def test_bucket_refills_at_the_configured_rate():
    clock = FakeClock()
    lim = TokenBucketLimiter(clock=clock)
    for _ in range(5):
        lim.acquire("k", rate=2.0, burst=5)
    assert not lim.acquire("k", rate=2.0, burst=5)[0]
    clock.advance(0.5)  # 0.5s at 2/s == 1 token
    assert lim.acquire("k", rate=2.0, burst=5)[0]
    assert not lim.acquire("k", rate=2.0, burst=5)[0]


def test_refill_is_capped_at_burst():
    clock = FakeClock()
    lim = TokenBucketLimiter(clock=clock)
    lim.acquire("k", rate=10.0, burst=3)
    clock.advance(3600)  # an hour of idling does not bank an hour of tokens
    for _ in range(3):
        assert lim.acquire("k", rate=10.0, burst=3)[0]
    assert not lim.acquire("k", rate=10.0, burst=3)[0]


def test_buckets_are_isolated_per_key():
    lim = TokenBucketLimiter(clock=FakeClock())
    for _ in range(3):
        lim.acquire("noisy", rate=1.0, burst=3)
    assert not lim.acquire("noisy", rate=1.0, burst=3)[0]
    assert lim.acquire("quiet", rate=1.0, burst=3)[0], "one key must not starve another"
