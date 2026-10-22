"""A simulated month of a company using the platform, and the report leadership would get from it.

Six teams roll out AI tools over four weeks: engineering teams with coding assistants and agents,
customer operations summarising cases (with customer data that must be redacted), a payments team
on the self-hosted model, marketing with no customer data allowed. Users join week by week, some
paste secrets or customer data, one team runs into its budget. Every request goes through the real
gateway (with mock models); the report is built only from the gateway's own usage ledger.
"""

from __future__ import annotations

import asyncio
import calendar
import random
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import psycopg
import redis
from psycopg.rows import dict_row

from aigw.app import Settings, create_app
from aigw.providers import MockProvider

ADMIN = "sim-admin"
POLICY = Path(__file__).resolve().parents[3] / "policies" / "fintech-example.yaml"

TEAMS = [
    # id, name, policy, budget USD, users by week, requests per user-day, use cases
    (
        "payments-eng",
        "Payments Engineering",
        "engineering",
        900,
        (6, 14, 22, 28),
        9,
        ("coding", "code-review", "agents"),
    ),
    (
        "platform-eng",
        "Platform Engineering",
        "engineering",
        700,
        (8, 12, 16, 18),
        8,
        ("coding", "incident-analysis"),
    ),
    ("data", "Data & Analytics", "engineering", 12, (2, 5, 9, 12), 6, ("sql", "coding")),
    (
        "customer-ops",
        "Customer Operations",
        "customer-operations",
        250,
        (0, 10, 25, 40),
        7,
        ("case-summary", "reply-draft"),
    ),
    ("payments-ops", "Payments Operations", "payments-restricted", 50, (3, 4, 6, 6), 5, ("reconciliation",)),
    ("marketing", "Marketing", "marketing", 2, (2, 4, 5, 6), 4, ("copy",)),
]
MODELS = {
    "coding": [("claude-sonnet-5-5", 0.7), ("claude-haiku-5-5", 0.2), ("claude-opus-5-5", 0.1)],
    "code-review": [("claude-sonnet-5-5", 1.0)],
    "agents": [("claude-sonnet-5-5", 0.6), ("claude-opus-5-5", 0.4)],
    "incident-analysis": [("claude-sonnet-5-5", 1.0)],
    "sql": [("claude-haiku-5-5", 0.6), ("claude-sonnet-5-5", 0.4)],
    "case-summary": [("claude-haiku-5-5", 1.0)],
    "reply-draft": [("claude-haiku-5-5", 0.7), ("claude-sonnet-5-5", 0.3)],
    "reconciliation": [("local-llm", 1.0)],
    "copy": [("claude-haiku-5-5", 1.0)],
}
FILLER = "The service handles SEPA credit transfers and reconciles them against bank statements. " * 800


def _prompt(use_case: str, rng: random.Random) -> str:
    # characters of context; about four per token (coding assistants send whole files)
    size = {
        "coding": 40000,
        "code-review": 60000,
        "agents": 48000,
        "incident-analysis": 30000,
        "sql": 12000,
        "case-summary": 6000,
        "reply-draft": 4000,
        "reconciliation": 6000,
        "copy": 1500,
    }[use_case]
    body = FILLER[: int(size * rng.uniform(0.5, 1.5))]
    if use_case in ("case-summary", "reply-draft") and rng.random() < 0.6:
        body += (
            f" Customer {rng.choice(['anna', 'jonas', 'mia'])}.{rng.randint(1, 99)}@example.de asks about "
            f"DE89 3704 0044 0532 0130 00 and a payment from +49 30 {rng.randint(1000000, 9999999)}."
        )
    if use_case == "reconciliation":
        body += " Statement line: IBAN GB82 WEST 1234 5698 7654 32, amount 1.250,00 EUR."
    if use_case in ("coding", "incident-analysis") and rng.random() < 0.004:
        body += " config: aws_access_key_id = AKIAIOSFODNN7EXAMPLE"
    if use_case == "copy" and rng.random() < 0.05:
        body += " Quote from our customer lena.weber@example.de: great rates!"
    return body


async def _simulate(database_url: str, redis_url: str, start: date, seed: int) -> None:
    rng = random.Random(seed)
    providers = {
        n: MockProvider(n, keep_seen=False) for n in ("anthropic", "anthropic-backup", "openai", "local")
    }
    app = create_app(Settings(database_url, redis_url, str(POLICY), ADMIN), providers)
    async with app.router.lifespan_context(app):
        # A simulated day runs in seconds, so per-minute rate limits would refuse almost everything.
        for pol in app.state.gw.loaded.config.policies.values():
            pol.rate.requests_per_minute, pol.rate.tokens_per_minute = 10**9, 10**12
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gw", timeout=60
        ) as client:
            keys = {}
            for tid, name, pol, budget, *_ in TEAMS:
                await client.post(
                    "/admin/teams",
                    headers={"X-Admin-Token": ADMIN},
                    json={"id": tid, "name": name, "policy": pol, "monthly_budget_usd": budget},
                )
                r = await client.post(
                    "/admin/keys",
                    headers={"X-Admin-Token": ADMIN},
                    json={"team_id": tid, "label": "simulation"},
                )
                keys[tid] = r.json()["key"]
            # an incident: on day 17 the primary Anthropic account fails for two hours
            outage_day = 17
            for day in range(28):
                d = start + timedelta(days=day)
                if d.weekday() >= 5:
                    continue
                week = day // 7
                providers["anthropic"].failure_rate = 0.5 if day == outage_day else 0.0
                for b in app.state.gw.router.breakers.values():  # a simulated night passes in a second
                    b.success()
                wall = datetime.now()
                jobs = []
                for tid, _, _, _, users_by_week, per_day, cases in TEAMS:
                    for u in range(users_by_week[week]):
                        for _ in range(max(0, int(rng.gauss(per_day, per_day / 3)))):
                            case = rng.choice(cases)
                            models, weights = zip(*MODELS[case], strict=True)
                            jobs.append(
                                (
                                    tid,
                                    f"{tid}-user{u + 1:02d}",
                                    case,
                                    rng.choices(models, weights)[0],
                                    _prompt(case, rng),
                                )
                            )

                async def send(job, sem=asyncio.Semaphore(16)):  # noqa: B008 - one semaphore per day
                    tid, user, case, model, prompt = job
                    async with sem:
                        await client.post(
                            "/v1/chat/completions",
                            json={
                                "model": model,
                                "max_tokens": rng.choice([512, 1024, 2048]),
                                "messages": [{"role": "user", "content": prompt}],
                            },
                            headers={
                                "Authorization": f"Bearer {keys[tid]}",
                                "X-User": user,
                                "X-Use-Case": case,
                            },
                        )

                await asyncio.gather(*[send(j) for j in jobs])
                with psycopg.connect(
                    database_url, autocommit=True
                ) as conn:  # give the day's rows their simulated time
                    conn.execute(
                        """UPDATE gw.usage SET at = %s::date + interval '8 hours' + random() * interval '10 hours'
                            WHERE at >= %s""",
                        (d, wall),
                    )


def simulate(database_url: str, redis_url: str, start: date = date(2026, 9, 1), seed: int = 4) -> None:
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS gw CASCADE")
    redis.Redis.from_url(redis_url).flushdb()
    asyncio.run(_simulate(database_url, redis_url, start, seed))


# ---- the report --------------------------------------------------------------------------------------------------

COLOURS = ["#0f4c5c", "#e36414", "#5f0f40", "#9a031e", "#2a9d8f", "#6c757d"]


def report(database_url: str, out_dir: Path, start: date = date(2026, 9, 1)) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "figures").mkdir(exist_ok=True)
    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        q = lambda sql, *a: conn.execute(sql, a).fetchall()  # noqa: E731
        teams = q("""SELECT t.id, t.name, t.policy, t.monthly_budget_usd::float AS budget,
                            count(u.*) AS requests, count(DISTINCT u.user_ref) AS users,
                            coalesce(sum(u.cost_usd), 0)::float AS spend,
                            count(*) FILTER (WHERE u.status = 'ok') AS ok,
                            count(*) FILTER (WHERE u.status = 'blocked') AS blocked,
                            count(*) FILTER (WHERE u.status = 'over_budget') AS over_budget,
                            coalesce(sum(u.redactions), 0) AS redactions,
                            count(*) FILTER (WHERE u.fallback) AS fallbacks
                       FROM gw.teams t LEFT JOIN gw.usage u ON u.team_id = t.id
                      GROUP BY t.id ORDER BY spend DESC""")
        weekly = q(
            """SELECT team_id, floor(extract(epoch FROM at - %s::timestamptz) / 604800)::int AS week,
                             count(DISTINCT user_ref) AS users, coalesce(sum(cost_usd), 0)::float AS spend,
                             count(*) AS requests
                        FROM gw.usage GROUP BY 1, 2 ORDER BY 2""",
            start,
        )
        models = q("""SELECT model, count(*) AS requests, sum(cost_usd)::float AS spend,
                             percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95_ms
                        FROM gw.usage WHERE status = 'ok' GROUP BY model ORDER BY spend DESC""")
        reasons = q("""SELECT team_id, reason, count(*) AS n FROM gw.usage WHERE status <> 'ok'
                        GROUP BY 1, 2 ORDER BY 3 DESC""")
        cases = q("""SELECT use_case, count(*) AS requests, sum(cost_usd)::float AS spend,
                            (sum(cost_usd) / nullif(count(*), 0))::float AS per_request
                       FROM gw.usage WHERE status = 'ok' GROUP BY 1 ORDER BY spend DESC""")
        classes = q(
            """SELECT unnest(data_classes) AS class, status, count(*) AS n FROM gw.usage GROUP BY 1, 2"""
        )
        crossings = q("""SELECT team_id, min(day) AS day FROM (
                            SELECT u.team_id, date(u.at) AS day,
                                   sum(u.cost_usd) OVER (PARTITION BY u.team_id ORDER BY u.at) AS running,
                                   t.monthly_budget_usd AS budget
                              FROM gw.usage u JOIN gw.teams t ON t.id = u.team_id) x
                           WHERE running >= 0.8 * budget GROUP BY team_id""")
        first_refusal = q("""SELECT team_id, min(date(at)) AS day, count(*) AS n FROM gw.usage
                              WHERE status = 'over_budget' GROUP BY team_id""")
        outage = q("""SELECT date(at) AS day, count(*) AS requests, count(*) FILTER (WHERE fallback) AS fallbacks,
                             count(*) FILTER (WHERE status = 'upstream_error') AS failed
                        FROM gw.usage GROUP BY 1 HAVING count(*) FILTER (WHERE fallback) > 0 ORDER BY 1""")
    days = calendar.monthrange(start.year, start.month)[1]
    elapsed = 28
    total_spend = sum(t["spend"] for t in teams)
    for t in teams:
        t["forecast"] = t["spend"] / elapsed * days
        t["utilisation"] = t["spend"] / t["budget"] if t["budget"] else 0.0

    # charts
    weeks = sorted({w["week"] for w in weekly})
    fig, axes = plt.subplots(1, 2, figsize=(11, 3.6))
    for i, (tid, name, *_rest) in enumerate(TEAMS):
        ys = [
            next((w["users"] for w in weekly if w["team_id"] == tid and w["week"] == wk), 0) for wk in weeks
        ]
        axes[0].plot([f"week {wk + 1}" for wk in weeks], ys, marker="o", color=COLOURS[i], label=name)
    axes[0].set_title("Weekly active users by team", loc="left", fontsize=11)
    axes[0].legend(fontsize=7, frameon=False)
    bottom = [0.0] * len(weeks)
    for i, (tid, name, *_rest) in enumerate(TEAMS):
        ys = [
            next((w["spend"] for w in weekly if w["team_id"] == tid and w["week"] == wk), 0) for wk in weeks
        ]
        axes[1].bar([f"week {wk + 1}" for wk in weeks], ys, bottom=bottom, color=COLOURS[i], label=name)
        bottom = [a + b for a, b in zip(bottom, ys, strict=True)]
    axes[1].set_title("Spend by team (USD)", loc="left", fontsize=11)
    for ax in axes:
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
    fig.tight_layout()
    fig.savefig(out_dir / "figures" / "adoption_and_spend.png", dpi=150)
    plt.close(fig)

    result = {
        "budget_80_crossed": crossings,
        "budget_exhausted": first_refusal,
        "teams": teams,
        "weekly": weekly,
        "models": models,
        "refusals": reasons,
        "use_cases": cases,
        "data_classes": classes,
        "provider_incident": outage,
        "total_spend": total_spend,
    }
    _markdown(result, out_dir / "USAGE.md", start)
    return result


def _markdown(r: dict, path: Path, start: date) -> None:
    t = r["teams"]
    lines = [
        f"# AI platform report: {start:%B %Y} (simulated)",
        "",
        "Generated by `aigw simulate` from the gateway's usage ledger. The month is simulated (mock models, "
        "synthetic users); the numbers show what the platform measures and how leadership would see it.",
        "",
        "![Adoption and spend](figures/adoption_and_spend.png)",
        "",
        "## Teams",
        "",
        "| Team | Policy | Active users | Requests | Spend | Budget | Used | Month-end forecast | Refused by policy | Values redacted |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for x in t:
        lines.append(
            f"| {x['name']} | {x['policy']} | {x['users']} | {x['requests']:,} | ${x['spend']:,.2f} | "
            f"${x['budget']:,.0f} | {x['utilisation']:.0%} | ${x['forecast']:,.2f} | {x['blocked']} | "
            f"{x['redactions']:,} |"
        )
    lines += [
        "",
        "## Where the money goes",
        "",
        "| Use case | Requests | Spend | Per request |",
        "|---|---:|---:|---:|",
    ]
    for c in r["use_cases"]:
        lines.append(
            f"| {c['use_case']} | {c['requests']:,} | ${c['spend']:,.2f} | ${c['per_request']:.4f} |"
        )
    lines += ["", "| Model | Requests | Spend |", "|---|---:|---:|"]
    for m in r["models"]:
        lines.append(f"| {m['model']} | {m['requests']:,} | ${m['spend']:,.2f} |")
    lines += ["", "## Refusals", "", "| Team | Reason | Requests |", "|---|---|---:|"]
    for x in r["refusals"]:
        lines.append(f"| {x['team_id']} | {x['reason']} | {x['n']:,} |")
    lines += ["", "## Budgets", ""]
    for c in r["budget_80_crossed"]:
        lines.append(
            f"- {c['team_id']} passed 80% of its budget on {c['day']} (the alert fires once, to the team "
            "and the platform channel)."
        )
    for c in r["budget_exhausted"]:
        lines.append(
            f"- {c['team_id']} ran out on {c['day']}; {c['n']:,} requests were refused with 402 until the "
            "budget was raised or the month ended."
        )
    lines += ["", "## Provider incident", ""]
    for o in r["provider_incident"]:
        lines.append(
            f"- {o['day']}: the primary Anthropic account failed half its calls. {o['fallbacks']:,} of "
            f"{o['requests']:,} requests that day were served by the backup account; {o['failed']} failed, "
            "all on claude-opus-5-5, which has no backup provider in the policy. Action: add one."
        )
    path.write_text("\n".join(lines) + "\n")
