# Running the platform in production

Other teams depend on the gateway to do their work, so it is run like the critical service it is.

## Service-level objectives

| Objective | Target | Measured by | Enforced in CI |
|---|---|---|---|
| Availability (non-5xx answers caused by the gateway) | 99.9% per month | `aigw_requests_total` by status | - |
| Gateway overhead at nominal load | p95 < 25 ms | `aigw_overhead_seconds`; `aigw evaluate` load test | yes |
| Single provider outage | no user-visible failure for models with a backup | failover test; `aigw_fallbacks_total` | yes |
| Data protection | held-out precision and recall ≥ 99% | `aigw evaluate` | yes |
| Budgets | no team spends beyond its budget | budget race test; `gw.budgets` | yes |
| Audit | chain verified intact every day | `aigw audit-verify` (scheduled) | - |

**Error budget.** 0.1% of a month is 43 minutes. When half is spent, risky changes (provider adapters,
policy engine) wait until reliability work lands; when it is gone, the next cycle is reliability first.
The team decides this together with the data, not ad hoc.

## Failure modes and what the gateway does

| Failure | Behaviour | Why |
|---|---|---|
| A model provider fails or slows | Retry on the next provider for the model; circuit breaker takes the failing one out for 30 s, then probes | Most outages are one provider or one account |
| Redis is down | Keep serving; rate limits fall back to a per-replica bucket at the replica's share of the rate; `aigw_ratelimit_degraded_total` alerts | A rate limiter outage should not become a company-wide AI outage |
| PostgreSQL is down | Fail closed: no request is answered without its budget reservation and audit entry | Unaudited use of AI with company data is not acceptable in a regulated company |
| A replica dies mid-request | Its budget reservations are returned by the reaper after 10 minutes | Holds must not leak a team's budget |
| An invalid policy is deployed | Reload is refused, the running policy stays, the error names the problem | Policy mistakes must not lock teams out or open data paths |

## On-call

- Business-hours primary and secondary from the team, weekly rotation; after hours, the company's platform
  on-call covers the gateway with our runbooks, and pages us only for Sev 1.
- At most one week in five per person; on-call load reviewed monthly; a noisy alert is a bug.
- Handover note every Monday: incidents, open risks, provider status, budget alerts.

## Incidents

| Severity | Example | Response |
|---|---|---|
| Sev 1 | Gateway down; data sent to a provider against policy | Page now; incident lead, comms to affected teams every 30 minutes; security informed immediately for any data issue |
| Sev 2 | One model family unavailable; degraded rate limiting | Within the hour; status message in the platform channel |
| Sev 3 | A team's budget exhausted; a policy request blocked wrongly | Same working day |

Every Sev 1 and Sev 2 gets a blameless review within five working days, with actions that have owners and dates.

## Runbooks

**Provider outage.** Check `aigw_breaker_open` and `aigw_fallbacks_total`. If a model has no backup provider,
tell the affected teams which model to switch to. Afterwards: add a backup for that model (the simulated month
shows exactly this case for one model).

**Suspected data leak (data reached a provider that should not have).** Sev 1. Revoke or restrict the key,
identify affected requests from the audit trail by request id and prompt hash (prompts are not stored), add the
missed format to the held-out corpus, fix the detector, and involve security and the DPO for notification duties.

**Leaked API key.** `POST /admin/keys/{id}/revoke`; takes effect within the 10-second key cache. Issue a new key;
review the key's usage in the ledger.

**Team out of budget.** The team gets 402 with spend and limit. Agree a raise with the budget owner, then update
the team (`POST /admin/teams`). Every change is audited.

**Bad policy change.** Revert the pull request and reload. The audit trail records the hash of every policy in force.
