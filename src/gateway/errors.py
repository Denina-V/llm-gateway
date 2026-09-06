"""Error taxonomy.

Every failure the gateway can produce maps to exactly one of these, so callers
can branch on a stable machine-readable code instead of parsing prose. The HTTP
status is a property of the error, not of the call site that raises it.
"""

from __future__ import annotations


class GatewayError(Exception):
    """Base class. `code` is the contract; `message` is for humans."""

    code = "internal_error"
    status_code = 500
    retryable = False

    def __init__(self, message: str, *, detail: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or {}

    def as_payload(self, request_id: str) -> dict:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "retryable": self.retryable,
                "request_id": request_id,
                **({"detail": self.detail} if self.detail else {}),
            }
        }


class AuthError(GatewayError):
    code = "unauthorized"
    status_code = 401


class UnknownModelError(GatewayError):
    code = "unknown_model"
    status_code = 400


class RateLimitedError(GatewayError):
    code = "rate_limited"
    status_code = 429
    retryable = True


class BudgetExceededError(GatewayError):
    code = "budget_exceeded"
    status_code = 402


class CircuitOpenError(GatewayError):
    code = "circuit_open"
    status_code = 503
    retryable = True


class ProviderError(GatewayError):
    """Upstream said no. `retryable` decides whether the retry loop tries again."""

    code = "provider_error"
    status_code = 502

    def __init__(
        self, message: str, *, retryable: bool = False, detail: dict | None = None
    ) -> None:
        super().__init__(message, detail=detail)
        self.retryable = retryable


class ProviderTimeoutError(ProviderError):
    code = "provider_timeout"
    status_code = 504

    def __init__(self, message: str, *, detail: dict | None = None) -> None:
        super().__init__(message, retryable=True, detail=detail)
