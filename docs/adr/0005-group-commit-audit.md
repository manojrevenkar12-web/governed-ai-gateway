# 5. Audit entries are written in group commits

**Status:** accepted

**Context.** The audit trail is a hash chain, so appends are serialised. With one append per request, each under
its own lock and transaction, every request also paid a round of lock waiting that grows with concurrency.

**Decision.** One writer task appends all waiting entries, and their usage rows, in one transaction under one lock.
Each request waits for its own entry to commit before answering.

**Consequences.** Nothing is acknowledged that is not in the audit trail, and the lock is taken once per batch
instead of once per request. On the 2-vCPU test machine the measured gain at 32 concurrent requests was modest
(overhead p50 75 ms before, about 65-70 ms after, throughput about 250/s either way): at that load the machine is
CPU-bound, so the answer is more replicas, not a cleverer lock. At 8 concurrent requests the gateway adds about
10 ms (p50).
