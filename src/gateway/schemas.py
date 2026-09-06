"""Wire contract.

Deliberately narrower than any single vendor's API: the gateway's job is to be
the stable surface that survives a provider swap, so anything vendor-specific
stays behind the provider interface.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=100_000)


class CompletionRequest(BaseModel):
    model: str = Field(min_length=1, max_length=128)
    messages: list[Message] = Field(min_length=1, max_length=200)
    system: str | None = Field(default=None, max_length=100_000)
    max_tokens: int = Field(default=1024, ge=1, le=64_000)
    temperature: float = Field(default=0.0, ge=0.0, le=1.0)
    # Opt out of the cache for calls whose whole point is a fresh sample.
    cache: bool = True


class Usage(BaseModel):
    input_tokens: int
    output_tokens: int
    cost_usd: float


class CompletionResponse(BaseModel):
    request_id: str
    model: str
    content: str
    stop_reason: str | None = None
    usage: Usage
    cached: bool = False
    provider: str
    latency_ms: float
    attempts: int = 1


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    version: str
    providers: dict[str, str]
    uptime_s: float


class BudgetResponse(BaseModel):
    key_id: str
    spent_usd: float
    limit_usd: float | None
    remaining_usd: float | None
    requests: int
