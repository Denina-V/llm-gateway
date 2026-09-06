"""Structured logs and Prometheus metrics.

Logs are JSON on one line each, because the first thing anyone does with them is
ship them somewhere that wants to index fields, and a human-friendly format that
has to be regex-parsed later is a false economy. Every line carries the
request_id, so one call is greppable end to end.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
import time
from typing import Any

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": request_id_var.get(),
        }
        if extra := getattr(record, "fields", None):
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO") -> logging.Logger:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger("gateway")
    root.handlers = [handler]
    root.setLevel(level.upper())
    root.propagate = False
    return root


def log(logger: logging.Logger, level: int, msg: str, **fields: Any) -> None:
    logger.log(level, msg, extra={"fields": fields})


class Metrics:
    """Metric definitions live in one place so the names stay consistent.

    A fresh registry per instance keeps tests from tripping over duplicate
    timeseries registration in the default global registry.
    """

    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry()

        self.requests = Counter(
            "gateway_requests_total",
            "Completion requests by outcome.",
            ["model", "outcome"],
            registry=self.registry,
        )
        self.latency = Histogram(
            "gateway_request_duration_seconds",
            "End-to-end request latency, gateway edge to edge.",
            ["model"],
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
            registry=self.registry,
        )
        self.provider_latency = Histogram(
            "gateway_provider_duration_seconds",
            "Upstream provider latency, excluding gateway overhead.",
            ["provider", "model"],
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
            registry=self.registry,
        )
        self.tokens = Counter(
            "gateway_tokens_total",
            "Tokens billed, by direction.",
            ["model", "direction"],
            registry=self.registry,
        )
        self.cost = Counter(
            "gateway_cost_usd_total",
            "Cumulative spend in USD.",
            ["model", "key_id"],
            registry=self.registry,
        )
        self.cache_events = Counter(
            "gateway_cache_events_total",
            "Cache hits and misses.",
            ["event"],
            registry=self.registry,
        )
        self.retries = Counter(
            "gateway_retries_total",
            "Retried provider attempts, by error code.",
            ["code"],
            registry=self.registry,
        )
        self.rate_limited = Counter(
            "gateway_rate_limited_total",
            "Requests rejected by the token bucket.",
            ["key_id"],
            registry=self.registry,
        )
        self.breaker_state = Gauge(
            "gateway_circuit_breaker_state",
            "Circuit breaker state: 0=closed, 1=half_open, 2=open.",
            ["provider"],
            registry=self.registry,
        )
        self.inflight = Gauge(
            "gateway_inflight_requests",
            "Requests currently being served.",
            registry=self.registry,
        )

    def render(self) -> bytes:
        return generate_latest(self.registry)


BREAKER_STATE_VALUE = {"closed": 0, "half_open": 1, "open": 2}
