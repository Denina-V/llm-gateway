"""Deterministic in-process provider.

Exists so the gateway is fully runnable, testable and demonstrable with no API
key, no network and no spend -- `docker compose up` and the thing works. It is
also the fault injector: the resilience code paths (retry, breaker, timeout)
are otherwise only exercised during a real outage, which is a bad time to find
out whether they work.

Same request in, same response out, so cache behaviour is observable.
"""

from __future__ import annotations

import asyncio
import hashlib
import random

from ..errors import ProviderError, ProviderTimeoutError
from .base import ProviderRequest, ProviderResult

_WORDS = (
    "signal", "noise", "latency", "budget", "retry", "breaker", "cadence",
    "throughput", "drift", "ingest", "contract", "schema", "window", "cohort",
    "baseline", "anomaly", "threshold", "rollout",
)


class MockProvider:
    name = "mock"

    def __init__(
        self,
        *,
        latency_ms: float = 12.0,
        failure_rate: float = 0.0,
        timeout_rate: float = 0.0,
        seed: int | None = None,
    ) -> None:
        self.latency_ms = latency_ms
        self.failure_rate = failure_rate
        self.timeout_rate = timeout_rate
        self._rng = random.Random(seed)
        self.calls = 0

    async def complete(self, request: ProviderRequest) -> ProviderResult:
        self.calls += 1

        if self.timeout_rate and self._rng.random() < self.timeout_rate:
            raise ProviderTimeoutError("mock provider timed out")
        if self.failure_rate and self._rng.random() < self.failure_rate:
            raise ProviderError("mock provider returned 503", retryable=True)

        if self.latency_ms:
            await asyncio.sleep(self.latency_ms / 1000.0)

        prompt = "\n".join(m["content"] for m in request.messages)
        digest = hashlib.sha256(
            f"{request.model}|{request.system}|{prompt}|{request.temperature}".encode()
        ).hexdigest()

        # Derive the reply from the digest so it is stable per request but
        # visibly different across requests.
        rng = random.Random(int(digest[:16], 16))
        n_words = min(request.max_tokens, 24)
        body = " ".join(rng.choice(_WORDS) for _ in range(n_words))
        content = f"[mock:{digest[:8]}] {body}."

        input_tokens = max(1, len(prompt) // 4 + len(request.system or "") // 4)
        output_tokens = max(1, len(content) // 4)
        return ProviderResult(
            content=content,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            stop_reason="end_turn",
            latency_ms=self.latency_ms,
        )

    async def close(self) -> None:
        return None
