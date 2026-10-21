---
last_updated: 2026-10-19
scope: db
status: planned
---

# Database Context — Navigation, not a second schema definition

The canonical **planned** domain model is in [`docs/ARCHITECTURE.md`](../ARCHITECTURE.md), section **Domain model (core tables)**. This file avoids copying its fields so the plan cannot silently diverge.

- Before touching SQLAlchemy models/migrations, inspect actual model code and the latest Alembic revision; code + migrations are authoritative after implementation.
- No schema exists in this specification-only repository yet. Do not infer that planned fields were migrated.
- Enforce unique idempotency keys, scoped ownership and account/session isolation in database constraints where feasible; document transactional guarantees and limitations.
- Never write raw auth state to relational tables; store only a vault reference.
- Data migrations need forward/backward consideration and direct testing. Destructive changes require explicit approval.
- Record schema-impact decision under `docs/adr/` only when a lasting tradeoff is made.
