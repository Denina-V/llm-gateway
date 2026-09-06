"""End-to-end tests through the real ASGI app.

Every one of these drives the app the way a client would, with a stub provider
in place of the network. That is the point of the app factory: no monkeypatching
of globals, no live upstream, no API key.
"""

import pytest
from fastapi.testclient import TestClient

from gateway.config import ApiKey, Settings
from gateway.errors import ProviderError, ProviderTimeoutError
from gateway.main import create_app
from gateway.providers.base import ProviderResult
from gateway.providers.mock import MockProvider

KEY = ApiKey(key_id="team-a", secret="sk-test-a", budget_usd=None, rps=100.0, burst=200)
TINY_BUDGET = ApiKey(key_id="team-b", secret="sk-test-b", budget_usd=0.0005, rps=100.0, burst=200)
SLOW_KEY = ApiKey(key_id="team-c", secret="sk-test-c", budget_usd=None, rps=1.0, burst=2)

AUTH = {"Authorization": "Bearer sk-test-a"}
BODY = {"model": "mock-small", "messages": [{"role": "user", "content": "hello"}]}


class FlakyProvider:
    """Fails a fixed number of times, then succeeds. Deterministic by design."""

    name = "flaky"

    def __init__(self, failures: int, exc=None) -> None:
        self.remaining = failures
        self.exc = exc or ProviderError("upstream 503", retryable=True)
        self.calls = 0

    async def complete(self, request):
        self.calls += 1
        if self.remaining > 0:
            self.remaining -= 1
            raise self.exc
        return ProviderResult(
            content="recovered", input_tokens=10, output_tokens=5,
            stop_reason="end_turn", latency_ms=1.0,
        )

    async def close(self):
        return None


def build(provider=None, **overrides) -> TestClient:
    settings = Settings(
        provider="mock",
        api_keys=(KEY, TINY_BUDGET, SLOW_KEY),
        backoff_base_s=0.0,
        backoff_max_s=0.0,
        **overrides,
    )
    return TestClient(create_app(settings, provider or MockProvider(latency_ms=0, seed=7)))


# ----------------------------------------------------------------------- auth

def test_a_request_without_credentials_is_rejected():
    r = build().post("/v1/completions", json=BODY)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"


def test_a_bad_key_is_rejected():
    r = build().post("/v1/completions", json=BODY, headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_auth_can_be_disabled_for_a_trusted_network():
    r = build(require_auth=False).post("/v1/completions", json=BODY)
    assert r.status_code == 200


# ------------------------------------------------------------------ happy path

def test_a_completion_returns_content_usage_and_a_request_id():
    r = build().post("/v1/completions", json=BODY, headers=AUTH)
    assert r.status_code == 200
    payload = r.json()
    assert payload["content"]
    assert payload["usage"]["input_tokens"] > 0
    assert payload["usage"]["cost_usd"] > 0
    assert payload["request_id"] == r.headers["x-request-id"]
    assert payload["attempts"] == 1


def test_the_caller_can_supply_its_own_request_id_for_tracing():
    r = build().post(
        "/v1/completions", json=BODY, headers={**AUTH, "x-request-id": "trace-abc"}
    )
    assert r.headers["x-request-id"] == "trace-abc"
    assert r.json()["request_id"] == "trace-abc"


def test_an_unpriced_model_is_refused_before_any_upstream_call():
    provider = MockProvider(latency_ms=0)
    client = build(provider)
    r = client.post("/v1/completions", json={**BODY, "model": "gpt-imaginary"}, headers=AUTH)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "unknown_model"
    assert provider.calls == 0, "an unknown model must not reach the provider"


@pytest.mark.parametrize(
    "bad",
    [
        {"model": "mock-small", "messages": []},
        {"model": "mock-small", "messages": [{"role": "system", "content": "x"}]},
        {"model": "mock-small", "messages": [{"role": "user", "content": "x"}], "max_tokens": 0},
        {"model": "mock-small", "messages": [{"role": "user", "content": "x"}], "temperature": 3},
    ],
)
def test_malformed_requests_are_rejected_by_the_schema(bad):
    assert build().post("/v1/completions", json=bad, headers=AUTH).status_code == 422


# ---------------------------------------------------------------------- cache

def test_an_identical_deterministic_request_is_served_from_cache():
    provider = MockProvider(latency_ms=0, seed=7)
    client = build(provider)
    first = client.post("/v1/completions", json=BODY, headers=AUTH).json()
    second = client.post("/v1/completions", json=BODY, headers=AUTH).json()
    assert first["cached"] is False and second["cached"] is True
    assert second["content"] == first["content"]
    assert provider.calls == 1, "the second request must not reach the provider"


def test_a_cache_hit_costs_the_caller_nothing():
    client = build()
    client.post("/v1/completions", json=BODY, headers=AUTH)
    after_first = client.get("/v1/budget", headers=AUTH).json()["spent_usd"]
    client.post("/v1/completions", json=BODY, headers=AUTH)
    after_second = client.get("/v1/budget", headers=AUTH).json()["spent_usd"]
    assert after_second == after_first


def test_sampled_requests_are_never_cached():
    """Serving one stored sample for a temperature>0 call would silently turn
    sampling into a constant -- a correctness bug disguised as a speed-up."""
    provider = MockProvider(latency_ms=0, seed=7)
    client = build(provider)
    hot = {**BODY, "temperature": 0.9}
    client.post("/v1/completions", json=hot, headers=AUTH)
    second = client.post("/v1/completions", json=hot, headers=AUTH).json()
    assert second["cached"] is False
    assert provider.calls == 2


def test_a_caller_can_opt_out_of_the_cache():
    provider = MockProvider(latency_ms=0, seed=7)
    client = build(provider)
    client.post("/v1/completions", json=BODY, headers=AUTH)
    r = client.post("/v1/completions", json={**BODY, "cache": False}, headers=AUTH).json()
    assert r["cached"] is False
    assert provider.calls == 2


def test_a_different_prompt_is_a_different_cache_entry():
    provider = MockProvider(latency_ms=0, seed=7)
    client = build(provider)
    client.post("/v1/completions", json=BODY, headers=AUTH)
    other = {"model": "mock-small", "messages": [{"role": "user", "content": "different"}]}
    assert client.post("/v1/completions", json=other, headers=AUTH).json()["cached"] is False
    assert provider.calls == 2


# --------------------------------------------------------------- rate limiting

def test_exceeding_the_burst_returns_429_with_a_retry_after_header():
    client = build()
    auth_c = {"Authorization": "Bearer sk-test-c"}  # rps=1, burst=2
    codes = []
    for i in range(4):
        # Distinct prompts, so the cache cannot mask the limiter.
        body = {"model": "mock-small", "messages": [{"role": "user", "content": f"m{i}"}]}
        codes.append(client.post("/v1/completions", json=body, headers=auth_c).status_code)
    assert codes[:2] == [200, 200]
    assert 429 in codes
    last = client.post(
        "/v1/completions",
        json={"model": "mock-small", "messages": [{"role": "user", "content": "z"}]},
        headers=auth_c,
    )
    assert last.status_code == 429
    assert int(last.headers["retry-after"]) >= 1


def test_one_noisy_key_does_not_throttle_another():
    client = build()
    auth_c = {"Authorization": "Bearer sk-test-c"}
    for i in range(5):
        client.post(
            "/v1/completions",
            json={"model": "mock-small", "messages": [{"role": "user", "content": f"n{i}"}]},
            headers=auth_c,
        )
    assert client.post("/v1/completions", json=BODY, headers=AUTH).status_code == 200


# --------------------------------------------------------------------- budgets

def test_a_key_over_its_budget_is_refused_with_402():
    client = build()
    auth_b = {"Authorization": "Bearer sk-test-b"}  # budget_usd = 0.0005
    codes = []
    for i in range(12):
        body = {
            "model": "mock-large",
            "messages": [{"role": "user", "content": f"spend {i}"}],
            "max_tokens": 1000,
        }
        codes.append(client.post("/v1/completions", json=body, headers=auth_b).status_code)
    assert 402 in codes, "the budget must eventually refuse a request"
    detail = client.post(
        "/v1/completions",
        json={"model": "mock-large", "messages": [{"role": "user", "content": "again"}],
              "max_tokens": 1000},
        headers=auth_b,
    ).json()["error"]
    assert detail["code"] == "budget_exceeded"
    assert detail["detail"]["limit_usd"] == 0.0005


def test_budget_endpoint_reports_spend_for_the_calling_key_only():
    client = build()
    client.post("/v1/completions", json=BODY, headers=AUTH)
    mine = client.get("/v1/budget", headers=AUTH).json()
    assert mine["key_id"] == "team-a"
    assert mine["spent_usd"] > 0
    assert mine["requests"] == 1
    theirs = client.get("/v1/budget", headers={"Authorization": "Bearer sk-test-b"}).json()
    assert theirs["spent_usd"] == 0.0


def test_a_refused_request_does_not_leave_a_reservation_behind():
    """If the release path leaked, a key would slowly lose headroom to failures
    it was never charged for."""
    provider = FlakyProvider(failures=99)
    client = build(provider, max_attempts=1)
    for _ in range(3):
        client.post("/v1/completions", json=BODY, headers=AUTH)
    # Every reservation was released, so a fresh request still gets through.
    ok_client = build()
    assert ok_client.post("/v1/completions", json=BODY, headers=AUTH).status_code == 200


# ------------------------------------------------------------ retry & breaker

def test_a_transient_failure_is_retried_and_reported_in_attempts():
    provider = FlakyProvider(failures=2)
    r = build(provider, max_attempts=3).post("/v1/completions", json=BODY, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["attempts"] == 3
    assert provider.calls == 3


def test_a_timeout_is_retried_too():
    provider = FlakyProvider(failures=1, exc=ProviderTimeoutError("upstream timed out"))
    r = build(provider, max_attempts=3).post("/v1/completions", json=BODY, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["attempts"] == 2


def test_a_non_retryable_provider_error_is_not_retried():
    provider = FlakyProvider(failures=99, exc=ProviderError("bad request", retryable=False))
    r = build(provider, max_attempts=3).post("/v1/completions", json=BODY, headers=AUTH)
    assert r.status_code == 502
    assert provider.calls == 1, "one attempt, not three"


def test_sustained_failure_opens_the_breaker_and_stops_calling_upstream():
    provider = FlakyProvider(failures=10_000)
    client = build(provider, max_attempts=1, breaker_failure_threshold=3)
    for i in range(3):
        body = {"model": "mock-small", "messages": [{"role": "user", "content": f"f{i}"}]}
        assert client.post("/v1/completions", json=body, headers=AUTH).status_code == 502
    assert provider.calls == 3

    r = client.post(
        "/v1/completions",
        json={"model": "mock-small", "messages": [{"role": "user", "content": "after"}]},
        headers=AUTH,
    )
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "circuit_open"
    assert provider.calls == 3, "an open breaker must not touch the provider at all"


def test_an_open_breaker_makes_the_replica_unready_but_still_alive():
    """Liveness and readiness answer different questions: the process is fine,
    it just should not be receiving traffic."""
    provider = FlakyProvider(failures=10_000)
    client = build(provider, max_attempts=1, breaker_failure_threshold=2)
    for i in range(2):
        client.post(
            "/v1/completions",
            json={"model": "mock-small", "messages": [{"role": "user", "content": f"x{i}"}]},
            headers=AUTH,
        )
    assert client.get("/readyz").status_code == 503
    assert client.get("/healthz").status_code == 200
    assert client.get("/healthz").json()["status"] == "degraded"


def test_a_client_error_does_not_trip_the_breaker():
    """One caller sending malformed requests must not take the upstream out for
    everyone else."""
    provider = MockProvider(latency_ms=0)
    client = build(provider, breaker_failure_threshold=2)
    for _ in range(5):
        client.post("/v1/completions", json={**BODY, "model": "nope"}, headers=AUTH)
    assert client.get("/readyz").json()["breaker"] == "closed"
    assert client.post("/v1/completions", json=BODY, headers=AUTH).status_code == 200


# --------------------------------------------------------------- observability

def test_metrics_expose_counters_in_prometheus_format():
    client = build()
    client.post("/v1/completions", json=BODY, headers=AUTH)
    body = client.get("/metrics").text
    assert "gateway_requests_total" in body
    assert 'outcome="ok"' in body
    assert "gateway_cost_usd_total" in body
    assert "gateway_request_duration_seconds_bucket" in body


def test_metrics_record_the_cache_hit():
    client = build()
    client.post("/v1/completions", json=BODY, headers=AUTH)
    client.post("/v1/completions", json=BODY, headers=AUTH)
    assert 'gateway_cache_events_total{event="hit"} 1.0' in client.get("/metrics").text


def test_healthz_reports_the_provider_and_version():
    payload = build().get("/healthz").json()
    assert payload["status"] == "ok"
    assert payload["uptime_s"] >= 0
    assert "mock" in payload["providers"]


def test_models_endpoint_lists_what_the_gateway_will_price():
    models = build().get("/v1/models").json()["models"]
    assert "claude-opus-5" in models
    assert "mock-small" in models
