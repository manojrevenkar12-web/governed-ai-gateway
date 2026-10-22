# 2. Rate limiting fails open to local limits; auditing fails closed

**Status:** accepted

**Context.** Rate limits use Redis so replicas share one bucket per key. Budgets and the audit trail use PostgreSQL.

**Decision.** If Redis is unreachable the gateway keeps serving, limiting each key per replica to its share of the
rate, and raises an alert. If PostgreSQL is unreachable the gateway refuses requests.

**Consequences.** A Redis outage costs precision of rate limits, not availability. A PostgreSQL outage is a gateway
outage, deliberately: in a regulated company, unaudited and unbudgeted use of AI is worse than a short outage.
