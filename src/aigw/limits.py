"""Rate limits (Redis) and budgets (PostgreSQL).

**Rate limits** are token buckets per API key for requests and for tokens per minute, checked and
taken together in one Lua script, so concurrent gateway replicas never over-admit. If Redis is
unreachable the gateway does not stop serving: it falls back to a per-replica bucket at the same
rate divided by the replica count, counts the event, and alerts (see docs/team/OPERATIONS.md).

**Budgets** are monthly per team. Each request first reserves its worst-case cost (estimated input
tokens plus the full output allowance) in one conditional UPDATE, and settles the actual cost
afterwards. Reservations, not check-then-spend, are what stop a burst of concurrent requests from
running a team over its budget; a reaper returns reservations a crashed replica never settled.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import redis.asyncio as aioredis
from psycopg_pool import AsyncConnectionPool

TOKEN_BUCKET = """
local now = tonumber(ARGV[1])
local allowed = 1
local wait = 0
local state = {}
for i = 1, 2 do
  local key = KEYS[i]
  local capacity = tonumber(ARGV[2 * i])
  local cost = tonumber(ARGV[2 * i + 1])
  local rate = capacity / 60.0
  local b = redis.call('HMGET', key, 'tokens', 'ts')
  local tokens = tonumber(b[1]) or capacity
  local ts = tonumber(b[2]) or now
  tokens = math.min(capacity, tokens + (now - ts) * rate)
  state[i] = tokens
  if tokens < cost then
    allowed = 0
    wait = math.max(wait, (cost - tokens) / rate)
  end
end
if allowed == 1 then
  for i = 1, 2 do
    local cost = tonumber(ARGV[2 * i + 1])
    redis.call('HSET', KEYS[i], 'tokens', state[i] - cost, 'ts', now)
    redis.call('EXPIRE', KEYS[i], 120)
  end
end
return {allowed, tostring(wait)}
"""


@dataclass
class RateResult:
    allowed: bool
    retry_after_s: float
    degraded: bool = False


class LocalBucket:
    def __init__(self) -> None:
        self.state: dict[str, tuple[float, float]] = {}

    def take(self, key: str, capacity: float, cost: float, now: float) -> tuple[bool, float]:
        tokens, ts = self.state.get(key, (capacity, now))
        rate = capacity / 60.0
        tokens = min(capacity, tokens + (now - ts) * rate)
        if tokens < cost:
            return False, (cost - tokens) / rate
        self.state[key] = (tokens - cost, now)
        return True, 0.0


class RateLimiter:
    def __init__(self, redis: aioredis.Redis, replicas: int = 1):
        self.redis, self.replicas = redis, max(1, replicas)
        self.script = redis.register_script(TOKEN_BUCKET)
        self.local = LocalBucket()
        self.degraded_events = 0

    async def take(self, key_id: str, rpm: int, tpm: int, tokens: int) -> RateResult:
        now = time.time()
        try:
            allowed, wait = await self.script(
                keys=[f"rl:{key_id}:req", f"rl:{key_id}:tok"], args=[now, rpm, 1, tpm, min(tokens, tpm)]
            )
            return RateResult(bool(int(allowed)), float(wait))
        except (aioredis.ConnectionError, aioredis.TimeoutError, OSError):
            self.degraded_events += 1
            ok1, w1 = self.local.take(f"{key_id}:req", rpm / self.replicas, 1, now)
            ok2, w2 = (
                self.local.take(f"{key_id}:tok", tpm / self.replicas, min(tokens, tpm / self.replicas), now)
                if ok1
                else (False, 0.0)
            )
            return RateResult(ok1 and ok2, max(w1, w2), degraded=True)


# ---- budgets --------------------------------------------------------------------------------------------------


def month_of(d: date | None = None) -> date:
    d = d or date.today()
    return d.replace(day=1)


@dataclass
class Reservation:
    id: str | None
    team_id: str
    month: date
    amount: Decimal
    ok: bool
    spent: Decimal = Decimal(0)
    limit: Decimal = Decimal(0)


class Budgets:
    def __init__(self, pool: AsyncConnectionPool):
        self.pool = pool

    async def reserve(
        self,
        team_id: str,
        limit_usd: Decimal,
        amount: Decimal,
        month: date | None = None,
        naive: bool = False,
    ) -> Reservation:
        m = month_of(month)
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute(
                "INSERT INTO gw.budgets (team_id, month, limit_usd) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (team_id, m, limit_usd),
            )
            if naive:  # check-then-spend, kept only to show in the evaluation why it overspends
                row = await (
                    await conn.execute(
                        "SELECT spent_usd, limit_usd FROM gw.budgets WHERE team_id = %s AND month = %s",
                        (team_id, m),
                    )
                ).fetchone()
                ok = row["spent_usd"] + amount <= row["limit_usd"]
                return Reservation(None, team_id, m, Decimal(0), ok, row["spent_usd"], row["limit_usd"])
            row = await (
                await conn.execute(
                    """UPDATE gw.budgets SET reserved_usd = reserved_usd + %(a)s
                    WHERE team_id = %(t)s AND month = %(m)s AND spent_usd + reserved_usd + %(a)s <= limit_usd
                RETURNING spent_usd, limit_usd""",
                    {"a": amount, "t": team_id, "m": m},
                )
            ).fetchone()
            if row is None:
                cur = await (
                    await conn.execute(
                        "SELECT spent_usd, limit_usd FROM gw.budgets WHERE team_id = %s AND month = %s",
                        (team_id, m),
                    )
                ).fetchone()
                return Reservation(None, team_id, m, amount, False, cur["spent_usd"], cur["limit_usd"])
            rid = str(uuid.uuid4())
            await conn.execute(
                "INSERT INTO gw.reservations (id, team_id, month, amount_usd) VALUES (%s, %s, %s, %s)",
                (rid, team_id, m, amount),
            )
            return Reservation(rid, team_id, m, amount, True, row["spent_usd"], row["limit_usd"])

    async def settle(self, r: Reservation, actual: Decimal) -> tuple[Decimal, Decimal, bool]:
        """Books the actual cost and releases the hold. Returns spent, limit and whether this request
        crossed 80% of the budget (for the alert)."""
        async with self.pool.connection() as conn, conn.transaction():
            if r.id is not None:
                deleted = await conn.execute("DELETE FROM gw.reservations WHERE id = %s", (r.id,))
                held = r.amount if deleted.rowcount == 1 else Decimal(0)  # the reaper may have released it
            else:
                held = Decimal(0)
            row = await (
                await conn.execute(
                    """UPDATE gw.budgets SET reserved_usd = GREATEST(0, reserved_usd - %(h)s), spent_usd = spent_usd + %(a)s
                    WHERE team_id = %(t)s AND month = %(m)s
                RETURNING spent_usd, limit_usd, alerted_at_80""",
                    {"h": held, "a": actual, "t": r.team_id, "m": r.month},
                )
            ).fetchone()
            crossed = row["alerted_at_80"] is None and row["spent_usd"] >= row["limit_usd"] * Decimal("0.8")
            if crossed:
                await conn.execute(
                    "UPDATE gw.budgets SET alerted_at_80 = now() WHERE team_id = %s AND month = %s",
                    (r.team_id, r.month),
                )
            return row["spent_usd"], row["limit_usd"], crossed

    async def reap(self, older_than_s: int = 600) -> int:
        """Returns holds that a crashed replica never settled."""
        async with self.pool.connection() as conn, conn.transaction():
            rows = await (
                await conn.execute(
                    """DELETE FROM gw.reservations WHERE created_at < now() - make_interval(secs => %s)
                RETURNING team_id, month, amount_usd""",
                    (older_than_s,),
                )
            ).fetchall()
            for r in rows:
                await conn.execute(
                    "UPDATE gw.budgets SET reserved_usd = GREATEST(0, reserved_usd - %s) WHERE team_id = %s AND month = %s",
                    (r["amount_usd"], r["team_id"], r["month"]),
                )
            return len(rows)
