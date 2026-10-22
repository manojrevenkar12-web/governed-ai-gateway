# 1. Budgets are reserved before a request, not checked

**Status:** accepted

**Context.** A budget check followed by spending after the answer lets concurrent requests all pass the check before
any of them is booked. The evaluation shows it: 300 requests, 64 at a time, against a $0.05 budget overspend by
16-23% with check-then-spend.

**Decision.** Each request reserves its worst-case cost (estimated input plus its full output allowance) in one
conditional `UPDATE ... WHERE spent + reserved + amount <= limit`, and settles the actual cost after. A reaper
returns reservations older than 10 minutes left by a crashed replica.

**Consequences.** No overspend under concurrency (0% in the same test). Near the end of a budget, a request may be
refused although its actual cost would have fitted: we accept slightly early refusals over overspend.
