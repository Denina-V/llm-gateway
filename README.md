# llm-gateway

**The unglamorous layer between your application and a very expensive API.**

A single LLM call is one line of code. A *fleet* of LLM calls — several teams, a
shared budget, a provider that occasionally 503s, and a finance director who wants
to know where $4,000 went — is an infrastructure problem, and it is the same
infrastructure problem every time.

This is that layer, built properly: auth, per-key rate limits, hard spend caps,
response caching, retries, and a circuit breaker, behind one stable HTTP contract.

```
    client
      │
      ▼
 ┌──────────┐   401     Each stage is a cheaper way of saying no than the
 │   auth   │──────▶    one after it, so a request is rejected as early as
 └────┬─────┘           it possibly can be. The last stage — the one that
      ▼                 costs actual money — is reached only by requests
 ┌──────────┐   429     that have earned it.
 │rate limit│──────▶
 └────┬─────┘
      ▼
 ┌──────────┐   400
 │  model   │──────▶
 └────┬─────┘
      ▼
 ┌──────────┐   HIT ─────────────────────┐   a cache hit costs nothing,
 │  cache   │                            │   so it never touches the budget
 └────┬─────┘                            │
      ▼                                  │
 ┌──────────┐   402                      │
 │  budget  │──────▶  (reserve)          │
 └────┬─────┘                            │
      ▼                                  │
 ┌──────────┐   503                      │
 │ breaker  │──────▶                     │
 └────┬─────┘                            │
      ▼                                  │
 ┌──────────┐                            │
 │  retry   │──▶ provider ──▶ settle ────┴──▶ 200
 └──────────┘
```

**It runs with no API key and no network.** The default provider is a
deterministic in-process mock, so `make run` gives you a working gateway in about
four seconds — and, more usefully, it is a fault injector, which is how the
retry, timeout and breaker paths get tested without waiting for a real outage.

---

## Quickstart

```bash
make install && make test && make run
```

```bash
curl -s -X POST localhost:8080/v1/completions \
  -H 'Authorization: Bearer sk-local-research' \
  -H 'Content-Type: application/json' \
  -d '{"model":"mock-small","messages":[{"role":"user","content":"hello"}]}' | jq
```

```json
{
  "request_id": "723f54e484514246",
  "model": "mock-small",
  "content": "[mock:7804c53d] schema cohort throughput window rollout breaker ...",
  "stop_reason": "end_turn",
  "usage": { "input_tokens": 1, "output_tokens": 52, "cost_usd": 2.09e-05 },
  "cached": false,
  "provider": "mock",
  "latency_ms": 13.27,
  "attempts": 1
}
```

Send it twice and `cached` flips to `true`. Ask for a model nobody priced and you
get a `400` naming the models that *are* priced. Point the demo key's $0.50 at
something expensive and you get a `402` — before the call goes out, not after.

To point it at the real thing: `GATEWAY_PROVIDER=anthropic` and an
`ANTHROPIC_API_KEY`. Nothing else changes — that is the point.

---

## The four decisions worth defending

Most of this is standard. These four are where a shortcut would have been quietly
wrong, and they are the parts worth reading the code for.

### 1. Budgets reserve before the call, not after

The obvious implementation — call the provider, add the cost to a running total —
has a race with teeth. Ten concurrent requests all check a $1 limit while the
balance still reads $0.60, all pass, and all spend. The limit is only enforced
against traffic that isn't concurrent, which is to say, against traffic that
wasn't going to breach it anyway.

So the ledger **reserves an upper bound before dispatching and settles the
difference once the real token counts come back**. Overshoot becomes bounded by
the estimate instead of by the concurrency.

Fire 40 concurrent requests at the $0.50 demo key, each reserving $0.032:

```
statuses: {200: 15, 402: 25}
```

Fifteen admitted — 15 x $0.032 = $0.48, which fits — and twenty-five refused,
every one of them on *held reservations*, before a single call settled:

```json
{"error": {"code": "budget_exceeded", "message": "budget exhausted for key team-demo",
           "retryable": false, "request_id": "6841699b10204db0",
           "detail": {"limit_usd": 0.5, "spent_usd": 0.0,
                      "reserved_usd": 0.48015, "requested_usd": 0.03201}}}
```

`spent_usd` is still `0.0`. A post-hoc ledger would have seen exactly that number
and waved all forty through.

`estimate_input_tokens` deliberately over-estimates. Reserving slightly too much
for a few hundred milliseconds is free; reserving too little defeats the feature.

### 2. Only the provider's failures trip the breaker

A circuit breaker that counts *every* error is a denial-of-service vector wearing
a safety vest: one team shipping malformed requests trips the breaker and takes
the upstream offline for everybody else. A `400` is not evidence that the provider
is unwell — it is evidence that one caller is.

Only `provider_error` and `provider_timeout` count. There is a test that fires
five malformed requests through a two-failure breaker and asserts it is still
closed and still serving.

The same reasoning splits the health endpoints. `/healthz` is liveness and stays
`200` — the process is fine. `/readyz` returns `503` while the circuit is open, so
an orchestrator pulls the replica out of the load balancer instead of restarting a
process that has nothing wrong with it.

### 3. Sampled requests are never cached

Caching on `(model, system, messages, max_tokens, temperature)` is the easy part.
The trap is caching a `temperature > 0` request: you serve one stored sample
forever, and sampling silently becomes a constant. It looks like a cache hit and
behaves like a correctness bug, which is the worst combination available.

Only `temperature == 0` is cacheable, and callers can opt out per request with
`"cache": false`. A cache hit also **short-circuits before the budget is touched** —
a response that costs nothing shouldn't consume anyone's quota.

### 4. One retry layer, and it is the one that can see the breaker

The Anthropic SDK retries by default. So does this gateway. Left alone, they
compose: 3 gateway attempts × 3 SDK retries is **nine** upstream calls for one
client request, and the breaker counts three failures where nine happened.

The SDK's layer is switched off (`max_retries=0`). Retry lives in exactly one
place — the place that can see the circuit breaker and the budget.

Backoff uses **full jitter** (`random() * min(max, base * 2^n)`). Fixed backoff
synchronises every stalled client onto the same instant, so a provider that just
came back up gets hit by the entire herd at once and goes down again.

---

## Measured

`make bench` drives the real ASGI app in-process, so the numbers are the
gateway's own cost with no network in them. 2,000 requests, 50 concurrent,
on an M-series laptop:

| workload | req/s | p50 | p95 | p99 | cache |
|---|---:|---:|---:|---:|---:|
| overhead — 0 ms upstream | 2,025 | 21.9 ms | 32.6 ms | 35.1 ms | 0% |
| unique — 25 ms upstream | 967 | 46.2 ms | 68.4 ms | 75.1 ms | 0% |
| repeat — 25 ms upstream | 1,819 | 22.4 ms | 46.7 ms | 53.1 ms | 97.5% |

Read the last two rows together: against a 25 ms upstream, a cacheable workload
**roughly doubles throughput and halves p50** — and every one of those hits is a
provider call that was never billed.

Two caveats, because a table of numbers invites more trust than one run earns.
The latencies include queueing at 50 in-flight requests — they are not
single-request service times. And this is **one representative run on a laptop**:
repeat it and throughput moves by roughly ±10%. The *ratio* is the stable part
and the only claim worth making. All 6,000 requests returned `200`.

## Tested

**61 tests, 87% coverage, ~1s.** No network, no API key, no sleeping — every
resilience primitive takes an injected clock, so timeouts and reset windows are
driven by advancing a variable rather than by waiting.

```
tests/test_breaker.py     state machine, including the half-open window restart
tests/test_ratelimit.py   burst, refill, the cap, per-key isolation
tests/test_cache.py       TTL, LRU eviction, key stability
tests/test_costs.py       pricing, reserve/settle, bounded concurrent overshoot
tests/test_retry.py       what is retried, what is not, jitter bounds
tests/test_api.py         the whole chain through the real ASGI app
```

The tests are written to describe behaviour, not coverage:

```
test_a_client_error_does_not_trip_the_breaker
test_sampled_requests_are_never_cached
test_an_open_breaker_makes_the_replica_unready_but_still_alive
test_a_refused_request_does_not_leave_a_reservation_behind
test_an_unpriced_model_is_refused_before_any_upstream_call
```

CI runs lint and tests on Python 3.11 and 3.12, then **builds the image, boots
the container, and requires a real `200` out of it** — a green unit suite says
nothing about whether the thing actually starts.

---

## API

| | |
|---|---|
| `POST /v1/completions` | The one that matters. Auth via `Authorization: Bearer`. |
| `GET /v1/budget` | Spend, limit and remaining for the calling key. |
| `GET /v1/models` | What the gateway will price. |
| `GET /healthz` | Liveness — `200` while the process lives. |
| `GET /readyz` | Readiness — `503` while the circuit is open. |
| `GET /metrics` | Prometheus. |

Every error is one machine-readable code with a stable HTTP status and an
explicit `retryable` flag, so callers branch on a contract instead of parsing
prose:

```json
{"error": {"code": "unknown_model", "message": "unknown model 'gpt-9'",
           "retryable": false, "request_id": "c30d6b2922ac4365",
           "detail": {"known_models": ["claude-fable-5-1", "claude-haiku-4-5", "..."]}}}
```

`unauthorized` · `unknown_model` · `rate_limited` · `budget_exceeded` ·
`circuit_open` · `provider_error` · `provider_timeout`

Metrics: `gateway_requests_total{model,outcome}`,
`gateway_cost_usd_total{model,key_id}`, `gateway_tokens_total{model,direction}`,
`gateway_request_duration_seconds`, `gateway_provider_duration_seconds`,
`gateway_cache_events_total`, `gateway_retries_total{code}`,
`gateway_circuit_breaker_state`, `gateway_inflight_requests`.

`docker compose up` brings up the gateway **and** a Prometheus already scraping
it, so that list is checkable rather than merely claimed.

Logs are one JSON object per line, every line carrying the `request_id` that is
also returned in the `x-request-id` header — which is the difference between "a
user reports a 500" and "here is the exact upstream call that produced it".

---

## Configuration

YAML for the shape of a deployment, environment for secrets and per-environment
knobs; env wins, so one image serves every environment.

```yaml
api_keys:
  - key_id: team-research
    secret: sk-local-research
    budget_usd: 25.00
    rps: 5
    burst: 10
```

`GATEWAY_PROVIDER` · `ANTHROPIC_API_KEY` · `GATEWAY_REQUIRE_AUTH` ·
`GATEWAY_MAX_ATTEMPTS` · `GATEWAY_CACHE_ENABLED` · `GATEWAY_REQUEST_TIMEOUT_S`

The built-in pricing table carries Anthropic's published rates, but it is a
**default, not a price oracle** — vendor pricing moves and partner platforms bill
differently. Override `pricing` in config and treat that file as the source of
truth for anything you bill against.

---

## What this deliberately is not

Stating the limits is part of the design, not an apology for it.

- **The ledger and the rate limiter are per process.** Both are a dict behind a
  lock. Two replicas means two budgets. Back them with Redis before scaling out —
  the interfaces are drawn so that swap is contained.
- **No streaming.** `text/event-stream` is a real feature, not a small one, and
  half of it would be worse than none.
- **The token estimator is a heuristic**, used only to size a reservation. If you
  need exact pre-flight counts, call the provider's token-counting endpoint.
- **Costs are floats.** These are fractions of a cent and this is a rate limiter,
  not a general ledger. Bill directly off these numbers and you want `Decimal`
  first.

---

MIT. Built by [Denina Vincent](https://github.com/Denina-V).
