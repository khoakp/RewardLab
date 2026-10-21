---
last_updated: 2026-10-19
scope: core
status: accepted
---

# ADBAP — Core Context

## Product mission

- Build a provider-agnostic automation platform for **owned or explicitly authorized websites**. Reason: reusable engineering without dependence on a third-party rewards program.
- RewardLab is the owned integration-test target with 12 **synthetic** accounts; these are fixtures, not real identities.
- Microsoft Rewards is a user-assisted companion only: official links, reminders, manually confirmed tracking. Reason: platform account and automation policy.
- `.crx` samples are untrusted design references; never execute or copy code/assets without provenance, license and security review.

## System boundaries

- Control plane owns providers, policies, accounts, workflows, scheduled jobs, status and audit history.
- Execution plane owns worker lifecycle, allowed browser/runtime capabilities and job results.
- A provider adapter declares permitted domains, actions, identity requirements, data sources and success verification.
- Default-deny policy applies before scheduling **and** at runtime; sandbox/provider authorization must not be inferred from a human-like browser experience.
- Account, session, job, workflow version and worker node have separate lifecycles.
- Assigned account count never implies that many simultaneously running Chromium processes.

## Reliability invariants

- A queued job is durable; an API HTTP request never owns a long-lived browser execution.
- Retry means at-least-once **attempts**, never a guarantee of exactly-once effect.
- Sensitive side effects require server-verifiable postconditions and, when possible, idempotency keys.
- Account locks, lease fencing and result reconciliation guard duplicates and stale workers.
- A browser crash, auth challenge, 403 or unexplained side effect stops unsafe execution and enters review/recovery state.

## Data/security invariants

- Authenticated test-session data is confidential; never store plaintext credentials in repository, logs or AI prompts.
- Browser contexts isolate test accounts; do not assume a context alone is a hostile-code security sandbox.
- User supplied goal text, workflow YAML, page DOM and LLM outputs are untrusted inputs requiring strict validation.
- AI may suggest **draft** workflows/repairs only; execution needs schema validation, policy validation, test and approval.

## Technology defaults (change only with documented evidence)

- Backend Python 3.12+, FastAPI, Pydantic v2, SQLAlchemy 2, Alembic, pytest, Ruff.
- Playwright Async + Chromium for authorized Linux/Windows worker.
- SQLite for single-host bootstrap; PostgreSQL for multi-host transactional lease claims.
- Next.js/TypeScript dashboard only after API is stable; Android lightweight-agent feasibility spike, not assumed Chromium pool.
- AI feature flag OFF by default; no paid service or external LLM required for MVP.

## References

- Formal architecture/contracts: `docs/ARCHITECTURE.md`.
- Roadmap/gates: `docs/IMPLEMENTATION_PLAN.md`.
- Current state: `docs/ai-context/01_status.md`.
- Domain-specific instructions: `docs/ai-context/03_folder_map.md`.
