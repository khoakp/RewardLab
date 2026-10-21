---
last_updated: 2026-10-21
scope: gate_report
milestone: M3.3
status: accepted
verdict: M3.3 PASSED
---

# Gate Report: Milestone M3.3 — Job State Machine & Atomic Per-Account Locking

## 1. Executive Summary & Verdict

- **Milestone:** M3.3 — Persistent Job State Machine, Unique Idempotency Keys, Database-Level Per-Account Execution Lock, Failure Classification, Bounded Backoff, and Side-Effect Safety.
- **Verdict:** **`M3.3 PASSED`**
- **Scope Verified:**
  - **Persistent Job State Machine (`app/orchestration/state_machine.py`):**
    - Enforces strict transition graph: `PENDING -> VALIDATING -> QUEUED -> LEASED -> RUNNING -> SUCCEEDED | FAILED | RETRY_WAIT | PAUSED_FOR_REVIEW | CANCELLED | REJECTED`.
    - Terminal states (`SUCCEEDED`, `FAILED`, `CANCELLED`, `REJECTED`) are strictly immutable: any transition out raises `TerminalStateModificationError`.
    - Illegal state jumps raise `IllegalStateTransitionError` (subclass of `ValueError`).
  - **Database-Level Per-Account Mutual Exclusion Lock (`app/persistence/models.py` & `repository.py`):**
    - `AccountExecutionLockModel` mapped to table `account_execution_locks` with `account_id` as PRIMARY KEY and `job_id` as UNIQUE.
    - Zero dependency on Python `threading.Lock`: mutual exclusion is enforced by the database engine at constraint level across independent processes and connections.
    - Automatic cleanup of locks when jobs reach terminal states or pause for operator review.
    - Transaction rollback safety verified: rolling back an uncommitted transaction leaves zero orphaned locks.
  - **Atomic Job Claim in SQLite (`JobRepository.claim_next_job` & `JobService.claim_job`):**
    - Atomically acquires `account_execution_lock` and updates `JobModel` (`status='leased'`, monotonic `lease_token + 1`, `lease_until`) within the same transaction.
    - Fast read-check in `cleanup_expired_locks` minimizes write lock contention.
    - SQLite PRAGMAs configured: `PRAGMA foreign_keys=ON`, `PRAGMA journal_mode=WAL`, `PRAGMA busy_timeout=10000`, and `BEGIN IMMEDIATE` on transaction begin to prevent upgrade deadlocks.
  - **Multi-Connection Concurrency Safety on File-Backed SQLite:**
    - Verified on real file-backed SQLite database on disk (`m3_3_concurrency.db`):
      1. Two independent DB connections claiming for the same account: exactly one acquires the lock; the other receives `None` and catches zero corruption or deadlocks.
      2. Two independent DB connections claiming for different accounts: both succeed concurrently without blocking.
  - **Idempotent Job Submission (`JobService.submit_job`):**
    - Exact replay: submitting with identical `workflow_version_id` and `account_id` returns existing job and `is_duplicate=True` without re-execution.
    - Conflicting replay: reusing key with different parameters raises `IdempotencyConflictError`.
    - Concurrency race on insert safely caught via nested savepoint and resolved cleanly.
  - **Security & Policy Boundaries (`JobService`):**
    - Submitting Microsoft Rewards workflow is unconditionally rejected with `PolicyDeniedException` and audited as `policy_denied`.
    - Provider-account mismatch rejected: account belonging to provider X cannot run workflow belonging to provider Y.
    - Unapproved (draft) workflows cannot be submitted.
    - Zero secrets in job records: column inspection verifies no passwords, tokens, or cookies are stored.
  - **Failure Classification & Bounded Backoff (`classify_failure`, `compute_backoff_seconds`):**
    - Categorizes errors into `POLICY_DENIED`, `VALIDATION_ERROR`, `AUTH_FAILED`, `LOCATOR_NOT_FOUND`, `ACTION_TIMEOUT`, `SIDE_EFFECT_AMBIGUOUS`, `INFRASTRUCTURE_FAILURE`, `SYSTEM_ERROR`.
    - Bounded exponential backoff: $T = \min(\text{base} \cdot 2^{(\text{attempt}-1)}, \text{max})$.
    - **Side-Effect Safety Invariant:** Any step with `requires_verification=True` that fails or times out is classified as `SIDE_EFFECT_AMBIGUOUS`, transitions the job to `PAUSED_FOR_REVIEW`, and strictly prohibits auto-retry (`can_retry=False`).
  - **Quality Gates:**
    - **253 passing tests across workspace** (189 backend + 64 rewardlab).
    - **21 dedicated M3.3 tests in `backend/tests/test_job_orchestration.py`**.
    - **0 lint errors** in Ruff across both `backend` and `rewardlab`.
    - Zero regressions across M0–M3.2.

---

## 2. Test Execution Breakdown

### 2.1 Workspace Test Summary

| Test Suite | Location | Tests | Duration | Warnings | Result |
|---|---|---|---|---|---|
| **Backend Test Suite** | `backend/tests/` | **189 passed** | 67.50s | 1 (Starlette deprecation) | **PASSED** |
| - Job Orchestration & Locking (M3.3) | `test_job_orchestration.py` | 21 passed | 1.11s | 0 | `PASSED` |
| - Secure Session Store (M3.2) | `test_session_store.py` | 20 passed | 3.70s | 0 | `PASSED` |
| - Database Persistence (M3.1) | `test_persistence.py` | 12 passed | 0.65s | 0 | `PASSED` |
| - CLI Runner & Hardening (M2.5) | `test_cli.py` | 29 passed | 7.00s | 0 | `PASSED` |
| - Runtime Security & Hardening (M2.4) | `test_runtime_security.py` | 21 passed | 9.10s | 0 | `PASSED` |
| - Browser Manager & Lifecycle (M2.3) | `test_browser_manager.py` | 13 passed | 15.80s | 0 | `PASSED` |
| - Action Interpreter (M2.2) | `test_action_interpreter.py` | 17 passed | 24.50s | 0 | `PASSED` |
| - RewardLab Live E2E Integration (M2.2) | `test_workflow_e2e_rewardlab.py` | 3 passed | 4.80s | 0 | `PASSED` |
| - Workflow DSL & Parser (M2.1) | `test_workflow_dsl.py` | 24 passed | 0.07s | 0 | `PASSED` |
| - Policy & URL Validation (M0) | `test_policy.py` | 25 passed | 0.04s | 0 | `PASSED` |
| - Health Endpoint (M0) | `test_health.py` | 1 passed | 0.01s | 0 | `PASSED` |
| - Secret Redaction (M0) | `test_logging.py` | 3 passed | 0.01s | 0 | `PASSED` |
| **RewardLab Test Suite** | `rewardlab/tests/` | **64 passed** | 2.44s | 1 (Starlette deprecation) | **PASSED** |
| **Workspace Total** | Entire Repo | **253 passed** | **69.94s** | **2** | **ALL PASSED** |

---

## 3. Detailed Verification Matrix (M3.3)

| Requirement / Scenario | Test Name | Input / Precondition | Expected Outcome | Observed Result |
|---|---|---|---|---|
| **Valid State Transitions** | `test_valid_transitions_pass` | All allowed transitions in graph | Transitions succeed without error | **PASSED** |
| **Invalid State Jumps** | `test_invalid_transitions_rejected` | `PENDING -> RUNNING`, `QUEUED -> SUCCEEDED` | Raises `IllegalStateTransitionError` | **PASSED** |
| **Terminal State Immutability** | `test_terminal_states_immutable` | Any transition from terminal states | Raises `TerminalStateModificationError` | **PASSED** |
| **Side-Effect Failure Safety** | `test_failure_classification_side_effects` | `requires_verification=True` failure | Yields `SIDE_EFFECT_AMBIGUOUS`, `can_retry=False` | **PASSED** |
| **Failure Classification** | `test_failure_classification_categories` | Error codes (LOCATOR, TIMEOUT, POLICY, AUTH) | Categorized and retryability evaluated accurately | **PASSED** |
| **Bounded Exponential Backoff** | `test_bounded_exponential_backoff` | Attempts 1, 2, 3, 4, 10 | Follows $2 \times 2^{n-1}$, capped at 60s | **PASSED** |
| **Idempotent Submission Replay** | `test_idempotent_submission_identical_params` | Submit identical job twice | Returns existing job, `is_duplicate=True` | **PASSED** |
| **Idempotency Key Conflict** | `test_idempotent_submission_conflict` | Same key with different account | Raises `IdempotencyConflictError` | **PASSED** |
| **Microsoft Rewards Rejection** | `test_microsoft_rewards_automation_rejected` | Workflow with `provider_id='microsoft_rewards'` | Raises `PolicyDeniedException`, audit recorded | **PASSED** |
| **Unapproved Workflow Rejection** | `test_unapproved_workflow_rejected` | Draft workflow submission | Rejected with `ValueError` | **PASSED** |
| **Provider Mismatch Rejection** | `test_provider_mismatch_rejected` | Account provider != workflow provider | Rejected with `ValueError` | **PASSED** |
| **Lifecycle & Lock Release** | `test_job_execution_lifecycle_success` | Full submit -> claim -> start -> complete | Lock active during execution, released on SUCCEEDED | **PASSED** |
| **Lease Fencing Enforcement** | `test_lease_fencing_enforcement` | Impostor worker or stale lease token | Raises `ValueError: Lease fence violation` | **PASSED** |
| **Side-Effect Pause for Review** | `test_side_effect_failure_pauses_for_review` | Mutating step fails with timeout | Enters `PAUSED_FOR_REVIEW`, lock freed, no auto-retry | **PASSED** |
| **Retry & Backoff Lifecycle** | `test_retry_backoff_transitions` | Transient locator failure (attempt 1/3) | Enters `RETRY_WAIT`, delay scheduled, attempt=2 | **PASSED** |
| **File DB Per-Account Exclusion** | `test_file_db_per_account_mutual_exclusion` | 2 connections claim jobs for same account | Connection 1 claims, Connection 2 receives `None` | **PASSED** |
| **File DB Multi-Account Concurrency**| `test_file_db_multi_account_concurrent_claim` | 2 connections claim jobs for diff accounts | Both succeed concurrently via `asyncio.gather` | **PASSED** |
| **Rollback Safety (No Orphan Locks)**| `test_file_db_transaction_rollback_safety` | Lock acquired then transaction rolled back | Re-opened session verifies 0 orphaned locks | **PASSED** |
| **Process Restart Persistence** | `test_file_db_process_restart_persistence` | Engine disposed mid-lease and re-opened | Job status (`LEASED`) and lock intact on disk | **PASSED** |
| **Zero Secrets in Job Records** | `test_no_secrets_persisted_in_job_records` | Inspect all columns of `JobModel` | No passwords, tokens, cookies, or secrets found | **PASSED** |
| **Audit Event Correctness** | `test_audit_event_correctness` | Full job execution flow | Structured events emitted for all transitions | **PASSED** |

---

## 4. Invariants and Architectural Boundaries

1. **Per-Account Mutual Exclusion:**
   - Enforced by `account_execution_locks.account_id` as PRIMARY KEY in SQLite.
   - At no point can two worker connections hold execution locks for the same account simultaneously.
2. **Atomic Claim Protocol:**
   - Claim acquires the per-account execution lock and marks the job as `LEASED` with incremented `lease_token` in a single atomic transaction.
   - If another worker wins a race for the candidate job, the lock acquisition is safely rolled back.
3. **Side-Effect Non-Replay:**
   - External browser actions that mutate state (`requires_verification=True`) never retry automatically on ambiguous failure or timeout.
   - The job enters `PAUSED_FOR_REVIEW`, releasing the execution lock and requiring explicit operator review before any resumption.
4. **SQLite Concurrency Invariants:**
   - Enabled `PRAGMA journal_mode=WAL`, `PRAGMA foreign_keys=ON`, and `PRAGMA busy_timeout=10000`.
   - SQLite driver configured with `isolation_level=None` and `BEGIN IMMEDIATE` to prevent deadlocks when upgrading from read to write locks.

---

## 5. Scope Delimitation & Next Steps

- **Milestone M3.3 is complete.**
- **Deferred to M3.4:** Dedicated local background worker process and queued scheduler daemon (max concurrency initial 2). Durable jobs will run in the worker process rather than API request handlers.
- **Deferred to M4:** Multi-node distributed scheduler with PostgreSQL `FOR UPDATE SKIP LOCKED`, gRPC node agents, and distributed leases across hosts.
