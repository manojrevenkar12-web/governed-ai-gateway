"""Prometheus metrics: what the platform team, and leadership dashboards, are built on."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

REGISTRY = CollectorRegistry()

REQUESTS = Counter(
    "aigw_requests_total",
    "Requests by team, model and outcome",
    ["team", "model", "status"],
    registry=REGISTRY,
)
LATENCY = Histogram(
    "aigw_request_seconds",
    "End-to-end latency",
    ["model"],
    registry=REGISTRY,
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60),
)
OVERHEAD = Histogram(
    "aigw_overhead_seconds",
    "Time spent in the gateway itself (excludes the provider)",
    registry=REGISTRY,
    buckets=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25),
)
TOKENS = Counter(
    "aigw_tokens_total",
    "Tokens by team, model and direction",
    ["team", "model", "direction"],
    registry=REGISTRY,
)
COST = Counter("aigw_cost_usd_total", "Spend in USD by team and model", ["team", "model"], registry=REGISTRY)
REDACTIONS = Counter(
    "aigw_redactions_total", "Values redacted before leaving the company", ["kind"], registry=REGISTRY
)
BLOCKED = Counter("aigw_blocked_total", "Requests refused by policy", ["team", "code"], registry=REGISTRY)
FALLBACKS = Counter(
    "aigw_fallbacks_total", "Requests served by a fallback provider", ["model", "provider"], registry=REGISTRY
)
BREAKER = Gauge(
    "aigw_breaker_open", "1 while a provider's circuit breaker is open", ["provider"], registry=REGISTRY
)
RATE_DEGRADED = Counter(
    "aigw_ratelimit_degraded_total", "Rate-limit checks done locally because Redis failed", registry=REGISTRY
)
BUDGET_USED = Gauge(
    "aigw_budget_used_ratio", "Share of the monthly budget spent", ["team"], registry=REGISTRY
)
