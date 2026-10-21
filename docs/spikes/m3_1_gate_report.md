---
last_updated: 2026-10-20
scope: gate_report
milestone: M3.1
status: accepted
verdict: M3.1 PASSED
---

# Gate Report: Milestone M3.1 — Database Persistence Layer & Pre-M3 CLI Hardening

## 1. Executive Summary & Verdict

- **Milestone:** M3.1 — Database Schema, Persistence Layer, Transaction Handling, and Pre-M3 CLI Security Hardening.
- **Verdict:** **`M3.1 PASSED`**
- **Scope Verified:**
  - **Pre-M3 CLI Security Review & Hardening:**
    - **Issue 1 (`--session-id` credential exposure):** Emits an explicit security warning when `--session-id` is passed via CLI process arguments, encouraging operators to use `REWARDLAB_SESSION_ID` or `REWARDLAB_PASSWORD` environment variables instead to prevent process table / shell history sniffing.
    - **Issue 2 (Cross-account session binding):** Implemented `verify_session_account_binding` in `app/cli.py` validating session ownership via the provider `/api/tasks` endpoint. If an active session belongs to `test_02` but the operator passes `--account test_01`, execution is immediately aborted with `ExitCode.POLICY_DENIED` and error code `SESSION_ACCOUNT_MISMATCH` before allocating any browser contexts.
    - **Issue 3 (CLI Entry Points):** Added `build-system` (`hatchling`) and wheel package configuration to `backend/pyproject.toml` ensuring `uv sync` installs `run-workflow` and `adbap run-workflow` entry points into the virtual environment path.
    - **Issue 4 (SIGINT / Resource Cleanup):** Verified through automated cancellation tests that `finally` blocks in `app/cli.py` and `BrowserManager.close_all()` reliably tear down Playwright contexts and Chromium child processes even under sudden process interruption.
    - **Issue 5 (JSON Stability & Secret Redaction):** Verified that `--json` output maintains strict key stability and applies `sanitize_text` across all fields, ensuring zero leakage of bearer tokens or raw cookies.
  - **Milestone M3.1 Database Persistence Layer:**
    - **Architecture Compliance:** Fully implemented the 9 core tables defined in `docs/ARCHITECTURE.md` Section 5 using SQLAlchemy 2.0 async ORM (`app/persistence/models.py`).
    - **Tables Implemented:**
      1. `providers`: Provider registry with status, capabilities, and rate-limit configurations.
      2. `accounts`: Synthetic accounts linked to providers with authentication states (`UNCONFIGURED`, `READY`, `INVALID_CREDENTIALS`, `RESTRICTED`, `COOLDOWN`).
      3. `session_refs`: Secure session reference metadata (encrypted reference pointer, expiration timestamp, scopes; strictly zero plaintext passwords or bearer tokens).
      4. `workflow_versions`: Approved, immutable workflow definitions with SHA256 integrity hash and approval states.
      5. `jobs`: Orchestration job model with unique idempotency keys, single-active-job constraint per account, priority, and state machine status.
      6. `job_steps`: Granular execution tracking with composite unique constraint `(job_id, step_key, attempt)`.
      7. `nodes`: Node heartbeats, state (`ACTIVE`, `DRAINING`, `OFFLINE`), and concurrency bounds for future M4 scaling.
      8. `audit_events`: Append-only, immutable audit trail for operator actions, policy evaluations, and system events.
      9. `activity_records`: Provider compliance, cooldown tracking, and rate limiting counters.
    - **Database & Foreign Key Invariants (`app/persistence/database.py`):**
      - Asynchronous engine and session factory (`create_async_engine`, `async_sessionmaker`).
      - Event listener enforcing SQLite PRAGMA `foreign_keys = ON` on all SQLite connections to guarantee referential integrity.
    - **Schema Migrations (`alembic/`):**
      - Initialized Alembic migration framework (`backend/alembic.ini`, `backend/alembic/env.py`).
      - Created baseline migration `5577dab1c31c_initial_schema_m3_1.py` with `render_as_batch=True` for SQLite DDL compatibility.
    - **Repository Layer (`app/persistence/repository.py`):**
      - Implemented clean async repositories: `ProviderRepository`, `AccountRepository`, `SessionRefRepository`, `WorkflowVersionRepository`, `JobRepository`, and `AuditEventRepository`.
      - Enforced valid job state machine transitions via `VALID_JOB_TRANSITIONS` (`PENDING -> QUEUED -> RUNNING -> COMPLETED/FAILED/CANCELLED/TIMEOUT`), rejecting invalid state jumps with `ValueError`.
      - Implemented `acquire_account_execution_lock` and `get_active_job_for_account` ensuring strict single-job execution per account at the persistence layer.
  - **Quality Gates:**
    - **212 passing tests across workspace** (148 in `backend/tests/`, 64 in `rewardlab/tests/`).
    - **12 dedicated M3.1 persistence tests in `backend/tests/test_persistence.py`**.
    - **29 CLI tests in `backend/tests/test_cli.py`** (including 2 new security regression tests).
    - **0 lint errors** in Ruff across both `backend` and `rewardlab`.
    - Zero regressions across M0–M2.5.

---

## 2. Test Execution Breakdown

### 2.1 Workspace Test Summary

| Test Suite | Location | Tests | Duration | Warnings | Result |
|---|---|---|---|---|---|
| **Backend Test Suite** | `backend/tests/` | **148 passed** | 69.60s | 1 (Starlette deprecation) | **PASSED** |
| - Database Persistence (M3.1) | `test_persistence.py` | 12 passed | 0.70s | 0 | `PASSED` |
| - CLI Runner & Security Hardening (M2.5) | `test_cli.py` | 29 passed | 7.15s | 0 | `PASSED` |
| - Runtime Security & Hardening (M2.4) | `test_runtime_security.py` | 21 passed | 9.30s | 0 | `PASSED` |
| - Browser Manager & Lifecycle (M2.3) | `test_browser_manager.py` | 13 passed | 16.14s | 0 | `PASSED` |
| - Playwright Action Interpreter (M2.2) | `test_action_interpreter.py` | 17 passed | 25.10s | 0 | `PASSED` |
| - RewardLab Live E2E Integration (M2.2) | `test_workflow_e2e_rewardlab.py` | 3 passed | 5.04s | 0 | `PASSED` |
| - Workflow DSL & Parser (M2.1) | `test_workflow_dsl.py` | 24 passed | 0.07s | 0 | `PASSED` |
| - Policy & Static URL Validation (M0) | `test_policy.py` | 25 passed | 0.04s | 0 | `PASSED` |
| - Health Endpoint (M0) | `test_health.py` | 1 passed | 0.01s | 0 | `PASSED` |
| - Secret Redaction (M0) | `test_logging.py` | 3 passed | 0.01s | 0 | `PASSED` |
| **RewardLab Test Suite** | `rewardlab/tests/` | **64 passed** | 2.43s | 1 (Starlette deprecation) | **PASSED** |
| **Workspace Total** | Entire Repo | **212 passed** | **72.03s** | **2** | **ALL PASSED** |

---

## 3. Detailed Verification Matrix (M3.1 Persistence & Pre-M3 CLI)

| Requirement / Scenario | Test Name | Input / Precondition | Expected Outcome | Observed Result |
|---|---|---|---|---|
| **CLI Argument Warning for Session ID** | `test_cli_session_id_cli_arg_warning` | `--session-id sec_xyz` passed on CLI | Emits warning on stderr advising env var usage | **PASSED** |
| **Cross-Account Session Mismatch Denial** | `test_cli_cross_account_session_mismatch_denied` | Session belongs to `test_02`, CLI specifies `--account test_01` | Exit code `2` (`POLICY_DENIED`); `SESSION_ACCOUNT_MISMATCH` emitted; zero browser launch | **PASSED** |
| **Schema & Table Initialization** | `test_schema_table_creation` | In-memory SQLite async engine | All 9 core tables and indexes created successfully | **PASSED** |
| **Provider Repository CRUD** | `test_provider_repository_crud` | Insert and query provider models | Status updates and capability JSON stored and retrieved cleanly | **PASSED** |
| **Account Creation & Unique Constraints**| `test_account_creation_and_unique_constraint`| Duplicate `(provider_id, account_id)` | First insert succeeds; second insert raises `IntegrityError` | **PASSED** |
| **Foreign Key Enforcement** | `test_foreign_key_violation_rejected` | Account referencing non-existent provider | SQLite raises `IntegrityError` due to active `PRAGMA foreign_keys=ON` | **PASSED** |
| **Session Reference Protection** | `test_session_ref_management` | Insert session ref with encrypted payload | Raw token not exposed; active session queried; expired session excluded | **PASSED** |
| **Workflow Immutability & Hash Unique** | `test_workflow_version_immutability_and_unique_constraint` | Duplicate workflow version or hash | Version duplicate raises `IntegrityError`; approved state recorded | **PASSED** |
| **Job Idempotency Key Uniqueness** | `test_job_idempotency_key_uniqueness` | Re-insert same idempotency key for same provider | Second job creation raises `IntegrityError` | **PASSED** |
| **Job State Machine Valid Transitions** | `test_job_state_machine_valid_transitions` | `PENDING -> QUEUED -> RUNNING -> COMPLETED` | Transitions succeed; timestamp tracking recorded | **PASSED** |
| **Job State Machine Invalid Transition** | `test_job_state_machine_invalid_transition_rejected` | `PENDING -> COMPLETED` (skipping `RUNNING`) | Transition rejected with `ValueError` ("Invalid job status transition") | **PASSED** |
| **Single Active Job Account Exclusion** | `test_single_active_job_account_exclusion` | Two active jobs for same account | `acquire_account_execution_lock` rejects second job; returns `False` | **PASSED** |
| **Job Step Recording & Unique Constraint**| `test_job_step_recording_and_cascade` | Steps for job; duplicate `(job_id, step, attempt)` | Duplicate insert raises `IntegrityError`; step results updated | **PASSED** |
| **Immutable Audit Event Logging** | `test_audit_event_logging` | Record audit events | Events filtered by actor, entity type, and entity ID cleanly | **PASSED** |

---

## 4. Invariant and Safety Boundary Audit

1. **No Out-of-Scope Bloat:**
   - Did **NOT** implement PostgreSQL multi-node leasing, gRPC nodes, or remote agents (deferred to M4).
   - Did **NOT** implement scheduler background daemon (deferred to M3.4).
   - Did **NOT** implement frontend dashboard (deferred to M5).
2. **Persistence Boundary:**
   - Database operations use async sessions and strict schema boundaries.
   - Raw tokens/passwords are strictly forbidden in `session_refs`; only opaque references or encrypted payloads are accepted.
3. **Account Isolation:**
   - Persistence layer models and unique constraints enforce that an account cannot have multiple concurrent running jobs.
   - CLI cross-account validation blocks session hijacking or accidental account mixing.

---

## 5. Artifacts and Migrations Added

- `backend/app/persistence/database.py`: Async engine, sessionmaker, SQLite PRAGMA listener.
- `backend/app/persistence/models.py`: SQLAlchemy 2.0 ORM models for all 9 tables and state enums.
- `backend/app/persistence/repository.py`: Async repositories for providers, accounts, sessions, workflows, jobs, and audit events.
- `backend/alembic/`: Alembic environment configuration and baseline migration `5577dab1c31c_initial_schema_m3_1.py`.
- `backend/tests/test_persistence.py`: 12 comprehensive unit and integration tests.
