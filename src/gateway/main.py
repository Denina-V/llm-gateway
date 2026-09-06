"""The gateway itself: auth, limits, cache, budget, breaker, retry, then upstream.

The order of those stages is the design. Each one is a cheaper way of saying no
than the one after it, so the request is rejected as early as it can be:

    auth -> rate limit -> model check -> cache -> budget -> breaker -> retry -> provider

A cache hit short-circuits before the budget is touched, because a served-from-
cache response costs nothing and should not consume anyone's quota. The breaker
sits after the budget so that a provider outage cannot silently burn reservations.
"""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from .cache import ResponseCache, cache_key
from .config import ApiKey, Settings, load_settings
from .costs import BudgetLedger, Pricing, estimate_input_tokens
from .errors import (
    AuthError,
    BudgetExceededError,
    CircuitOpenError,
    GatewayError,
    RateLimitedError,
    UnknownModelError,
)
from .observability import (
    BREAKER_STATE_VALUE,
    Metrics,
    configure_logging,
    log,
    request_id_var,
)
from .providers.base import ProviderRequest
from .providers.mock import MockProvider
from .resilience.breaker import CircuitBreaker
from .resilience.ratelimit import TokenBucketLimiter
from .resilience.retry import retry_async
from .schemas import (
    BudgetResponse,
    CompletionRequest,
    CompletionResponse,
    HealthResponse,
    Usage,
)

ANON_KEY = ApiKey(key_id="anonymous", secret="", budget_usd=None, rps=50.0, burst=100)


def build_provider(settings: Settings):
    if settings.provider == "mock":
        return MockProvider(seed=1337)
    if settings.provider == "anthropic":
        # Imported lazily so a mock-only deployment does not need the SDK present.
        from .providers.anthropic import AnthropicProvider

        return AnthropicProvider(
            api_key=settings.anthropic_api_key,
            timeout_s=settings.request_timeout_s,
        )
    raise ValueError(f"unknown provider {settings.provider!r}")


def create_app(settings: Settings | None = None, provider=None) -> FastAPI:
    """App factory.

    Everything the app depends on is constructed here and hung off `app.state`,
    so a test can build an app with a fake provider, a frozen clock and a
    hair-trigger breaker without patching a single global.
    """
    settings = settings or load_settings()
    logger = configure_logging()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        log(logger, logging.INFO, "gateway.start", provider=app.state.provider.name)
        yield
        await app.state.provider.close()
        log(logger, logging.INFO, "gateway.stop")

    app = FastAPI(
        title="llm-gateway",
        version=settings.version,
        description="Policy, cost control and failure handling in front of an LLM provider.",
        lifespan=lifespan,
    )

    app.state.settings = settings
    app.state.logger = logger
    app.state.provider = provider or build_provider(settings)
    app.state.metrics = Metrics()
    app.state.pricing = Pricing(settings.pricing)
    app.state.ledger = BudgetLedger()
    app.state.limiter = TokenBucketLimiter()
    app.state.cache = ResponseCache(
        max_entries=settings.cache_max_entries, ttl_s=settings.cache_ttl_s
    )
    app.state.breaker = CircuitBreaker(
        failure_threshold=settings.breaker_failure_threshold,
        reset_timeout_s=settings.breaker_reset_timeout_s,
        half_open_max=settings.breaker_half_open_max,
        name=app.state.provider.name,
    )
    app.state.started_at = time.monotonic()

    for key in settings.api_keys:
        app.state.ledger.register(key.key_id, key.budget_usd)

    # ---------------------------------------------------------------- plumbing

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        """Stamp every request with an id and log one line per request.

        The id is echoed in the response header and in every log line and error
        body for that request, which is the difference between "a user reports a
        500" and "here is the exact upstream call that produced it".
        """
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        token = request_id_var.set(rid)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        response.headers["x-request-id"] = rid
        log(
            logger,
            logging.INFO,
            "http.request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=round(elapsed_ms, 2),
        )
        return response

    @app.exception_handler(GatewayError)
    async def gateway_error_handler(request: Request, exc: GatewayError):
        headers = {}
        # A 429 without Retry-After tells the client to back off but not by how
        # much, so every client invents its own answer and they all get it wrong.
        if (wait := exc.detail.get("retry_after_s")) is not None:
            headers["retry-after"] = str(max(1, int(float(wait) + 0.999)))
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.as_payload(request_id_var.get()),
            headers=headers,
        )

    def authenticate(request: Request) -> ApiKey:
        if not settings.require_auth:
            return ANON_KEY
        header = request.headers.get("authorization", "")
        secret = header[7:].strip() if header.lower().startswith("bearer ") else ""
        secret = secret or request.headers.get("x-api-key", "")
        if not secret:
            raise AuthError("missing credentials: send Authorization: Bearer <key>")
        key = settings.key_by_secret(secret)
        if key is None:
            raise AuthError("unrecognised API key")
        return key

    # ------------------------------------------------------------------ routes

    @app.get("/healthz", response_model=HealthResponse)
    async def healthz() -> HealthResponse:
        """Liveness. Cheap, dependency-free, and always 200 while the process runs."""
        breaker_state = app.state.breaker.state.value
        return HealthResponse(
            status="ok" if breaker_state == "closed" else "degraded",
            version=settings.version,
            providers={app.state.provider.name: breaker_state},
            uptime_s=round(time.monotonic() - app.state.started_at, 3),
        )

    @app.get("/readyz")
    async def readyz() -> Response:
        """Readiness. 503 while the breaker is open, so an orchestrator can pull
        this replica out of rotation instead of routing traffic at a dead upstream."""
        state = app.state.breaker.state.value
        ready = state != "open"
        return JSONResponse(
            status_code=200 if ready else 503,
            content={"ready": ready, "breaker": state},
        )

    @app.get("/metrics")
    async def metrics() -> Response:
        app.state.metrics.breaker_state.labels(provider=app.state.provider.name).set(
            BREAKER_STATE_VALUE[app.state.breaker.state.value]
        )
        return PlainTextResponse(
            app.state.metrics.render().decode(),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    @app.get("/v1/models")
    async def models() -> dict:
        return {"models": app.state.pricing.models(), "provider": app.state.provider.name}

    @app.get("/v1/budget", response_model=BudgetResponse)
    async def budget(key: ApiKey = Depends(authenticate)) -> BudgetResponse:
        ledger = app.state.ledger.snapshot(key.key_id)
        return BudgetResponse(
            key_id=ledger.key_id,
            spent_usd=round(ledger.spent_usd, 6),
            limit_usd=ledger.limit_usd,
            remaining_usd=(
                None if ledger.remaining_usd is None else round(ledger.remaining_usd, 6)
            ),
            requests=ledger.requests,
        )

    @app.post("/v1/completions", response_model=CompletionResponse)
    async def completions(
        body: CompletionRequest,
        key: ApiKey = Depends(authenticate),
    ) -> CompletionResponse:
        m: Metrics = app.state.metrics
        pricing: Pricing = app.state.pricing
        ledger: BudgetLedger = app.state.ledger
        rid = request_id_var.get()
        started = time.perf_counter()
        m.inflight.inc()
        try:
            allowed, retry_after = app.state.limiter.acquire(
                key.key_id, rate=key.rps, burst=key.burst
            )
            if not allowed:
                m.rate_limited.labels(key_id=key.key_id).inc()
                m.requests.labels(model=body.model, outcome="rate_limited").inc()
                raise RateLimitedError(
                    "rate limit exceeded",
                    detail={"retry_after_s": round(retry_after, 3), "rps": key.rps},
                )

            if not pricing.known(body.model):
                m.requests.labels(model=body.model, outcome="unknown_model").inc()
                raise UnknownModelError(
                    f"unknown model {body.model!r}",
                    detail={"known_models": pricing.models()},
                )

            messages = [msg.model_dump() for msg in body.messages]

            # Only deterministic requests are cacheable: replaying one sample of a
            # temperature>0 call would quietly turn sampling into a constant.
            cacheable = settings.cache_enabled and body.cache and body.temperature == 0.0
            key_hash = cache_key(
                {
                    "model": body.model,
                    "system": body.system,
                    "messages": messages,
                    "max_tokens": body.max_tokens,
                    "temperature": body.temperature,
                }
            )
            if cacheable and (hit := app.state.cache.get(key_hash)) is not None:
                m.cache_events.labels(event="hit").inc()
                m.requests.labels(model=body.model, outcome="cache_hit").inc()
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                m.latency.labels(model=body.model).observe(elapsed_ms / 1000.0)
                log(logger, logging.INFO, "completion.cache_hit", model=body.model,
                    key_id=key.key_id)
                return hit.model_copy(
                    update={"request_id": rid, "cached": True, "latency_ms": round(elapsed_ms, 2)}
                )
            if cacheable:
                m.cache_events.labels(event="miss").inc()

            # Reserve the worst case before dispatching. See costs.BudgetLedger
            # for why this is not a post-hoc increment.
            est_in = estimate_input_tokens(body.system, messages)
            reserved = pricing.max_cost_usd(body.model, est_in, body.max_tokens)
            ledger.reserve(key.key_id, reserved)

            settled = False
            try:
                if not app.state.breaker.allow():
                    m.requests.labels(model=body.model, outcome="circuit_open").inc()
                    raise CircuitOpenError(
                        f"circuit open for provider {app.state.provider.name}",
                        detail={"state": app.state.breaker.state.value},
                    )

                preq = ProviderRequest(
                    model=body.model,
                    messages=messages,
                    system=body.system,
                    max_tokens=body.max_tokens,
                    temperature=body.temperature,
                )

                def on_retry(attempt: int, exc: BaseException) -> None:
                    code = getattr(exc, "code", "unknown")
                    m.retries.labels(code=code).inc()
                    log(logger, logging.WARNING, "provider.retry", attempt=attempt,
                        code=code, model=body.model)

                async def call():
                    upstream_started = time.perf_counter()
                    try:
                        return await app.state.provider.complete(preq)
                    finally:
                        m.provider_latency.labels(
                            provider=app.state.provider.name, model=body.model
                        ).observe(time.perf_counter() - upstream_started)

                try:
                    result, attempts = await retry_async(
                        call,
                        max_attempts=settings.max_attempts,
                        base_s=settings.backoff_base_s,
                        max_s=settings.backoff_max_s,
                        on_retry=on_retry,
                    )
                except GatewayError as exc:
                    # Only failures that are actually the provider's fault should
                    # trip the breaker. Counting a 400 towards it would let one
                    # malformed client take the upstream out for everyone.
                    if getattr(exc, "code", "") in {"provider_error", "provider_timeout"}:
                        app.state.breaker.record_failure()
                    m.requests.labels(
                        model=body.model, outcome=getattr(exc, "code", "error")
                    ).inc()
                    raise

                app.state.breaker.record_success()

                cost = pricing.cost_usd(body.model, result.input_tokens, result.output_tokens)
                ledger.settle(key.key_id, reserved, cost)
                settled = True
            finally:
                if not settled:
                    ledger.release(key.key_id, reserved)

            elapsed_ms = (time.perf_counter() - started) * 1000.0
            payload = CompletionResponse(
                request_id=rid,
                model=body.model,
                content=result.content,
                stop_reason=result.stop_reason,
                usage=Usage(
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    cost_usd=round(cost, 8),
                ),
                cached=False,
                provider=app.state.provider.name,
                latency_ms=round(elapsed_ms, 2),
                attempts=attempts,
            )
            if cacheable:
                app.state.cache.set(key_hash, payload)

            m.requests.labels(model=body.model, outcome="ok").inc()
            m.latency.labels(model=body.model).observe(elapsed_ms / 1000.0)
            m.tokens.labels(model=body.model, direction="input").inc(result.input_tokens)
            m.tokens.labels(model=body.model, direction="output").inc(result.output_tokens)
            m.cost.labels(model=body.model, key_id=key.key_id).inc(cost)
            log(logger, logging.INFO, "completion.ok", model=body.model, key_id=key.key_id,
                cost_usd=round(cost, 8), attempts=attempts,
                input_tokens=result.input_tokens, output_tokens=result.output_tokens)
            return payload
        except BudgetExceededError:
            m.requests.labels(model=body.model, outcome="budget_exceeded").inc()
            raise
        finally:
            m.inflight.dec()

    return app


app = create_app()
