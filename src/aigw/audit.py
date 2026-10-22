"""A tamper-evident audit trail.

Each entry's hash covers its content and the previous entry's hash. Changing, inserting or
deleting any past entry breaks the chain from that point on, which `verify` reports. Entries hold
who, what, which model, which data classes and hashes of the prompt, never the prompt itself.
In production the latest hash is also exported periodically to write-once storage (S3 Object Lock),
so the whole table cannot be silently rewritten either.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from psycopg import AsyncConnection, Connection

GENESIS = "0" * 64
LOCK = 717171


def _digest(prev: str, seq: int, at: str, actor: str, action: str, detail: dict) -> str:
    body = json.dumps(
        {"seq": seq, "at": at, "actor": actor, "action": action, "detail": detail},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256((prev + body).encode()).hexdigest()


async def append(conn: AsyncConnection, actor: str, action: str, detail: dict) -> int:
    """Appends one entry. Serialised by an advisory lock so the chain has no forks."""
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK,))
        last = await (
            await conn.execute("SELECT seq, hash FROM gw.audit_log ORDER BY seq DESC LIMIT 1")
        ).fetchone()
        seq, prev = (last["seq"] + 1, last["hash"]) if last else (1, GENESIS)
        at = datetime.now(timezone.utc).isoformat()  # noqa: UP017
        detail = json.loads(json.dumps(detail, default=str))
        h = _digest(prev, seq, at, actor, action, detail)
        await conn.execute(
            "INSERT INTO gw.audit_log (seq, at, actor, action, detail, prev_hash, hash) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (seq, at, actor, action, json.dumps(detail), prev, h),
        )
        return seq


def verify(conn: Connection) -> tuple[bool, int, str]:
    """Walks the chain. Returns (intact, entries checked, first problem)."""
    prev, expected_seq, n = GENESIS, 1, 0
    with conn.transaction(), conn.cursor(name="audit_walk") as cur:
        cur.execute("SELECT seq, at, actor, action, detail, prev_hash, hash FROM gw.audit_log ORDER BY seq")
        for row in cur:
            n += 1
            if row["seq"] != expected_seq:
                return False, n, f"entry {expected_seq} is missing (found {row['seq']})"
            if row["prev_hash"] != prev:
                return False, n, f"entry {row['seq']} does not follow the entry before it"
            at = row["at"].astimezone(timezone.utc).isoformat()  # noqa: UP017
            if _digest(prev, row["seq"], at, row["actor"], row["action"], row["detail"]) != row["hash"]:
                return False, n, f"entry {row['seq']} was modified"
            prev, expected_seq = row["hash"], expected_seq + 1
    return True, n, ""


def prompt_hash(texts: list[str]) -> str:
    h = hashlib.sha256()
    for t in texts:
        h.update(hashlib.sha256(t.encode()).digest())
    return h.hexdigest()


class GroupWriter:
    """Writes audit entries (and the usage rows that go with them) in group commits.

    One writer task drains a queue and appends everything waiting in a single transaction under a
    single lock, so concurrent requests do not queue behind each other's audit lock. Each request
    still waits until its own entry is committed before it answers: nothing is acknowledged that
    is not in the audit trail.
    """

    def __init__(self, pool, max_batch: int = 500, max_wait_s: float = 0.002):
        import asyncio

        self.pool, self.max_batch, self.max_wait_s = pool, max_batch, max_wait_s
        self.queue: asyncio.Queue = asyncio.Queue()
        self.task = None
        self.batches = 0
        self.entries = 0

    def start(self) -> None:
        import asyncio

        self.task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()

    async def write(self, actor: str, action: str, detail: dict, usage: tuple | None = None) -> None:
        import asyncio

        done = asyncio.get_running_loop().create_future()
        await self.queue.put((actor, action, detail, usage, done))
        await done

    async def _run(self) -> None:
        import asyncio

        while True:
            items = [await self.queue.get()]
            deadline = asyncio.get_running_loop().time() + self.max_wait_s
            while len(items) < self.max_batch:
                timeout = deadline - asyncio.get_running_loop().time()
                if timeout <= 0:
                    break
                try:
                    items.append(await asyncio.wait_for(self.queue.get(), timeout))
                except TimeoutError:
                    break
            try:
                await self._commit(items)
                for *_, done in items:
                    if not done.done():
                        done.set_result(None)
            except Exception as exc:  # noqa: BLE001 - every waiting request learns its entry failed
                for *_, done in items:
                    if not done.done():
                        done.set_exception(exc)

    async def _commit(self, items: list) -> None:
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK,))
            last = await (
                await conn.execute("SELECT seq, hash FROM gw.audit_log ORDER BY seq DESC LIMIT 1")
            ).fetchone()
            seq, prev = (last["seq"], last["hash"]) if last else (0, GENESIS)
            rows, usage_rows = [], []
            for actor, action, detail, usage, _ in items:
                seq += 1
                at = datetime.now(timezone.utc).isoformat()  # noqa: UP017
                detail = json.loads(json.dumps(detail, default=str))
                h = _digest(prev, seq, at, actor, action, detail)
                rows.append((seq, at, actor, action, json.dumps(detail), prev, h))
                prev = h
                if usage is not None:
                    usage_rows.append(usage)
            async with conn.cursor() as cur:
                await cur.executemany(
                    "INSERT INTO gw.audit_log (seq, at, actor, action, detail, prev_hash, hash) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    rows,
                )
                if usage_rows:
                    await cur.executemany(
                        """INSERT INTO gw.usage (request_id, team_id, key_id, user_ref, use_case, model, provider, status,
                               reason, input_tokens, output_tokens, cost_usd, latency_ms, overhead_ms, data_classes,
                               redactions, fallback)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                        usage_rows,
                    )
        self.batches += 1
        self.entries += len(items)
