---
last_updated: 2026-10-21
scope: gate_report
milestone: M3.4
status: accepted
verdict: M3.4 PASSED
---

# Gate Report: Milestone M3.4 — Local Worker & Bounded Scheduler

## 1. Executive Summary & Verdict

- **Milestone:** M3.4 — Dedicated Local Worker Process, Persistent Job Polling/Claiming, Bounded Concurrency (Initial 2), Account Mutual Exclusion, and Workflow Runner Integration.
- **Verdict:** **`M3.4 PASSED`**
- **Scope Verified:**
  - **Local Background Worker Process (`app/workers/local_worker.py`):**
    - Runs completely decoupled from HTTP API request lifecycles.
    - CLI entrypoints: `run-worker` and `adbap worker` with configurable concurrency, poll interval, provider filter, base URL, headless mode, worker ID, and drain timeout.
    - Continuous polling loop claiming eligible jobs from persistent database queue.
  - **Bounded Concurrency (`asyncio.Semaphore`):**
    - Bounded execution concurrency strictly configured (default `max_concurrency = 2`).
    - Permit acquired prior to claiming each job; guaranteed release in `finally` block.
    - Verified under contention and burst load: maximum simultaneous browser executions strictly $\le 2$.
  - **Zero Database Transactions Held During Browser Execution (4-Phase Split):**
    - **Phase 1 (Claim):** Short DB transaction claims next job, acquires per-account lock, reads workflow and session reference, commits and closes connection.
    - **Phase 2 (Start):** Short DB transaction transitions job `LEASED -> RUNNING`, validates lease fencing, commits and closes connection.
    - **Phase 3 (Browser Automation):** Long-running Playwright browser automation runs with **ZERO open DB transactions or connections held**. Verified that independent transactions can freely read and write without blocking or busy timeouts.
    - **Phase 4 (Finalize):** Short DB transaction records all step results and transitions job to terminal state (`SUCCEEDED`, `FAILED`, `CANCELLED`) or `PAUSED_FOR_REVIEW`, releasing account lock.
  - **Account Mutual Exclusion & Safety Hold (`JobRepository.claim_next_job`):**
    - Database-level constraint (`account_execution_locks` table) prevents concurrent jobs on the same account across connections/workers.
    - **Account-Level Safety Hold:** If an account has any job in `PAUSED_FOR_REVIEW` (due to an unverified mutating side-effect), `claim_next_job` skips all queued jobs for that account until the ambiguous job is reviewed and resolved by an operator.
  - **BrowserManager Reuse & Context Isolation:**
    - Single shared Chromium browser process launched once and reused across worker lifetime.
    - Ephemeral, isolated `BrowserContext` created per execution and guaranteed closed in `finally`.
    - No cross-account cookie or storage leakage.
  - **Lease Ownership & Monotonic Fencing Checks:**
    - `JobModel.lease_token` is incremented monotonically on each claim.
    - Worker passes `(worker_id, lease_token)` on `start_job`, `complete_job`, and `fail_job`.
    - Stale worker whose lease was stolen or expired fails closed with lease fence violation; stale results are discarded without overwriting newer state.
  - **Graceful Shutdown & Drain Mode:**
    - Catches `SIGINT` (Ctrl+C) and `SIGTERM` signals.
    - Immediately stops accepting/claiming new jobs (`_running = False`).
    - In-flight jobs are allowed to complete cleanly within a configurable drain timeout window (default 30s).
    - BrowserManager and remaining active contexts are cleanly closed.
  - **Security & Policy Boundaries:**
    - Re-validates provider policy before browser launch: Microsoft Rewards automation is unconditionally rejected (`POLICY_DENIED`) and audited without executing any browser action.
    - Decrypted session cookies retrieved from `SecureSessionStore` and injected into Playwright context without plaintext secrets logged or exposed.
  - **End-to-End Real Chromium Testing:**
    - Live RewardLab server on loopback in background daemon thread.
    - Real Chromium browser launched in headless mode, navigating to RewardLab `/login`, asserting visibility of interactive elements, and persisting step execution outcomes.
  - **Quality Gates:**
    - **267 passing tests across workspace** (203 backend + 64 rewardlab).
    - **14 dedicated M3.4 tests in `backend/tests/test_local_worker.py`**.
    - **0 lint errors** in Ruff across both `backend` and `rewardlab`.
    - Zero regressions across M0–M3.3.

---

## 2. Test Execution Breakdown

### 2.1 Workspace Test Summary

| Test Suite | Location | Tests | Duration | Warnings | Result |
|---|---|---|---|---|---|
| **Backend Test Suite** | `backend/tests/` | **203 passed** | 79.55s | 1 (Starlette deprecation) | **PASSED** |
| - Local Worker & Bounded Scheduler (M3.4) | `test_local_worker.py` | 14 passed | 7.09s | 0 | `PASSED` |
| - Job Orchestration & Locking (M3.3) | `test_job_orchestration.py` | 21 passed | 1.15s | 0 | `PASSED` |
| - Secure Session Store (M3.2) | `test_session_store.py` | 20 passed | 3.65s | 0 | `PASSED` |
| - Database Persistence (M3.1) | `test_persistence.py` | 12 passed | 0.65s | 0 | `PASSED` |
| - CLI Runner & Hardening (M2.5) | `test_cli.py` | 29 passed | 7.10s | 0 | `PASSED` |
| - Runtime Security & Hardening (M2.4) | `test_runtime_security.py` | 21 passed | 9.20s | 0 | `PASSED` |
| - Browser Manager & Lifecycle (M2.3) | `test_browser_manager.py` | 13 passed | 15.90s | 0 | `PASSED` |
| - Action Interpreter (M2.2) | `test_action_interpreter.py` | 17 passed | 24.80s | 0 | `PASSED` |
| - RewardLab Live E2E Integration (M2.2) | `test_workflow_e2e_rewardlab.py` | 3 passed | 4.80s | 0 | `PASSED` |
| - Workflow DSL & Parser (M2.1) | `test_workflow_dsl.py` | 24 passed | 0.07s | 0 | `PASSED` |
| - Policy & URL Validation (M0) | `test_policy.py` | 25 passed | 0.04s | 0 | `PASSED` |
| - Health Endpoint (M0) | `test_health.py` | 1 passed | 0.01s | 0 | `PASSED` |
| - Secret Redaction (M0) | `test_logging.py` | 3 passed | 0.01s | 0 | `PASSED` |
| **RewardLab Test Suite** | `rewardlab/tests/` | **64 passed** | 1.86s | 1 (Starlette deprecation) | **PASSED** |
| **Workspace Total** | Entire Repo | **267 passed** | **81.41s** | **2** | **ALL PASSED** |

---

## 3. Detailed Verification Matrix (M3.4)

| Requirement / Scenario | Test Name | Input / Precondition | Expected Outcome | Observed Result |
|---|---|---|---|---|
| **Bounded Concurrency = 2** | `test_worker_concurrency_bound` | 4 queued jobs on 4 distinct accounts | At most 2 concurrent executions in Playwright; all 4 complete | **PASSED** |
| **Same-Account Mutual Exclusion** | `test_same_account_mutual_exclusion` | 2 queued jobs on identical account `acc_solo` | Job 2 strictly waits for Job 1 to finish; zero concurrent runs | **PASSED** |
| **Different-Account Parallelism** | `test_different_accounts_parallel_execution` | 2 queued jobs on `acc_alpha` and `acc_beta` | Both execute simultaneously in parallel (concurrency = 2) | **PASSED** |
| **12 Synthetic Accounts Batch** | `test_twelve_synthetic_accounts_batch` | 12 queued jobs across 12 fake accounts | Concurrency bounded $\le 2$; all 12 complete; all locks released | **PASSED** |
| **Multi-Worker Atomic Claim** | `test_atomic_claim_under_multi_worker_contention` | 2 workers polling 6 queued jobs on file SQLite | No double-claims or duplicate leases; each job processed once | **PASSED** |
| **Lease Fencing Stale Rejection** | `test_lease_token_fencing_drops_stale_results` | Worker lease token superseded by newer claim | Stale worker complete raises fence violation; results dropped | **PASSED** |
| **Worker Graceful Shutdown** | `test_worker_graceful_shutdown` | Worker stopped while jobs in-flight | Polling halts immediately; active jobs drain and succeed | **PASSED** |
| **Job Cancellation Cleanup** | `test_cancellation_cleanup` | Active job receives cancellation | Transitions to `CANCELLED`; account lock released cleanly | **PASSED** |
| **State & Step Persistence** | `test_job_state_and_step_persistence` | Executed workflow with navigation | Step results saved in `job_steps`; audit events emitted | **PASSED** |
| **Microsoft Rewards Denied** | `test_policy_denial_microsoft_rewards_rejected` | Workflow with `provider_id='microsoft_rewards'` | Rejected with `POLICY_DENIED`; zero browser actions initiated | **PASSED** |
| **Encrypted Session Injection** | `test_encrypted_session_retrieval` | Account with AES-256-GCM encrypted session | Session decrypted and passed to context; no plaintext in DB | **PASSED** |
| **Side-Effect Pause & Safety Hold** | `test_ambiguous_side_effect_pause_and_account_safety_hold` | Mutating step fails without verification | Job enters `PAUSED_FOR_REVIEW`; follow-up job on account blocked | **PASSED** |
| **Zero DB Lock During Browser Run** | `test_no_database_transaction_held_during_browser_execution` | Concurrent DB write during long browser execution | External transaction completes without waiting or database locks | **PASSED** |
| **Real Chromium E2E on RewardLab** | `test_worker_e2e_real_chromium` | Live RewardLab server + real Chromium browser | Browser launches, navigates `/login`, asserts button, completes | **PASSED** |

---

## 4. Key Architectural Decisions & Invariants

1. **Anti-Starvation Invariant (4-Phase Transaction Split):**
   Holding SQLite write transactions across network or browser operations can lock the database file for seconds, starving all other workers and API threads. By decoupling into 4 distinct phases (Claim $\rightarrow$ Start $\rightarrow$ Browser Execution $\rightarrow$ Finalize), database connections are only held for sub-millisecond durations.
2. **Account-Level Safety Hold:**
   When an ambiguous mutating action fails (`requires_verification=True`), the job enters `PAUSED_FOR_REVIEW`. `JobRepository.claim_next_job` explicitly checks for existing `PAUSED_FOR_REVIEW` jobs for that candidate account and skips it, guaranteeing no further actions execute on an account whose state is pending operator review.
3. **Monotonic Lease Fencing:**
   Every job transition validates `(worker_id, lease_token)`. If a worker's lease expires and the job is claimed or reconciled by another worker, the stale worker's completion is rejected.
4. **Strict Scope Discipline:**
   Milestone M3.4 is strictly completed. M3.5 crash recovery daemons, M4 distributed PostgreSQL nodes, and M5 dashboard are deferred to their designated milestones.
