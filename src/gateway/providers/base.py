"""The seam between the gateway and whoever is actually serving tokens.

Everything vendor-shaped -- auth headers, request envelopes, error formats --
stops here. The rest of the gateway only ever sees ProviderResult and the
error taxonomy, which is what makes swapping or shadowing a provider a
configuration change rather than a refactor.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class ProviderRequest:
    model: str
    messages: list[dict]
    system: str | None
    max_tokens: int
    temperature: float


@dataclass(frozen=True)
class ProviderResult:
    content: str
    input_tokens: int
    output_tokens: int
    stop_reason: str | None
    latency_ms: float


@runtime_checkable
class Provider(Protocol):
    name: str

    async def complete(self, request: ProviderRequest) -> ProviderResult: ...

    async def close(self) -> None: ...
