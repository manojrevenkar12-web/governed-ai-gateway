# Roadmap

## First 90 days as Engineering Lead

**Days 1-30: listen and stabilise.**
- 1:1s with every engineer and with the leads of the teams we serve; a written summary of what I heard, shared back.
- Review production: SLOs, incidents of the last two quarters, on-call load, open risks, provider contracts and spend.
- Agree with my manager what the team is measured on this quarter.

**Days 31-60: make it visible.**
- A monthly report on adoption, reliability, cost and impact, from the usage ledger ([example](../reports/USAGE.md)).
- Close the top reliability risks from the review (for example: models without a backup provider).
- Hiring plan for the gaps the review found; interview loop and rubric in place.

**Days 61-90: commit.**
- Quarterly goals and a roadmap the team owns, agreed with stakeholders.
- Build-versus-buy decisions written down for the next two capabilities ([BUILD-VS-BUY.md](BUILD-VS-BUY.md)).
- Development plans agreed with every engineer.

## Quarterly goals (example)

**Q1: governed access for every team.**
- Every engineering team on the gateway, with AI coding assistants configured through it. Measure: share of engineers active weekly.
- Data protection v2: personal names and addresses detected by a self-hosted model, so no external call sees them first. Measure: held-out recall for names ≥ 95%.
- Budgets and the monthly report for every team. Measure: no unplanned overrun.

**Q2: agents, safely.**
- Tool authorisation for agent runtimes and an approved-tools registry, with human approval for risky actions. Measure: agentic workflows in production with zero unapproved actions.
- Sandboxed execution for agents that run code, with no standing credentials.
- Prompt caching and model routing by task to cut cost per request. Measure: cost per request by use case.

**Q3: impact.**
- Before-and-after delivery metrics for teams that adopted AI-assisted engineering, published.
- Self-service onboarding: a team can get a key, a policy and a budget in a day.
