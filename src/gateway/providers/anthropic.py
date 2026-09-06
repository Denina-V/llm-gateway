"""Anthropic provider, over the official SDK.

Two decisions worth stating.

**The SDK's own retries are switched off** (`max_retries=0`). The gateway already
owns retry, backoff and the circuit breaker; leaving the SDK's layer on would
multiply the two -- 3 gateway attempts x 3 SDK retries is 9 upstream calls for
one client request, and the breaker would count 3 failures where 9 happened.
One retry layer, and it is the one that can see the breaker.

**Sampling parameters are model-gated.** `temperature` was removed on the
current generation (Fable 5/5.1, Opus 5, Opus 4.8/4.7, Sonnet 5) and sending it
returns a 400, while older models still accept it. A gateway whose whole purpose
is a stable surface cannot make the caller track that, so it drops the parameter
for models that reject it and says so in the logs.
"""

from __future__ import annotations

import logging
import time

import anthropic

from ..errors import AuthError, ProviderError, ProviderTimeoutError, UnknownModelError
from .base import ProviderRequest, ProviderResult

logger = logging.getLogger("gateway.provider.anthropic")

# Models on which `temperature` / `top_p` / `top_k` return a 400. Adding a model
# here is strictly safer than omitting it: the worst case is a dropped sampling
# parameter, where the worst case of omitting it is a hard 400 in production.
SAMPLING_UNSUPPORTED = frozenset(
    {
        "claude-fable-5-1",
        "claude-fable-5",
        "claude-mythos-5-1",
        "claude-opus-5",
        "claude-opus-4-8",
        "claude-opus-4-7",
        "claude-sonnet-5",
    }
)


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        timeout_s: float = 30.0,
        client: anthropic.AsyncAnthropic | None = None,
    ) -> None:
        # A zero-arg client resolves credentials from ANTHROPIC_API_KEY, then
        # ANTHROPIC_AUTH_TOKEN, then an `ant auth login` profile -- so an unset
        # env var does not mean "no credentials". Only pass api_key when the
        # deployment must pin a specific one.
        self._client = client or anthropic.AsyncAnthropic(
            **({"api_key": api_key} if api_key else {}),
            timeout=timeout_s,
            max_retries=0,
        )

    async def complete(self, request: ProviderRequest) -> ProviderResult:
        kwargs: dict = {
            "model": request.model,
            "max_tokens": request.max_tokens,
            "messages": request.messages,
        }
        if request.system:
            kwargs["system"] = request.system
        if request.temperature and request.model not in SAMPLING_UNSUPPORTED:
            kwargs["temperature"] = request.temperature
        elif request.temperature:
            logger.debug("dropped temperature for %s (parameter removed)", request.model)

        started = time.perf_counter()
        try:
            response = await self._client.messages.create(**kwargs)
        # Most specific first. Collapsing these into one `except APIStatusError`
        # would erase the retryable/non-retryable distinction the retry loop and
        # the breaker both depend on.
        except anthropic.AuthenticationError as exc:
            raise AuthError("upstream rejected the credentials") from exc
        except anthropic.PermissionDeniedError as exc:
            raise AuthError("credentials lack permission for this model") from exc
        except anthropic.NotFoundError as exc:
            raise UnknownModelError(f"upstream does not know model {request.model!r}") from exc
        except anthropic.BadRequestError as exc:
            # A malformed request will be exactly as malformed on attempt three.
            raise ProviderError(f"upstream rejected the request: {exc}", retryable=False) from exc
        except anthropic.RateLimitError as exc:
            retry_after = exc.response.headers.get("retry-after") if exc.response else None
            raise ProviderError(
                "upstream rate limited",
                retryable=True,
                detail={"retry_after": retry_after},
            ) from exc
        except anthropic.APIStatusError as exc:
            raise ProviderError(
                f"upstream returned {exc.status_code}",
                # 5xx is worth another attempt; a 4xx we have not named above is not.
                retryable=exc.status_code >= 500,
                detail={"status": exc.status_code},
            ) from exc
        except anthropic.APITimeoutError as exc:  # subclass of APIConnectionError
            raise ProviderTimeoutError(f"upstream timed out after {exc}") from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderError(f"could not reach upstream: {exc}", retryable=True) from exc

        elapsed_ms = (time.perf_counter() - started) * 1000.0

        # stop_details is populated only for a refusal, so guard before reading it.
        if response.stop_reason == "refusal":
            category = getattr(response.stop_details, "category", None)
            raise ProviderError(
                "upstream declined the request",
                retryable=False,
                detail={"stop_reason": "refusal", "category": category},
            )

        text = "".join(block.text for block in response.content if block.type == "text")
        return ProviderResult(
            content=text,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            stop_reason=response.stop_reason,
            latency_ms=elapsed_ms,
        )

    async def close(self) -> None:
        await self._client.close()
