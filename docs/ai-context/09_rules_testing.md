---
last_updated: 2026-10-19
scope: testing_observability
status: accepted
---

# Tests, Metrics & Evidence Rules

- Acceptance criteria must have exact tests and observed outputs; a passing lint/build does not prove browser workflow correctness.
- Unit: policy, DSL schema, job state transitions, validation and error classification.
- Integration: RewardLab + Playwright with synthetic sessions and server-verified outcomes.
- Chaos: crash, expired lease, duplicate result, network partition, retry-after, session expiry, redirects and cancelled jobs.
- Tests must not depend on live Microsoft Rewards accounts, reward issuance, real identity, AI tokens or network services unless test explicitly opts into approved fixtures.
- Use fake clocks, seeded accounts, deterministic fault injection and repeatable automation fixtures.
- Collect measured CPU/RSS, context count, startup, p50/p95 job latency, retry, completion, crash; record workload, host/OS and sample size.
- Benchmark data goes to `docs/spikes/`; current status is a one-line summary/link in `01_status.md`.
- Status transitions are persisted and audit logs are structured/redacted; log redaction gets explicit tests.
- Report `PASS`/`FAIL`/`NOT RUN` accurately, and why tests were skipped.
