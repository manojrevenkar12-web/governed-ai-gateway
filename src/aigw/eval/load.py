"""Load, failover and budget tests against a real gateway process.

The gateway runs as its own uvicorn process with PostgreSQL and Redis; providers are mock models
with a fixed latency, so what is measured is the gateway: authentication, policy, data
protection, rate limiting, budget reservation, routing, accounting, metrics and the audit entry.
"""

from __future__ import annotations

import asyncio
import os
import random
import statistics
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import psycopg
import redis
import yaml

ADMIN = "load-admin"


def _policy(path: Path, latency_ms: float, primary_failure: float) -> None:
    cfg = {
        "providers": {
            "primary": {
                "kind": "mock",
                "options": {
                    "latency_ms": latency_ms,
                    "failure_rate": primary_failure,
                    "keep_seen": False,
                    "seed": 1,
                },
            },
            "backup": {"kind": "mock", "options": {"latency_ms": latency_ms, "keep_seen": False}},
        },
        "models": {"model-a": {"providers": ["primary", "backup"], "price_in": 2.0, "price_out": 10.0}},
        "policies": {
            "eng": {
                "models": ["model-a"],
                "max_output_tokens": 256,
                "rate": {"requests_per_minute": 1_000_000, "tokens_per_minute": 1_000_000_000},
            }
        },
    }
    path.write_text(yaml.safe_dump(cfg))


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def gateway(
    database_url: str,
    redis_url: str,
    latency_ms: float = 40.0,
    primary_failure: float = 0.0,
    check_then_spend: bool = False,
):
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS gw CASCADE")
    redis.Redis.from_url(redis_url).flushdb()
    tmp = Path(tempfile.mkdtemp())
    _policy(tmp / "policy.yaml", latency_ms, primary_failure)
    port = _free_port()
    env = {
        **os.environ,
        "AIGW_DATABASE_URL": database_url,
        "AIGW_REDIS_URL": redis_url,
        "AIGW_POLICY_FILE": str(tmp / "policy.yaml"),
        "AIGW_ADMIN_TOKEN": ADMIN,
        "AIGW_BUDGET_CHECK_THEN_SPEND": "1" if check_then_spend else "0",
    }
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "aigw.main:app",
            "--port",
            str(port),
            "--log-level",
            "warning",
            "--no-access-log",
        ],
        env=env,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                if httpx.get(f"{url}/readyz", timeout=1).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            time.sleep(0.1)
        yield url
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def make_key(url: str, budget: float = 1_000_000.0) -> str:
    h = {"X-Admin-Token": ADMIN}
    httpx.post(
        f"{url}/admin/teams",
        headers=h,
        json={"id": "load", "name": "Load", "policy": "eng", "monthly_budget_usd": budget},
    ).raise_for_status()
    r = httpx.post(f"{url}/admin/keys", headers=h, json={"team_id": "load", "label": "load"})
    r.raise_for_status()
    return r.json()["key"]


PROMPTS = [
    "Summarise this ticket: customer cannot log in after changing phone.",
    "Refund DE89 3704 0044 0532 0130 00 for the duplicate charge, contact anna.schmidt@example.de",
    "Write a unit test for a function that rounds interest to two decimals.",
    "Explain this stack trace: NullPointerException at TransferService.java:142",
]


async def _fire(
    url: str, key: str, n: int, concurrency: int, seed: int = 0
) -> list[tuple[int, float, float, str]]:
    rng = random.Random(seed)
    sem = asyncio.Semaphore(concurrency)
    out: list[tuple[int, float, float, str]] = []
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(
        base_url=url,
        timeout=30,
        limits=limits,
        headers={"Authorization": f"Bearer {key}", "X-Use-Case": "load"},
    ) as client:

        async def one(i: int) -> None:
            async with sem:
                started = time.perf_counter()
                r = await client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "model-a",
                        "max_tokens": 64,
                        "messages": [{"role": "user", "content": rng.choice(PROMPTS)}],
                    },
                )
                overhead = float(r.headers.get("x-aigw-overhead-ms", "nan"))
                out.append(
                    (
                        r.status_code,
                        time.perf_counter() - started,
                        overhead,
                        r.headers.get("x-aigw-provider", ""),
                    )
                )

        await asyncio.gather(*[one(i) for i in range(n)])
    return out


def pct(values: list[float], q: float) -> float:
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))] if values else float("nan")


def load(
    database_url: str, redis_url: str, requests: int = 3000, concurrency: int = 32, latency_ms: float = 40.0
) -> dict:
    with gateway(database_url, redis_url, latency_ms) as url:
        key = make_key(url)
        asyncio.run(_fire(url, key, 200, concurrency))  # warm up
        started = time.perf_counter()
        results = asyncio.run(_fire(url, key, requests, concurrency, seed=1))
        elapsed = time.perf_counter() - started
        ok = [r for r in results if r[0] == 200]
        overhead = [r[2] for r in ok]
        total = [r[1] * 1000 for r in ok]
        with psycopg.connect(database_url) as conn:
            audited = conn.execute(
                "SELECT count(*) FROM gw.audit_log WHERE action = 'completion'"
            ).fetchone()[0]
    return {
        "requests": requests,
        "concurrency": concurrency,
        "provider_latency_ms": latency_ms,
        "ok": len(ok),
        "errors": len(results) - len(ok),
        "throughput_rps": len(results) / elapsed,
        "overhead_ms": {
            "p50": pct(overhead, 0.5),
            "p95": pct(overhead, 0.95),
            "p99": pct(overhead, 0.99),
            "mean": statistics.fmean(overhead),
        },
        "end_to_end_ms": {"p50": pct(total, 0.5), "p95": pct(total, 0.95), "p99": pct(total, 0.99)},
        "audit_entries_written": audited - 200,
    }


def failover(database_url: str, redis_url: str, requests: int = 600) -> dict:
    """The primary provider fails on every call. Requests must still succeed, and the breaker must
    stop the gateway from wasting time on the primary."""
    with gateway(database_url, redis_url, latency_ms=20, primary_failure=1.0) as url:
        key = make_key(url)
        results = asyncio.run(_fire(url, key, requests, 16))
        metrics = httpx.get(f"{url}/metrics").text
    served = {p: sum(1 for r in results if r[3] == p) for p in ("primary", "backup")}
    breaker_open = any(
        line.startswith('aigw_breaker_open{provider="primary"} 1') for line in metrics.splitlines()
    )
    return {
        "requests": requests,
        "succeeded": sum(1 for r in results if r[0] == 200),
        "served_by": served,
        "primary_breaker_open": breaker_open,
        "p95_ms": pct([r[1] * 1000 for r in results], 0.95),
    }


def budget_race(database_url: str, redis_url: str, budget_usd: float = 0.05, requests: int = 300) -> dict:
    """300 concurrent requests against a $0.05 budget, with reservations and with check-then-spend."""
    out = {}
    for mode, naive in (("reservations", False), ("check_then_spend", True)):
        with gateway(database_url, redis_url, latency_ms=200, check_then_spend=naive) as url:
            key = make_key(url, budget_usd)
            results = asyncio.run(_fire(url, key, requests, 64))
            with psycopg.connect(database_url) as conn:
                spent = float(
                    conn.execute("SELECT coalesce(sum(spent_usd), 0) FROM gw.budgets").fetchone()[0]
                )
        out[mode] = {
            "admitted": sum(1 for r in results if r[0] == 200),
            "refused_402": sum(1 for r in results if r[0] == 402),
            "spent_usd": round(spent, 6),
            "budget_usd": budget_usd,
            "overspend_pct": round(100 * max(0.0, spent - budget_usd) / budget_usd, 1),
        }
    return out
