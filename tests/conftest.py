from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import httpx
import psycopg
import pytest
import redis

from aigw.app import Settings, create_app
from aigw.providers import MockProvider

DB = os.environ.get("AIGW_TEST_DATABASE_URL")
REDIS = os.environ.get("AIGW_TEST_REDIS_URL", "redis://127.0.0.1:6379/15")
POLICY = str(Path(__file__).parent / "policy.yaml")
ADMIN = "test-admin-token"


@dataclass
class Env:
    client: httpx.AsyncClient
    app: object
    keys: dict[str, str]
    providers: dict[str, MockProvider]

    def auth(self, team: str) -> dict:
        return {"Authorization": f"Bearer {self.keys[team]}"}


def reset(db: str, redis_url: str) -> None:
    with psycopg.connect(db, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS gw CASCADE")
    redis.Redis.from_url(redis_url).flushdb()


@pytest.fixture
async def env():
    if not DB:
        pytest.skip("AIGW_TEST_DATABASE_URL not set")
    reset(DB, REDIS)
    providers = {
        "primary": MockProvider("primary"),
        "backup": MockProvider("backup"),
        "openai-like": MockProvider("openai-like"),
    }
    app = create_app(Settings(DB, REDIS, POLICY, ADMIN), providers)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://gw") as client:
            keys = {}
            for team, pol, budget in (("payments", "eng", 50.0), ("support", "strict", 5.0)):
                r = await client.post(
                    "/admin/teams",
                    headers={"X-Admin-Token": ADMIN},
                    json={"id": team, "name": team.title(), "policy": pol, "monthly_budget_usd": budget},
                )
                assert r.status_code == 200, r.text
                r = await client.post(
                    "/admin/keys", headers={"X-Admin-Token": ADMIN}, json={"team_id": team, "label": "test"}
                )
                keys[team] = r.json()["key"]
            yield Env(client, app, keys, providers)
