# AI Enablement: team charter

## Why we exist

Every team should be able to use AI in its daily work and in what it builds, without each one
solving security, compliance, cost and reliability on its own. We provide that shared foundation and
shape how the company builds software with AI.

## Who we serve

| Customer | What they need from us |
|---|---|
| Product engineering teams | AI coding assistants and agentic workflows that work out of the box and are safe to use on our code |
| Teams building AI into products | Governed model access with predictable latency, cost and data handling |
| Business operations | Approved tools for summarising, drafting and analysis, with customer data protected |
| Security, legal and compliance | Evidence: who used which model with which data, and that the rules were enforced |
| Leadership | Adoption, reliability, cost and impact, reported monthly |

## What we own

- The gateway: model access, keys, policies, data protection, rate limits, budgets, routing, audit.
- The policy repository: rules as code, changed by pull request, reviewed with security and compliance.
- Observability for AI usage: metrics, the usage ledger, the monthly report ([example](../../reports/USAGE.md)).
- AI-assisted engineering practices: tooling, guidance, workshops, the adoption programme ([ADOPTION.md](../ADOPTION.md)).
- Vendor relationships and licences for model providers and AI developer tools, with procurement.

## What we do not own

- What product teams build with AI, and their prompts and evaluations: we give them the tools to do it well.
- The data classification scheme (security owns it) and the legal basis for processing (legal owns it).
  We implement both in the platform and tell those owners when reality and the rules disagree.

## How we measure ourselves

| Area | Measure | Target |
|---|---|---|
| Adoption | Weekly active users per team, share of engineers using AI tools weekly | growing month on month; 70% of engineers after two quarters |
| Reliability | Gateway availability; overhead p95; provider failures absorbed | 99.9% monthly; < 25 ms; no user-visible failure for a single-provider outage |
| Trust | Data-protection recall on the held-out corpus; audit chain verified daily; policy change lead time | ≥ 99%; daily, intact; < 2 working days |
| Cost | Spend against budget per team; cost per request by use case; month-end forecast accuracy | no unplanned overrun; trend down per use case; ±10% |
| Impact | Engineering delivery metrics for teams that adopted (lead time, change failure rate), developer survey | measured before and after rollout, per team |

## How we work

- **Planning.** Quarterly goals agreed with my manager and stakeholders ([ROADMAP.md](../ROADMAP.md)); two-week
  cycles; roughly 70% new capability, 20% reliability and maintenance, 10% internal support, adjusted from data.
- **Decisions.** Architecture decisions recorded as ADRs ([adr/](../adr)); tech leads own them, I make sure they get
  made in time and that security and compliance are in the room for anything touching data.
- **Operations.** On-call, SLOs, incidents and runbooks in [OPERATIONS.md](OPERATIONS.md).
- **People.** Weekly 1:1s, individual development plans, structured hiring ([HIRING-AND-GROWTH.md](HIRING-AND-GROWTH.md)).
- **Openness.** Monthly demo for the whole company; office hours twice a week; a public roadmap.
