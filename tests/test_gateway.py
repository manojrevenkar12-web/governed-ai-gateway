"""The request path end to end, against PostgreSQL and Redis, with mock providers."""

from __future__ import annotations

import asyncio
import json

import psycopg
from psycopg.rows import dict_row

from aigw import audit
from tests.conftest import ADMIN, DB

IBAN = "DE89 3704 0044 0532 0130 00"


def body(text: str, model: str = "model-a", **kw) -> dict:
    return {"model": model, "messages": [{"role": "user", "content": text}], **kw}


async def test_redacted_values_never_reach_the_provider_and_come_back_in_the_answer(env):
    r = await env.client.post(
        "/v1/chat/completions", headers=env.auth("payments"), json=body(f"Refund {IBAN} for anna@example.com")
    )
    assert r.status_code == 200, r.text
    sent = env.providers["primary"].seen[-1][-1].content
    assert IBAN not in sent and "anna@example.com" not in sent and "[IBAN_1]" in sent
    answer = r.json()["choices"][0]["message"]["content"]
    assert IBAN in answer and "anna@example.com" in answer  # restored for the user
    assert r.headers["x-aigw-redactions"] == "2"


async def test_streaming_restores_placeholders_split_across_chunks(env):
    async with env.client.stream(
        "POST",
        "/v1/chat/completions",
        headers=env.auth("payments"),
        json=body(f"Pay {IBAN} now", stream=True),
    ) as r:
        lines = [ln async for ln in r.aiter_lines() if ln.startswith("data: ") and ln != "data: [DONE]"]
    text = "".join(json.loads(ln[6:])["choices"][0]["delta"].get("content", "") for ln in lines)
    assert IBAN in text and "[IBAN" not in text
    assert "[IBAN_1]" in env.providers["primary"].seen[-1][-1].content


async def test_secrets_and_forbidden_data_are_blocked_without_echoing_them(env):
    r = await env.client.post(
        "/v1/chat/completions",
        headers=env.auth("payments"),
        json=body("deploy with AKIAIOSFODNN7EXAMPLE please"),
    )
    assert r.status_code == 403 and r.json()["error"]["code"] == "data_blocked"
    assert "AKIA" not in r.text
    r = await env.client.post(
        "/v1/chat/completions",
        headers=env.auth("support"),
        json=body("customer email is bob@example.org", model="model-b"),
    )
    assert r.status_code == 403 and "1 email" in r.json()["error"]["message"]
    assert env.providers["primary"].calls == 0


async def test_models_tools_and_approvals_follow_the_policy(env):
    r = await env.client.post(
        "/v1/chat/completions", headers=env.auth("support"), json=body("hi", model="model-a")
    )
    assert r.status_code == 403 and r.json()["error"]["code"] == "model_not_allowed"
    r = await env.client.get("/v1/models", headers=env.auth("support"))
    assert [m["id"] for m in r.json()["data"]] == ["model-b"]
    r = await env.client.post(
        "/v1/tools/authorize", headers=env.auth("payments"), json={"tool": "drop_database"}
    )
    assert r.status_code == 403 and r.json()["error"]["code"] == "tool_not_allowed"
    r = await env.client.post(
        "/v1/tools/authorize", headers=env.auth("payments"), json={"tool": "create_pull_request"}
    )
    assert r.json()["error"]["code"] == "approval_required"
    r = await env.client.post(
        "/v1/tools/authorize",
        headers={**env.auth("payments"), "X-Approval": "CHG-1042"},
        json={"tool": "create_pull_request"},
    )
    assert r.status_code == 200


async def test_output_is_capped_by_policy_and_cost_is_reported(env):
    r = await env.client.post(
        "/v1/chat/completions",
        headers=env.auth("support"),
        json=body("x" * 4000, model="model-b", max_tokens=100000),
    )
    assert r.status_code == 200
    assert r.json()["usage"]["completion_tokens"] <= 200
    assert float(r.headers["x-aigw-cost-usd"]) > 0


async def test_keys_are_checked_and_revocation_takes_effect(env):
    r = await env.client.post(
        "/v1/chat/completions", headers={"Authorization": "Bearer nope"}, json=body("hi")
    )
    assert r.status_code == 401
    r = await env.client.post(
        "/admin/keys", headers={"X-Admin-Token": ADMIN}, json={"team_id": "payments", "label": "t2"}
    )
    key, key_id = r.json()["key"], r.json()["key_id"]
    ok = await env.client.post(
        "/v1/chat/completions", headers={"Authorization": f"Bearer {key}"}, json=body("hi")
    )
    assert ok.status_code == 200
    await env.client.post(f"/admin/keys/{key_id}/revoke", headers={"X-Admin-Token": ADMIN})
    r = await env.client.post(
        "/v1/chat/completions", headers={"Authorization": f"Bearer {key}"}, json=body("hi")
    )
    assert r.status_code == 401
    r = await env.client.post(
        "/admin/keys", headers={"X-Admin-Token": "wrong"}, json={"team_id": "payments", "label": "x"}
    )
    assert r.status_code == 401


async def test_rate_limits_answer_429_with_retry_after(env):
    codes = []
    for _ in range(8):
        r = await env.client.post(
            "/v1/chat/completions", headers=env.auth("support"), json=body("hi", model="model-b")
        )
        codes.append(r.status_code)
    assert codes.count(200) == 5 and codes.count(429) == 3
    assert int(r.headers["Retry-After"]) >= 1


async def test_fallback_when_the_primary_provider_fails(env):
    env.providers["primary"].failure_rate = 1.0
    for _ in range(8):
        r = await env.client.post("/v1/chat/completions", headers=env.auth("payments"), json=body("hi"))
        assert r.status_code == 200 and r.headers["x-aigw-provider"] == "backup"
    # after 5 consecutive failures the breaker opens and the primary is no longer tried
    assert env.providers["primary"].calls == 5
    breaker = env.app.state.gw.router.breakers["primary"]
    assert breaker.state == "open"


async def test_concurrent_requests_never_overspend_the_budget(env):
    # support: $5 budget. Each request reserves its worst case (4,000 chars in, 200 out at model-b prices).
    gw = env.app.state.gw
    with psycopg.connect(DB, autocommit=True) as conn:
        conn.execute("UPDATE gw.teams SET monthly_budget_usd = 0.002 WHERE id = 'support'")
        conn.execute("UPDATE gw.budgets SET limit_usd = 0.002 WHERE team_id = 'support'")
    gw.key_cache.clear()
    gw.loaded.config.policies["strict"].rate.requests_per_minute = 10_000

    async def one():
        return await env.client.post(
            "/v1/chat/completions", headers=env.auth("support"), json=body("y" * 4000, model="model-b")
        )

    results = await asyncio.gather(*[one() for _ in range(40)])
    codes = [r.status_code for r in results]
    assert 200 in codes and 402 in codes
    with psycopg.connect(DB, row_factory=dict_row) as conn:
        b = conn.execute(
            "SELECT spent_usd, reserved_usd, limit_usd FROM gw.budgets WHERE team_id = 'support'"
        ).fetchone()
    assert b["spent_usd"] <= b["limit_usd"] and b["reserved_usd"] == 0


async def test_every_request_is_in_the_audit_trail_and_tampering_is_detected(env):
    for text in ("one", f"two {IBAN}", "AKIAIOSFODNN7EXAMPLE"):
        await env.client.post("/v1/chat/completions", headers=env.auth("payments"), json=body(text))
    with psycopg.connect(DB, row_factory=dict_row, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT action, detail FROM gw.audit_log WHERE action = 'completion' ORDER BY seq"
        ).fetchall()
        assert [r["detail"]["status"] for r in rows] == ["ok", "ok", "blocked"]
        assert all(IBAN not in json.dumps(r["detail"]) for r in rows)  # hashes, never prompts
        ok, n, _ = audit.verify(conn)
        assert ok and n >= 6
        conn.execute(
            "UPDATE gw.audit_log SET detail = jsonb_set(detail, '{status}', '\"ok\"') WHERE seq = "
            "(SELECT max(seq) FROM gw.audit_log WHERE action = 'completion')"
        )
        ok, _, problem = audit.verify(conn)
        assert not ok and "modified" in problem


async def test_policy_reload_rejects_an_invalid_file(env, tmp_path):
    gw = env.app.state.gw
    before = gw.loaded.sha256
    gw.settings.policy_file = str(tmp_path / "bad.yaml")
    (tmp_path / "bad.yaml").write_text("providers: {}\nmodels: {}\npolicies: {p: {models: [ghost]}}\n")
    r = await env.client.post("/admin/policies/reload", headers={"X-Admin-Token": ADMIN})
    assert r.status_code == 400 and "ghost" in r.json()["error"]["message"]
    assert gw.loaded.sha256 == before  # the running policy is untouched
