---
last_updated: 2026-10-19
scope: navigation
status: accepted
---

# Context Router — Load on Demand

Always load `AGENTS.md`, `00_core.md`, and `01_status.md`; for coding also read the **current milestone** in `docs/IMPLEMENTATION_PLAN.md`.

| Task touches | Read only the additional relevant context |
|---|---|
| SQLAlchemy models, database, migrations | `02_db_schema.md`, architecture Domain model section |
| Browser, contexts, auth/session, locator | `04_rules_browser_sessions.md` |
| DSL, action interpreter, workflow versioning, Recorder | `05_rules_workflows.md` |
| Scheduler, nodes, queues, leases, Android agent | `06_rules_distributed_workers.md` |
| LLM planning, prompt/DOM input, self-healing | `07_rules_ai_assistant.md` |
| Provider policy, URL boundary, security, external integrations | `08_rules_security_policy.md` |
| Testing, benchmark, observability, CI | `09_rules_testing.md` |
| Architecture/contract decision | `docs/ARCHITECTURE.md`, relevant ADR |
| Cross-module feature | Read the impacted modules only; announce why more context is required |

## Proposed initial layout

```text
AGENTS.md                       # Agent entry point
START_PROMPT.md                 # First-run prompt
GOALS.md                        # Stable project outcomes
CHANGELOG.md                    # Short append-only changes
docs/ARCHITECTURE.md            # Canonical design contracts
docs/IMPLEMENTATION_PLAN.md     # Milestones and acceptance
docs/GOAL_WORKFLOW.md           # /plan /goal usage
docs/ai-context/                # Core, status and domain rules
docs/adr/                       # Architecture decision records
docs/spikes/                    # Experimental evidence and gate reports
backend/                        # Created during M0
rewardlab/                      # Created during M1
frontend/                       # Created during M5
```

Files labeled as *created during* do not yet exist; do not invent current code structure.
