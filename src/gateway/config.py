"""Configuration.

Layered: YAML file for the shape of a deployment, environment for the secrets
and per-environment knobs. Env wins, so a container can override anything
baked into the image without a rebuild.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml

DEFAULT_PRICING: dict[str, tuple[float, float]] = {
    # model -> (USD per million input tokens, USD per million output tokens).
    #
    # Anthropic first-party list rates as published on 2026-06-24. This table is
    # a DEFAULT, not a price oracle: vendor pricing changes, and partner
    # platforms (Bedrock, Vertex) bill differently. Override `pricing` in
    # config.yaml and treat that file as the source of truth for anything you
    # bill against or alert on.
    "claude-fable-5-1": (10.0, 50.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
    # Local-only models served by the mock provider, priced so the budget and
    # accounting paths are exercisable without spending anything.
    "mock-small": (0.10, 0.40),
    "mock-large": (2.00, 8.00),
}


@dataclass(frozen=True)
class ApiKey:
    key_id: str
    secret: str
    budget_usd: float | None = None
    rps: float = 5.0
    burst: int = 10


@dataclass(frozen=True)
class Settings:
    version: str = "1.0.0"
    provider: str = "mock"
    anthropic_api_key: str | None = None
    anthropic_model_allowlist: tuple[str, ...] = ()

    request_timeout_s: float = 30.0
    max_attempts: int = 3
    backoff_base_s: float = 0.2
    backoff_max_s: float = 5.0

    breaker_failure_threshold: int = 5
    breaker_reset_timeout_s: float = 20.0
    breaker_half_open_max: int = 1

    cache_enabled: bool = True
    cache_max_entries: int = 1024
    cache_ttl_s: float = 300.0

    require_auth: bool = True
    api_keys: tuple[ApiKey, ...] = field(default_factory=tuple)
    pricing: dict[str, tuple[float, float]] = field(default_factory=lambda: dict(DEFAULT_PRICING))

    def key_by_secret(self, secret: str) -> ApiKey | None:
        for k in self.api_keys:
            if k.secret == secret:
                return k
        return None


def _as_bool(raw: str) -> bool:
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def load_settings(path: str | os.PathLike[str] | None = None) -> Settings:
    """Build settings from an optional YAML file, then overlay the environment."""
    data: dict = {}
    path = path or os.environ.get("GATEWAY_CONFIG")
    if path and Path(path).exists():
        data = yaml.safe_load(Path(path).read_text()) or {}

    keys = tuple(
        ApiKey(
            key_id=k["key_id"],
            secret=k["secret"],
            budget_usd=k.get("budget_usd"),
            rps=float(k.get("rps", 5.0)),
            burst=int(k.get("burst", 10)),
        )
        for k in data.get("api_keys", [])
    )

    pricing = dict(DEFAULT_PRICING)
    for model, entry in (data.get("pricing") or {}).items():
        pricing[model] = (float(entry["input_per_mtok"]), float(entry["output_per_mtok"]))

    settings = Settings(
        provider=data.get("provider", "mock"),
        request_timeout_s=float(data.get("request_timeout_s", 30.0)),
        max_attempts=int(data.get("max_attempts", 3)),
        cache_enabled=bool(data.get("cache", {}).get("enabled", True)),
        cache_max_entries=int(data.get("cache", {}).get("max_entries", 1024)),
        cache_ttl_s=float(data.get("cache", {}).get("ttl_s", 300.0)),
        breaker_failure_threshold=int(data.get("breaker", {}).get("failure_threshold", 5)),
        breaker_reset_timeout_s=float(data.get("breaker", {}).get("reset_timeout_s", 20.0)),
        require_auth=bool(data.get("require_auth", True)),
        api_keys=keys,
        pricing=pricing,
    )

    env_overrides: dict = {}
    if v := os.environ.get("GATEWAY_PROVIDER"):
        env_overrides["provider"] = v
    if v := os.environ.get("ANTHROPIC_API_KEY"):
        env_overrides["anthropic_api_key"] = v
    if v := os.environ.get("GATEWAY_REQUEST_TIMEOUT_S"):
        env_overrides["request_timeout_s"] = float(v)
    if v := os.environ.get("GATEWAY_MAX_ATTEMPTS"):
        env_overrides["max_attempts"] = int(v)
    if v := os.environ.get("GATEWAY_REQUIRE_AUTH"):
        env_overrides["require_auth"] = _as_bool(v)
    if v := os.environ.get("GATEWAY_CACHE_ENABLED"):
        env_overrides["cache_enabled"] = _as_bool(v)

    return replace(settings, **env_overrides) if env_overrides else settings
