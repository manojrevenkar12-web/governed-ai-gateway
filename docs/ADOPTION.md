# Rolling out AI-assisted engineering

A platform nobody uses has no impact. Adoption is part of the job, planned and measured like a feature.

## Phases

1. **Pilot (4 weeks).** Two volunteer teams with different work (a product team, a platform team). Tools configured
   through the gateway from day one, so the data rules hold. Weekly feedback; we fix friction before going wider.
2. **Champions.** One engineer per team who has used the tools for a month; they run their team's first session and
   are the first port of call.
3. **Wide rollout.** Team by team, each with a 90-minute workshop on its own codebase: what to delegate to an agent,
   how to review AI-written code, what never to paste (and what the gateway does if you do).
4. **Agentic workflows.** After assistants: agents in the development lifecycle (dependency upgrades, test
   generation, incident triage), each starting in shadow mode beside the people who do the work today.

## Guidance we publish

- How to get a key, which models to use for what, and what each costs per typical request.
- Reviewing AI-written code: the same bar as any code; tests first; the author owns it.
- Data rules in plain language, with examples of what is redacted and what is blocked.

## Measuring impact

| Measure | Why |
|---|---|
| Weekly active users per team | Adoption, from the usage ledger |
| Lead time for changes, change failure rate | Delivery, before and after rollout, per team |
| Developer survey (quarterly) | Whether people find it useful, and where it gets in the way |
| Cost per request by use case | Whether value scales sensibly with spend |

We do not measure lines of code or "share of code written by AI": they reward the wrong behaviour.

## From my own experience

At Wells Fargo I introduced GenAI tooling into my team's daily work: an LLM triage assistant on every CI build that
classified failures from raw logs, and retrieval over past defects. Manual investigation fell by about 40%. What made
it stick was the same as above: start with a real pain, measure it, and make the safe path the easy one.
