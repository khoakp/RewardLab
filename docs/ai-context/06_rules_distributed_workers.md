---
last_updated: 2026-10-19
scope: distributed_workers
status: accepted
---

# Distributed Workers & Scheduling Rules

- Control plane assigns/leases jobs; worker independently validates policy/capabilities before running.
- Use a dedicated worker process rather than handling durable browser jobs inside API request/BackgroundTasks.
- PostgreSQL transactional claims for multi-host; SQLite only for the documented one-host bootstrap.
- Each lease has owner, expiry and monotonically changing fencing token; stale results must be rejected.
- Account-level exclusion must work **across nodes**, not only asyncio locks within one Python process.
- A heartbeat means worker alive, not necessarily action complete; expired lease requires server-side reconciliation before retry.
- Backoff bounded, respect `429 Retry-After`, stop for policy block; never schedule actions simply to defeat provider controls.
- Workers authenticate to the controller with node-scoped credentials; support revocation, capacity advertisement and drain mode.
- Node telemetry: CPU, RSS, free RAM; for Android add battery, charging, thermal and background-execution limitations.
- Do not advertise `browser_automation` capability from an Android agent until a real test runtime passes acceptance.

## Acceptance tests

- Two nodes race for a job -> only one current lease.
- A stale worker reports completion -> result rejected or reconciled, no stale overwrite.
- Worker crashes after server-side task commit -> test server has exactly one side effect after recovery.
- Drained/offline node receives no new job; secret of account A is not provided to node assigned B.
