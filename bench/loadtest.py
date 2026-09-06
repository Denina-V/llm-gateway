"""In-process load test.

Drives the real ASGI app through httpx's ASGI transport, so it measures the
gateway's own overhead -- middleware, auth, limiter, cache, accounting -- with
no network and no upstream in the numbers.

Three workloads, because they answer three different questions:

  overhead -- distinct prompts against a 0 ms upstream. Everything measured here
              is the gateway's own cost: middleware, auth, limiter, accounting.
  unique   -- distinct prompts against a simulated `--upstream-ms` provider.
              Nothing is cacheable, so this is the realistic end-to-end path.
  repeat   -- a small prompt set against the same provider, so most requests are
              cache hits. The gap between `unique` and `repeat` is what the
              cache is actually worth.

    python bench/loadtest.py --requests 2000 --concurrency 50 --upstream-ms 25
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx  # noqa: E402

from gateway.config import ApiKey, Settings  # noqa: E402
from gateway.main import create_app  # noqa: E402
from gateway.providers.mock import MockProvider  # noqa: E402

KEY = ApiKey(key_id="bench", secret="sk-bench", budget_usd=None, rps=1e9, burst=1_000_000)
AUTH = {"Authorization": "Bearer sk-bench"}


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round((pct / 100.0) * (len(ordered) - 1))))
    return ordered[idx]


async def run(
    name: str, total: int, concurrency: int, distinct: int, upstream_ms: float
) -> dict:
    app = create_app(
        Settings(provider="mock", api_keys=(KEY,), require_auth=True),
        MockProvider(latency_ms=upstream_ms, seed=99),
    )
    # After create_app, which configures the logger. An access log line per
    # request would dominate a 2000-request run and measure the logger rather
    # than the gateway.
    logging.getLogger("gateway").setLevel(logging.WARNING)
    latencies: list[float] = []
    statuses: dict[int, int] = {}
    cached = 0
    sem = asyncio.Semaphore(concurrency)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://bench") as client:

        async def one(i: int) -> None:
            nonlocal cached
            body = {
                "model": "mock-small",
                "messages": [{"role": "user", "content": f"prompt {i % distinct}"}],
                "max_tokens": 64,
            }
            async with sem:
                started = time.perf_counter()
                r = await client.post("/v1/completions", json=body, headers=AUTH)
                latencies.append((time.perf_counter() - started) * 1000.0)
            statuses[r.status_code] = statuses.get(r.status_code, 0) + 1
            if r.status_code == 200 and r.json().get("cached"):
                cached += 1

        wall_start = time.perf_counter()
        async with app.router.lifespan_context(app):
            await asyncio.gather(*(one(i) for i in range(total)))
        wall = time.perf_counter() - wall_start

    ok = statuses.get(200, 0)
    return {
        "workload": name,
        "requests": total,
        "concurrency": concurrency,
        "wall_s": round(wall, 3),
        "rps": round(total / wall, 1),
        "ok": ok,
        "statuses": statuses,
        "cache_hit_pct": round(100.0 * cached / ok, 1) if ok else 0.0,
        "p50_ms": round(statistics.median(latencies), 3),
        "p95_ms": round(percentile(latencies, 95), 3),
        "p99_ms": round(percentile(latencies, 99), 3),
        "max_ms": round(max(latencies), 3),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--requests", type=int, default=2000)
    ap.add_argument("--concurrency", type=int, default=50)
    ap.add_argument("--upstream-ms", type=float, default=25.0,
                    help="simulated provider latency for the unique/repeat workloads")
    args = ap.parse_args()

    n, c, up = args.requests, args.concurrency, args.upstream_ms
    results = [
        asyncio.run(run("overhead (0ms upstream)", n, c, n, 0.0)),
        asyncio.run(run(f"unique ({up:.0f}ms upstream)", n, c, n, up)),
        asyncio.run(run(f"repeat ({up:.0f}ms upstream)", n, c, 20, up)),
    ]

    header = f"{'workload':<26}{'rps':>10}{'p50 ms':>10}{'p95 ms':>10}{'p99 ms':>10}{'cache':>8}"
    print(header)
    print("-" * len(header))
    for r in results:
        print(
            f"{r['workload']:<26}{r['rps']:>10}{r['p50_ms']:>10}"
            f"{r['p95_ms']:>10}{r['p99_ms']:>10}{str(r['cache_hit_pct']) + '%':>8}"
        )
    print()
    for r in results:
        print(f"{r['workload']}: {r['ok']}/{r['requests']} ok, {r['wall_s']}s, statuses={r['statuses']}")


if __name__ == "__main__":
    main()
