---
last_updated: 2026-10-21
scope: architecture_decisions
status: accepted
---

# ADR 0002: Personal Edition Alignment and Milestone M4 Deferral

## Context

Following the completion of Release R1 (Milestones M0 through M3.6), ADBAP has established a robust local automation stack: SQLite persistence, atomic per-account execution locking, Playwright Async browser execution, encrypted credential stores, crash recovery, and multi-account local orchestration tested across 12 synthetic accounts on RewardLab.

The original product roadmap envisioned Milestone M4 as a multi-host distributed scheduler backed by a PostgreSQL cluster, gRPC nodes, and remote worker agents. However, practical operational requirements prioritize a **Personal Browser Automation Tool**:
1. Single-operator execution on a standard workstation/laptop (e.g., 16 GB RAM).
2. Automating owned or explicitly authorized web applications via authentic browser UI interaction.
3. Lightweight, low-overhead operation without external database servers (PostgreSQL), distributed broker infrastructure, or complex distributed lease clustering.
4. Independent research into an Android lightweight agent (Milestone M8) remains on the roadmap, but is decoupled from multi-node distributed browser clustering.

Crucially, empirical observation across automated web interactions reveals an essential architectural principle:
> **Browser action success does not equal business outcome success.**
A successful DOM click or keystroke does not guarantee that the server-side business mutation succeeded. Furthermore, network dropouts after a commit can leave mutating outcomes ambiguous. Addressing interaction fidelity, postcondition verification, and failure classification locally provides far higher reliability value than prematurely distributing unverified browser actions across a multi-node cluster.

## Alternatives Considered

1. **Proceed with Milestone M4 Distributed Architecture as Planned:**
   - *Pros:* Fulfills the original distributed roadmap item.
   - *Cons:* Incurs significant operational overhead (PostgreSQL instance, connection pooling, network partition handling, multi-host clock skew, distributed fencing tokens). Adds zero reliability benefit to a single laptop user while increasing memory consumption and operational complexity.
2. **Strip Database Persistence Entirely (In-Memory CLI Only):**
   - *Pros:* Minimum memory footprint.
   - *Cons:* Loses auditability, crash recovery, job idempotency, encrypted session reuse, and post-crash reconciliation proved in Release R1.
3. **Personal Edition Architecture & Milestone P1 Focus (Chosen):**
   - *Pros:* Fully reuses Release R1 investments (SQLite, JobService, LocalWorker, SecureSessionStore, BrowserManager). Configures concurrency bounds tuned for laptop hardware (concurrency=1 during active usage, concurrency=2 when idle). Deferring M4 avoids premature optimization. Focuses immediate engineering effort (Milestone P1) on interaction reliability, missing Playwright actions (`hover`, `scroll`, `press_key`, `wait_for_state`), official browser context profiles (desktop/mobile viewports, locale, timezone), and a formal **Verified Outcome Layer**.

## Decision

1. **Product Direction - Personal Edition:**
   - Target runtime: Single-node local execution (Windows/Linux) managed via CLI or local worker process.
   - Storage engine: SQLite with WAL mode and `aiosqlite`.
   - Concurrency bounds: Concurrency default 1 when workstation is in use; bounded concurrency 2 when idle.
2. **Deferral of Milestone M4:**
   - PostgreSQL cluster migration, remote outbound-pull agents, gRPC node communication, and multi-host lease rebalancing are deferred until there is an established business requirement for multi-host scaling.
   - Milestone M8 (lightweight Android agent spike) remains an independent research objective and is not implemented in P1.
3. **Interaction Engine Enhancements (Milestone P1):**
   - Extend strict Workflow DSL v1 with missing Playwright actions: `hover`, `scroll`, `press_key`, and `wait_for_state`.
   - Enhance locators to support accessible roles, labels, text, and test IDs.
   - Check element visibility, enabled state, and interactability prior to dispatch.
   - Abort dispatch immediately if lease or cancellation signal is triggered.
4. **Verified Outcome Layer:**
   - Introduce structured outcome classification without breaking existing database schemas:
     - `ACTION_EXECUTED`: Browser UI actions were dispatched and executed successfully.
     - `STATE_VERIFIED`: Business outcome or postcondition explicitly verified (e.g. via DOM state or provider API).
     - `OUTCOME_UNCONFIRMED`: Mutating side effect occurred, but postcondition could not be conclusively verified (e.g. response lost, network timeout). Transitions job to `PAUSED_FOR_REVIEW` for operator safety hold; never blind auto-retry.
     - `PROVIDER_REJECTED`: Target provider rejected the interaction (e.g. 401 Unauthorized, 403 Forbidden, rate limit refusal, or business error). Job cannot succeed.
     - `HUMAN_ACTION_REQUIRED`: Security challenge, MFA, CAPTCHA, or explicit human intervention required. Transitions job to `PAUSED_FOR_REVIEW`.
5. **Browser Context Profiles:**
   - Support typed configuration for desktop and responsive mobile viewports, device scale factor, touch support, locale, and timezone through standard Playwright APIs.
   - Maintain context configuration consistency throughout execution.
   - No fingerprint overrides or anti-bot evasions.
6. **Preserved Invariants:**
   - `ProviderPolicy` remains strictly deny-by-default.
   - Microsoft Rewards remains companion-only (no automated earning, no multi-account farming, no evasion).
   - No arbitrary script execution, eval, or code generation in DSL.

## Consequences

- **Positive:**
  - High operational reliability on single laptop hardware with minimal memory footprint (< 500 MB peak RSS).
  - Clear distinction between superficial UI clicks and true business state confirmations.
  - Safe handling of ambiguous mutations via operator pause rather than dangerous automated replays.
  - 100% backward compatibility with Release R1 workflows, schemas, and persistence models.
- **Negative / Tradeoffs:**
  - Multi-host scaling is intentionally deferred; all executions run on the local machine.
  - Workflows performing mutating operations must include explicit `verify_state` steps to achieve `STATE_VERIFIED` status, otherwise they are classified as `OUTCOME_UNCONFIRMED` if side effects occurred.

## Verification

Verified via:
- Regression verification against existing M0–M3.6 test suites (278 passing tests).
- New acceptance test suite for Milestone P1 covering interaction engine actions, outcome classifications, responsive browser profiles, and failure modes.
