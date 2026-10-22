"""The gateway: one governed way for every team to use AI models.

For each request: authenticate the team's key -> apply the team's policy (model, tools, data) ->
redact or block regulated data -> rate-limit -> reserve budget -> route to a provider with
fallback -> restore redacted values in the answer -> settle the cost -> write usage, metrics and
a hash-chained audit entry. Clients use the OpenAI-compatible chat completions API, so AI coding
assistants and SDKs work by changing their base URL and key.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import secrets
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal
from importlib.resources import files
from typing import Any, Literal

import redis.asyncio as aioredis
from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from prometheus_client import generate_latest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, ConfigDict

from aigw import audit, dlp, metrics, policy
from aigw.limits import Budgets, RateLimiter
from aigw.providers import (
    ClientError,
    Completion,
    Message,
    NoProvider,
    Provider,
    Router,
    build,
    estimate_tokens,
)

# ---- settings and state ---------------------------------------------------------------------------------------


@dataclass
class Settings:
    database_url: str
    redis_url: str
    policy_file: str
    admin_token: str
    replicas: int = 1
    key_cache_s: float = 10.0
    budget_check_then_spend: bool = False  # only for the evaluation that shows why reservations are needed

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            database_url=os.environ.get("AIGW_DATABASE_URL", "postgresql://postgres@127.0.0.1:5432/aigw"),
            redis_url=os.environ.get("AIGW_REDIS_URL", "redis://127.0.0.1:6379/0"),
            policy_file=os.environ.get("AIGW_POLICY_FILE", "policies/fintech-example.yaml"),
            admin_token=os.environ.get("AIGW_ADMIN_TOKEN", ""),
            replicas=int(os.environ.get("AIGW_REPLICAS", "1")),
        )


@dataclass
class KeyInfo:
    key_id: str
    team_id: str
    policy: str
    budget: Decimal
    expires_at: float | None


class GatewayError(Exception):
    def __init__(self, status: int, code: str, message: str, headers: dict | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.headers = status, code, message, headers or {}


def error(status: int, code: str, message: str, headers: dict | None = None) -> JSONResponse:
    kind = {
        401: "authentication_error",
        402: "insufficient_quota",
        403: "permission_error",
        429: "rate_limit_error",
    }.get(status, "invalid_request_error" if status < 500 else "api_error")
    return JSONResponse(
        {"error": {"message": message, "type": kind, "code": code}}, status_code=status, headers=headers
    )


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def new_key() -> tuple[str, str, str]:
    raw = "aigw_" + secrets.token_urlsafe(32)
    h = hash_key(raw)
    return raw, f"key_{h[:10]}", h


# ---- request model --------------------------------------------------------------------------------------------


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")
    role: Literal["system", "developer", "user", "assistant", "tool"]
    content: str | list[dict] | None = None


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str
    messages: list[ChatMessage]
    max_tokens: int | None = None
    max_completion_tokens: int | None = None
    stream: bool = False
    tools: list[dict] | None = None


class ToolCheck(BaseModel):
    tool: str
    arguments: dict[str, Any] = {}


class TeamIn(BaseModel):
    id: str
    name: str
    policy: str
    monthly_budget_usd: float


class KeyIn(BaseModel):
    team_id: str
    label: str
    days_valid: int | None = 90


def _text(m: ChatMessage) -> str:
    if m.content is None:
        return ""
    if isinstance(m.content, str):
        return m.content
    parts = []
    for p in m.content:
        if p.get("type") != "text":
            raise GatewayError(400, "unsupported_content", "only text content is supported")
        parts.append(p.get("text", ""))
    return "".join(parts)


# ---- the application --------------------------------------------------------------------------------------------


class Gateway:
    def __init__(self, settings: Settings, providers: dict[str, Provider] | None = None):
        self.settings = settings
        self.loaded = policy.load(settings.policy_file)
        self.providers = providers or {n: build(n, s) for n, s in self.loaded.config.providers.items()}
        self.router = Router(self.providers)
        self.key_cache: dict[str, tuple[float, KeyInfo | None]] = {}

    async def start(self) -> None:
        self.pool = AsyncConnectionPool(
            self.settings.database_url,
            min_size=2,
            max_size=20,
            open=False,
            kwargs={"row_factory": dict_row, "autocommit": True},
        )
        await self.pool.open()
        async with self.pool.connection() as conn:
            await conn.execute("SELECT pg_advisory_lock(919191)")
            try:
                await conn.execute(files("aigw").joinpath("sql/schema.sql").read_text())
            finally:
                await conn.execute("SELECT pg_advisory_unlock(919191)")
        self.redis = aioredis.from_url(
            self.settings.redis_url, socket_timeout=0.25, socket_connect_timeout=0.25
        )
        self.limiter = RateLimiter(self.redis, self.settings.replicas)
        self.budgets = Budgets(self.pool)
        self.writer = audit.GroupWriter(self.pool)
        self.writer.start()
        async with self.pool.connection() as conn:
            await audit.append(
                conn, "system", "policy.load", {"file": self.loaded.path, "sha256": self.loaded.sha256}
            )

    async def stop(self) -> None:
        await self.writer.stop()
        await self.pool.close()
        await self.redis.aclose()

    # -- identity --
    async def authenticate(self, authorization: str | None) -> KeyInfo:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise GatewayError(401, "missing_key", "send your team's key as 'Authorization: Bearer <key>'")
        h = hash_key(authorization[7:].strip())
        now = time.time()
        cached = self.key_cache.get(h)
        if cached and cached[0] > now:
            info = cached[1]
        else:
            async with self.pool.connection() as conn:
                row = await (
                    await conn.execute(
                        """SELECT k.id, k.team_id, t.policy, t.monthly_budget_usd,
                              extract(epoch FROM k.expires_at) AS exp
                         FROM gw.api_keys k JOIN gw.teams t ON t.id = k.team_id
                        WHERE k.key_hash = %s AND k.revoked_at IS NULL""",
                        (h,),
                    )
                ).fetchone()
            info = (
                KeyInfo(
                    row["id"],
                    row["team_id"],
                    row["policy"],
                    row["monthly_budget_usd"],
                    float(row["exp"]) if row["exp"] else None,
                )
                if row
                else None
            )
            self.key_cache[h] = (now + self.settings.key_cache_s, info)
        if info is None or (info.expires_at and info.expires_at < now):
            raise GatewayError(401, "invalid_key", "this key is unknown, expired or revoked")
        return info

    def policy_for(self, info: KeyInfo) -> policy.Policy:
        p = self.loaded.config.policies.get(info.policy)
        if p is None:
            raise GatewayError(403, "no_policy", f"team {info.team_id} has no policy {info.policy}")
        return p

    # -- the request path --
    async def prepare(self, req: ChatRequest, info: KeyInfo, approval: str | None):
        if any(m.role == "tool" for m in req.messages):
            raise GatewayError(
                400, "unsupported_role", "tool results are not supported by this gateway version"
            )
        pol = self.policy_for(info)
        texts = [_text(m) for m in req.messages]
        tool_names = [t.get("function", {}).get("name", t.get("name", "?")) for t in req.tools or []]
        decision = policy.decide(
            pol, req.model, texts, tool_names, req.max_completion_tokens or req.max_tokens, approval
        )
        if not decision.allowed:
            raise GatewayError(decision.status, decision.code, decision.reason)
        spec = self.loaded.config.models[req.model]
        if req.tools and any(self.loaded.config.providers[p].kind == "anthropic" for p in spec.providers):
            raise GatewayError(
                400,
                "tools_unsupported",
                "tool definitions are only forwarded to OpenAI-compatible providers; "
                "agent runtimes authorise tool calls with POST /v1/tools/authorize",
            )
        pseudo = dlp.Pseudonymiser()
        messages = []
        for i, (m, text) in enumerate(zip(req.messages, texts, strict=True)):
            role = "system" if m.role == "developer" else m.role
            messages.append(Message(role, pseudo.redact(text, decision.findings.get(i, []))))
        est_in = sum(estimate_tokens(m.content) for m in messages)
        worst = Decimal(str((est_in * spec.price_in + decision.max_tokens * spec.price_out) / 1e6))
        return pol, decision, spec, pseudo, messages, est_in, worst

    async def admit(self, info: KeyInfo, pol: policy.Policy, est_tokens: int, worst: Decimal):
        rl = await self.limiter.take(
            info.key_id, pol.rate.requests_per_minute, pol.rate.tokens_per_minute, est_tokens
        )
        if rl.degraded:
            metrics.RATE_DEGRADED.inc()
        if not rl.allowed:
            wait = max(1, int(rl.retry_after_s + 0.999))
            raise GatewayError(
                429, "rate_limited", f"rate limit for this key; retry in {wait} s", {"Retry-After": str(wait)}
            )
        reservation = await self.budgets.reserve(
            info.team_id, info.budget, worst, naive=self.settings.budget_check_then_spend
        )
        if not reservation.ok:
            raise GatewayError(
                402,
                "budget_exhausted",
                f"team {info.team_id} has used ${reservation.spent:.2f} of its "
                f"${reservation.limit:.2f} monthly budget",
            )
        return reservation

    async def record(
        self,
        rid: str,
        info: KeyInfo,
        req: ChatRequest,
        status: str,
        reason: str,
        user: str | None,
        use_case: str | None,
        c: Completion | None,
        cost: Decimal,
        latency: float,
        overhead: float,
        classes: set[str],
        redactions: int,
        fallback: bool,
        texts: list[str],
    ) -> None:
        provider = c.provider if c else None
        usage = (
            rid,
            info.team_id,
            info.key_id,
            user,
            use_case,
            req.model,
            provider,
            status,
            reason or None,
            c.input_tokens if c else 0,
            c.output_tokens if c else 0,
            cost,
            int(latency * 1000),
            int(overhead * 1000),
            sorted(classes),
            redactions,
            fallback,
        )
        await self.writer.write(
            f"{info.team_id}/{info.key_id}",
            "completion",
            {
                "request_id": rid,
                "status": status,
                "reason": reason,
                "model": req.model,
                "provider": provider,
                "user": user,
                "use_case": use_case,
                "data_classes": sorted(classes),
                "redactions": redactions,
                "prompt_sha256": audit.prompt_hash(texts),
                "cost_usd": str(cost),
            },
            usage,
        )
        metrics.REQUESTS.labels(info.team_id, req.model, status).inc()
        if c:
            metrics.TOKENS.labels(info.team_id, req.model, "in").inc(c.input_tokens)
            metrics.TOKENS.labels(info.team_id, req.model, "out").inc(c.output_tokens)
            metrics.COST.labels(info.team_id, req.model).inc(float(cost))
            metrics.LATENCY.labels(req.model).observe(latency)
            metrics.OVERHEAD.observe(overhead)
        for name, b in self.router.breakers.items():
            metrics.BREAKER.labels(name).set(1 if b.state == "open" else 0)

    def cost(self, spec: policy.ModelSpec, c: Completion) -> Decimal:
        return Decimal(
            str((c.input_tokens * spec.price_in + c.output_tokens * spec.price_out) / 1e6)
        ).quantize(Decimal("0.000001"))


def create_app(settings: Settings | None = None, providers: dict[str, Provider] | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    gw = Gateway(settings, providers)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await gw.start()
        reaper = asyncio.create_task(_reap(gw))
        yield
        reaper.cancel()
        await gw.stop()

    app = FastAPI(title="governed-ai-gateway", version="1.0.0", lifespan=lifespan)
    app.state.gw = gw

    @app.exception_handler(GatewayError)
    async def _gateway_error(_: Request, exc: GatewayError) -> JSONResponse:
        return error(exc.status, exc.code, exc.message, exc.headers)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        checks = {}
        try:
            async with gw.pool.connection() as conn:
                await conn.execute("SELECT 1")
            checks["postgres"] = "ok"
        except Exception as exc:  # noqa: BLE001
            checks["postgres"] = f"down: {type(exc).__name__}"
        try:
            await gw.redis.ping()
            checks["redis"] = "ok"
        except Exception as exc:  # noqa: BLE001
            checks["redis"] = f"degraded: {type(exc).__name__}"  # still serving, with local limits
        ok = checks["postgres"] == "ok"
        return JSONResponse(
            {"ready": ok, "checks": checks, "policy_sha256": gw.loaded.sha256}, status_code=200 if ok else 503
        )

    @app.get("/metrics")
    async def prom() -> Response:
        return Response(generate_latest(metrics.REGISTRY), media_type="text/plain; version=0.0.4")

    @app.get("/v1/models")
    async def models(authorization: str | None = Header(None)) -> dict:
        info = await gw.authenticate(authorization)
        allowed = gw.policy_for(info).models
        return {"object": "list", "data": [{"id": m, "object": "model", "owned_by": "aigw"} for m in allowed]}

    @app.post("/v1/chat/completions")
    async def chat(
        req: ChatRequest,
        authorization: str | None = Header(None),
        x_user: str | None = Header(None),
        x_use_case: str | None = Header(None),
        x_approval: str | None = Header(None),
    ):
        started = time.perf_counter()
        rid = "chatcmpl-" + uuid.uuid4().hex[:24]
        info = await gw.authenticate(authorization)
        texts = [_text(m) for m in req.messages]
        try:
            pol, decision, spec, pseudo, messages, est_in, worst = await gw.prepare(req, info, x_approval)
            reservation = await gw.admit(info, pol, est_in + decision.max_tokens, worst)
        except GatewayError as exc:
            status = {429: "rate_limited", 402: "over_budget"}.get(exc.status, "blocked")
            if status == "blocked":
                metrics.BLOCKED.labels(info.team_id, exc.code).inc()
            classes = (
                {f.data_class for t in texts for f in dlp.scan(t)} if exc.code == "data_blocked" else set()
            )
            await gw.record(
                rid,
                info,
                req,
                status,
                exc.code,
                x_user,
                x_use_case,
                None,
                Decimal(0),
                time.perf_counter() - started,
                time.perf_counter() - started,
                classes,
                0,
                False,
                texts,
            )
            raise
        redactions = sum(len(v) for v in decision.findings.values())
        for fs in decision.findings.values():
            for f in fs:
                metrics.REDACTIONS.labels(f.kind).inc()
        headers = {"x-aigw-request-id": rid, "x-aigw-redactions": str(redactions)}

        if req.stream:
            return StreamingResponse(
                _stream(
                    gw,
                    req,
                    info,
                    spec,
                    pseudo,
                    messages,
                    decision,
                    reservation,
                    rid,
                    started,
                    x_user,
                    x_use_case,
                    texts,
                    redactions,
                ),
                media_type="text/event-stream",
                headers=headers,
            )

        provider_started = time.perf_counter()
        try:
            c, fallback = await gw.router.complete(
                spec.providers, spec.upstream_model, req.model, messages, decision.max_tokens
            )
        except (NoProvider, ClientError) as exc:
            await gw.budgets.settle(reservation, Decimal(0))
            code = "upstream_unavailable" if isinstance(exc, NoProvider) else "upstream_rejected"
            await gw.record(
                rid,
                info,
                req,
                "upstream_error",
                code,
                x_user,
                x_use_case,
                None,
                Decimal(0),
                time.perf_counter() - started,
                0,
                decision.data_classes,
                redactions,
                False,
                texts,
            )
            status = 503 if isinstance(exc, NoProvider) else exc.status
            return error(status, code, f"the model provider could not answer: {exc}")
        provider_time = time.perf_counter() - provider_started
        if fallback:
            metrics.FALLBACKS.labels(req.model, c.provider).inc()
        cost = gw.cost(spec, c)
        spent, limit, crossed = await gw.budgets.settle(reservation, cost)
        metrics.BUDGET_USED.labels(info.team_id).set(float(spent / limit) if limit else 0)
        if crossed:
            async with gw.pool.connection() as conn:
                await audit.append(
                    conn,
                    "system",
                    "budget.alert_80",
                    {"team": info.team_id, "spent_usd": str(spent), "limit_usd": str(limit)},
                )
        text = pseudo.restore(c.text)
        latency = time.perf_counter() - started
        await gw.record(
            rid,
            info,
            req,
            "ok",
            "",
            x_user,
            x_use_case,
            c,
            cost,
            latency,
            latency - provider_time,
            decision.data_classes,
            redactions,
            fallback,
            texts,
        )
        headers["x-aigw-cost-usd"] = str(cost)
        headers["x-aigw-overhead-ms"] = f"{(time.perf_counter() - started - provider_time) * 1000:.2f}"
        headers["x-aigw-provider"] = c.provider
        return JSONResponse(
            {
                "id": rid,
                "object": "chat.completion",
                "created": int(time.time()),
                "model": req.model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": text},
                        "finish_reason": c.finish_reason,
                    }
                ],
                "usage": {
                    "prompt_tokens": c.input_tokens,
                    "completion_tokens": c.output_tokens,
                    "total_tokens": c.input_tokens + c.output_tokens,
                },
            },
            headers=headers,
        )

    @app.post("/v1/tools/authorize")
    async def authorize_tool(
        body: ToolCheck,
        authorization: str | None = Header(None),
        x_approval: str | None = Header(None),
        x_user: str | None = Header(None),
    ) -> dict:
        """For agent runtimes and MCP gateways: may this team's agent call this tool now?"""
        info = await gw.authenticate(authorization)
        pol = gw.policy_for(info)
        d = policy.decide(pol, pol.models[0], [json.dumps(body.arguments)], [body.tool], None, x_approval)
        async with gw.pool.connection() as conn:
            await audit.append(
                conn,
                f"{info.team_id}/{info.key_id}",
                "tool.authorize",
                {
                    "tool": body.tool,
                    "allowed": d.allowed,
                    "code": d.code,
                    "user": x_user,
                    "approval": x_approval,
                },
            )
        if not d.allowed:
            raise GatewayError(d.status, d.code, d.reason)
        return {"allowed": True, "tool": body.tool}

    # ---- administration (separate token; every action audited) ----

    def admin(token: str | None) -> None:
        if not settings.admin_token or not token or not secrets.compare_digest(token, settings.admin_token):
            raise GatewayError(401, "admin_only", "admin token required")

    @app.post("/admin/teams")
    async def create_team(body: TeamIn, x_admin_token: str | None = Header(None)) -> dict:
        admin(x_admin_token)
        if body.policy not in gw.loaded.config.policies:
            raise GatewayError(400, "unknown_policy", f"no policy {body.policy}")
        async with gw.pool.connection() as conn:
            await conn.execute(
                """INSERT INTO gw.teams (id, name, policy, monthly_budget_usd) VALUES (%s, %s, %s, %s)
                   ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, policy = EXCLUDED.policy,
                       monthly_budget_usd = EXCLUDED.monthly_budget_usd""",
                (body.id, body.name, body.policy, body.monthly_budget_usd),
            )
            await conn.execute(
                "UPDATE gw.budgets SET limit_usd = %s WHERE team_id = %s AND month >= date_trunc('month', now())",
                (body.monthly_budget_usd, body.id),
            )
            await audit.append(conn, "admin", "team.upsert", body.model_dump())
        gw.key_cache.clear()
        return body.model_dump()

    @app.post("/admin/keys")
    async def create_key(body: KeyIn, x_admin_token: str | None = Header(None)) -> dict:
        admin(x_admin_token)
        raw, key_id, h = new_key()
        async with gw.pool.connection() as conn:
            await conn.execute(
                """INSERT INTO gw.api_keys (id, team_id, key_hash, label, expires_at)
                   VALUES (%s, %s, %s, %s, CASE WHEN %s::int IS NULL THEN NULL ELSE now() + make_interval(days => %s::int) END)""",
                (key_id, body.team_id, h, body.label, body.days_valid, body.days_valid),
            )
            await audit.append(
                conn, "admin", "key.create", {"key_id": key_id, "team": body.team_id, "label": body.label}
            )
        return {"key_id": key_id, "key": raw, "note": "shown once; only its hash is stored"}

    @app.post("/admin/keys/{key_id}/revoke")
    async def revoke_key(key_id: str, x_admin_token: str | None = Header(None)) -> dict:
        admin(x_admin_token)
        async with gw.pool.connection() as conn:
            cur = await conn.execute(
                "UPDATE gw.api_keys SET revoked_at = now() WHERE id = %s AND revoked_at IS NULL", (key_id,)
            )
            await audit.append(conn, "admin", "key.revoke", {"key_id": key_id})
        gw.key_cache.clear()
        return {"revoked": cur.rowcount == 1}

    @app.post("/admin/policies/reload")
    async def reload(x_admin_token: str | None = Header(None)) -> dict:
        admin(x_admin_token)
        try:
            new = policy.load(settings.policy_file)
        except Exception as exc:  # noqa: BLE001 - an invalid file never replaces the running policy
            raise GatewayError(400, "invalid_policy", f"policy not reloaded: {exc}") from exc
        gw.loaded = new
        async with gw.pool.connection() as conn:
            await audit.append(conn, "admin", "policy.reload", {"file": new.path, "sha256": new.sha256})
        return {"sha256": new.sha256, "policies": list(new.config.policies)}

    @app.get("/admin/usage")
    async def usage(month: str | None = None, x_admin_token: str | None = Header(None)) -> dict:
        admin(x_admin_token)
        async with gw.pool.connection() as conn:
            rows = await (
                await conn.execute(
                    """SELECT team_id, count(*) AS requests, count(*) FILTER (WHERE status = 'ok') AS ok,
                          sum(cost_usd)::float AS cost_usd, count(DISTINCT user_ref) AS users
                     FROM gw.usage WHERE at >= coalesce(%s::date, date_trunc('month', now()))
                    GROUP BY team_id ORDER BY cost_usd DESC NULLS LAST""",
                    (month,),
                )
            ).fetchall()
        return {"teams": rows}

    return app


async def _stream(
    gw: Gateway,
    req: ChatRequest,
    info: KeyInfo,
    spec: policy.ModelSpec,
    pseudo: dlp.Pseudonymiser,
    messages: list[Message],
    decision: policy.Decision,
    reservation,
    rid: str,
    started: float,
    user: str | None,
    use_case: str | None,
    texts: list[str],
    redactions: int,
) -> AsyncIterator[bytes]:
    restorer = dlp.StreamRestorer(pseudo)
    final: Completion | None = None
    streamed = []
    status, reason = "ok", ""

    def chunk(delta: dict, finish: str | None = None, usage: dict | None = None) -> bytes:
        body = {
            "id": rid,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": req.model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        if usage:
            body["usage"] = usage
        return f"data: {json.dumps(body)}\n\n".encode()

    provider_started = time.perf_counter()
    try:
        yield chunk({"role": "assistant"})
        async for part in gw.router.stream(
            spec.providers, spec.upstream_model, req.model, messages, decision.max_tokens
        ):
            if isinstance(part, Completion):
                final = part
                continue
            out = restorer.feed(part)
            streamed.append(part)
            if out:
                yield chunk({"content": out})
        tail = restorer.flush()
        if tail:
            yield chunk({"content": tail})
        usage = (
            {
                "prompt_tokens": final.input_tokens,
                "completion_tokens": final.output_tokens,
                "total_tokens": final.input_tokens + final.output_tokens,
            }
            if final
            else None
        )
        yield chunk({}, final.finish_reason if final else "stop", usage)
        yield b"data: [DONE]\n\n"
    except (NoProvider, ClientError) as exc:
        status, reason = "upstream_error", type(exc).__name__
        yield f"data: {json.dumps({'error': {'message': str(exc), 'code': 'upstream_unavailable'}})}\n\n".encode()
    finally:
        if final is None and streamed:  # the client went away mid-answer: bill what was produced
            final = Completion(
                "",
                sum(estimate_tokens(m.content) for m in messages),
                estimate_tokens("".join(streamed)),
                "stop",
                "",
            )
        cost = gw.cost(spec, final) if final else Decimal(0)
        with contextlib.suppress(Exception):
            await gw.budgets.settle(reservation, cost)
            latency = time.perf_counter() - started
            await gw.record(
                rid,
                info,
                req,
                status,
                reason,
                user,
                use_case,
                final,
                cost,
                latency,
                latency - (time.perf_counter() - provider_started),
                decision.data_classes,
                redactions,
                False,
                texts,
            )


async def _reap(gw: Gateway) -> None:
    while True:
        await asyncio.sleep(60)
        with contextlib.suppress(Exception):
            await gw.budgets.reap()
