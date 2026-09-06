from gateway.cache import ResponseCache, cache_key
from helpers import FakeClock


def test_key_is_order_independent_but_value_sensitive():
    a = cache_key({"model": "m", "messages": [{"role": "user", "content": "hi"}]})
    b = cache_key({"messages": [{"role": "user", "content": "hi"}], "model": "m"})
    c = cache_key({"model": "m", "messages": [{"role": "user", "content": "hi "}]})
    assert a == b, "dict ordering must not change the key"
    assert a != c, "a trailing space is a different prompt"


def test_entries_expire_on_ttl():
    clock = FakeClock()
    cache = ResponseCache(ttl_s=60.0, clock=clock)
    cache.set("k", "v")
    clock.advance(59)
    assert cache.get("k") == "v"
    clock.advance(2)
    assert cache.get("k") is None


def test_lru_evicts_the_least_recently_used():
    cache = ResponseCache(max_entries=2, clock=FakeClock())
    cache.set("a", 1)
    cache.set("b", 2)
    cache.get("a")       # 'a' is now the most recently used, so 'b' is next out
    cache.set("c", 3)
    assert cache.get("a") == 1
    assert cache.get("b") is None
    assert cache.get("c") == 3


def test_hit_rate_tracks_hits_and_misses():
    cache = ResponseCache(clock=FakeClock())
    cache.set("a", 1)
    cache.get("a")
    cache.get("nope")
    assert cache.hits == 1 and cache.misses == 1
    assert cache.hit_rate == 0.5


def test_hit_rate_of_an_untouched_cache_is_zero_not_a_crash():
    assert ResponseCache(clock=FakeClock()).hit_rate == 0.0
