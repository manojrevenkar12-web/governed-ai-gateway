-- The gateway's own state. Prompts and completions are never stored: only their hashes.
CREATE SCHEMA IF NOT EXISTS gw;

CREATE TABLE IF NOT EXISTS gw.teams (
    id                text PRIMARY KEY,
    name              text        NOT NULL,
    policy            text        NOT NULL,           -- name of the policy in policies/*.yaml
    monthly_budget_usd numeric(12, 4) NOT NULL,
    created_at        timestamptz NOT NULL DEFAULT now()
);

-- API keys are random 256-bit secrets; only their SHA-256 is kept.
CREATE TABLE IF NOT EXISTS gw.api_keys (
    id          text PRIMARY KEY,                  -- public prefix shown in logs, e.g. key_3f9a1c
    team_id     text        NOT NULL REFERENCES gw.teams (id),
    key_hash    text        NOT NULL UNIQUE,
    label       text        NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz,
    revoked_at  timestamptz
);

-- Spend per team and month. reserved_usd is money held for requests in flight, so concurrent
-- requests cannot together overrun the budget.
CREATE TABLE IF NOT EXISTS gw.budgets (
    team_id     text           NOT NULL REFERENCES gw.teams (id),
    month       date           NOT NULL,
    limit_usd   numeric(12, 4) NOT NULL,
    spent_usd   numeric(14, 6) NOT NULL DEFAULT 0,
    reserved_usd numeric(14, 6) NOT NULL DEFAULT 0,
    alerted_at_80 timestamptz,
    PRIMARY KEY (team_id, month)
);

CREATE TABLE IF NOT EXISTS gw.reservations (
    id          uuid PRIMARY KEY,
    team_id     text           NOT NULL,
    month       date           NOT NULL,
    amount_usd  numeric(14, 6) NOT NULL,
    created_at  timestamptz    NOT NULL DEFAULT now()
);

-- One row per request: the usage ledger behind cost and adoption reporting.
CREATE TABLE IF NOT EXISTS gw.usage (
    id            bigserial PRIMARY KEY,
    request_id    text           NOT NULL,
    at            timestamptz    NOT NULL DEFAULT now(),
    team_id       text           NOT NULL,
    key_id        text           NOT NULL,
    user_ref      text,
    use_case      text,
    model         text,
    provider      text,
    status        text           NOT NULL,          -- ok, blocked, rate_limited, over_budget, upstream_error
    reason        text,
    input_tokens  int            NOT NULL DEFAULT 0,
    output_tokens int            NOT NULL DEFAULT 0,
    cost_usd      numeric(14, 6) NOT NULL DEFAULT 0,
    latency_ms    int            NOT NULL DEFAULT 0,
    overhead_ms   int            NOT NULL DEFAULT 0,
    data_classes  text[]         NOT NULL DEFAULT '{}',
    redactions    int            NOT NULL DEFAULT 0,
    fallback      boolean        NOT NULL DEFAULT false
);
CREATE INDEX IF NOT EXISTS usage_team_at ON gw.usage (team_id, at);

-- Append-only, hash-chained audit trail: each entry commits to the one before it, so editing or
-- deleting a past entry breaks every hash after it. Holds hashes of prompts, never prompts.
CREATE TABLE IF NOT EXISTS gw.audit_log (
    seq         bigint PRIMARY KEY,
    at          timestamptz NOT NULL,
    actor       text        NOT NULL,              -- team/key or admin
    action      text        NOT NULL,              -- completion, key.create, key.revoke, policy.reload ...
    detail      jsonb       NOT NULL,
    prev_hash   text        NOT NULL,
    hash        text        NOT NULL
);

-- Who may administer the platform is not a gateway concern: admin calls carry a separate token.
