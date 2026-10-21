---
last_updated: 2026-10-21
scope: gate_report
milestone: Personal-P2
status: accepted
verdict: PERSONAL-P2 PASSED
---

# Gate Report: Milestone Personal P2 — Causal Verification Hardening & Safe Operator Console

## 1. Executive Summary & Verdict

- **Milestone:** Personal P2 — Causal Verification Hardening & Safe Operator Console
- **Verdict:** **`PERSONAL-P2 PASSED`**
- **Objective:** Eliminate FIFO-order false positives in business outcome verification through explicit mutation-to-postcondition causal linking, provide a safe Personal Operator Console (`adbap jobs`) to inspect and resolve paused jobs without side-effect replay, and introduce a lightweight, zero-browser workflow inspector (`adbap inspect-workflow`) with secure diagnostics referencing.
- **Invariants Established:**
  1. **Explicit Causal Mapping (`verifies_step_ids`):** `VerifyStateStep` now optionally specifies `verifies_step_ids: list[str] | None`. The schema validator statically enforces that referenced steps exist, appear before the verification step, are classified as mutating, and do not conflict in task scope.
  2. **Zero False Positives from Duplicate Verifications:** Re-verifying a mutation B multiple times only fulfills mutation B (which is already fulfilled); mutation A remains in `unverified_mutations`, fail-closing the workflow to `OUTCOME_UNCONFIRMED` (`can_retry=False`).
  3. **Backward-Compatible Legacy Resolution:** Legacy workflows omitting `verifies_step_ids` with a single mutation or a cohesive form sequence (`fill* -> click/press_key`) are causal-linked automatically. Ambiguous legacy workflows with multiple disjoint mutations fail-closed.
  4. **Strict State-Machine Operator Review:** Paused jobs can be reviewed via `adbap jobs review <job-id> --decision {cancel,reject} --reason "..."`. Only `CANCELLED` and `REJECTED` transitions are permitted from `PAUSED_FOR_REVIEW`. Transitions directly to `SUCCEEDED` are strictly prohibited. Reasons are mandatory and recorded in immutable audit events.
  5. **Account Safety Hold Preservation:** Accounts with any job in `PAUSED_FOR_REVIEW` remain blocked from claiming new jobs in `JobRepository.claim_next_job`. Reviewing one paused job keeps the hold active if other paused jobs exist on the same account. Cross-account isolation is preserved.
  6. **Zero-Browser Workflow Inspection:** `adbap inspect-workflow` parses DSL, prints execution plans, maps mutations to verifications, and validates provider policy without launching Chromium or network sockets.
  7. **Diagnostics Path Traversal Hardening:** `resolve_diagnostic_path` strictly rejects path traversal patterns (`..`, outside directory paths) and restrains reads to valid JSON diagnostics inside `backend/var/diagnostics/` with full credential redaction.

---

## 2. Root Cause Analysis: FIFO vs Causal Verification

| Area | Flaw in P1.1 Implementation | Causal Hardening in P2 |
|---|---|---|
| **Mutation Resolution Mechanism** | Used `unverified_mutations.pop(0)` upon successful `verify_state`. This resolved pending mutations strictly in FIFO order, regardless of what resource was verified. | Replaced FIFO queue with explicit step ID resolution. `VerifyStateStep` references the specific mutation(s) it proves. Successful verification only relieves those specific step IDs. |
| **Duplicate Verification Vulnerability** | Executing: `[mutate A] -> [mutate B] -> [verify B] -> [verify B]` caused the second verification of B to pop mutation A from the FIFO queue, falsely marking the workflow as `STATE_VERIFIED`. | Re-verifying mutation B marks B verified (idempotent); mutation A is never removed from `unverified_mutations`. Leftover mutation A triggers `OUTCOME_UNCONFIRMED`. |
| **Static Reference Validation** | In P1.1, verification steps did not declare targets, allowing verifications to assert arbitrary criteria disconnected from previous mutating steps. | Added `validate_verification_mappings()` in `schema.py` enforcing: referenced step exists, referenced step precedes verification, referenced step is mutating, step IDs are unique, and task scopes match. |
| **Legacy DSL Compatibility** | Naive enforcement could break existing single-mutation DSL v1 workflows. | Implemented safe legacy inference: single pending mutation or `fill* -> submit` sequences are causal-linked; multiple ambiguous mutations fail-closed. |

---

## 3. Formal Causal Verification Acceptance Cases (A – L)

All 12 required canonical causal cases were verified in the dedicated test suite:

| Case | Scenario | Expected Outcome | P2 Test Result |
|---|---|---|---|
| **Case A** | `verify A -> mutate A -> end` | `OUTCOME_UNCONFIRMED` (Postcondition preceded mutation) | **PASS** |
| **Case B** | `mutate A -> verify A -> mutate B -> end` | `OUTCOME_UNCONFIRMED` (Mutation B unverified) | **PASS** |
| **Case C** | `mutate A -> verify A -> end` | `STATE_VERIFIED` (Causal postcondition confirmed) | **PASS** |
| **Case D** | `mutate A -> verify A -> mutate B -> verify B` | `STATE_VERIFIED` (Both mutations explicitly verified) | **PASS** |
| **Case E** | `mutate A -> mutate B -> verify B -> verify B` | `OUTCOME_UNCONFIRMED` (FIFO flaw eliminated; A unverified) | **PASS** |
| **Case F** | `mutate A -> verify B` | `WorkflowValidationError` (Forward/unrelated reference rejected) | **PASS** |
| **Case G** | Verification references non-existent step | `WorkflowValidationError` (`step_id not found`) | **PASS** |
| **Case H** | Verification references future mutation | `WorkflowValidationError` (`must appear before verification`) | **PASS** |
| **Case I** | Verification references non-mutating step | `WorkflowValidationError` (`must be a mutating step`) | **PASS** |
| **Case J** | Mutation succeeded on server but HTTP response lost | `OUTCOME_UNCONFIRMED` (No blind retry; routed to review) | **PASS** |
| **Case K** | Legacy workflow with 1 mutation and 1 verification | `STATE_VERIFIED` (Backward compatibility preserved) | **PASS** |
| **Case L** | Legacy workflow with ambiguous multiple mutations | `OUTCOME_UNCONFIRMED` (Fail-closed; cannot guess mapping) | **PASS** |

---

## 4. Personal Operator Console Architecture

### 4.1 CLI Command Design

```text
# List paused jobs (or filter by status/account)
adbap jobs list [--status PAUSED_FOR_REVIEW] [--account ACCOUNT_ID] [--limit 50] [--json]

# Show detailed execution, outcome, and audit trail for a job
adbap jobs show <job-id> [--json]

# Review a paused job (cancel or reject with mandatory reason)
adbap jobs review <job-id> --decision {cancel,reject} --reason "Operator verified side effect" [--json]
```

### 4.2 State Machine Review Rules
- Review is exclusively permitted when `job.status == PAUSED_FOR_REVIEW`.
- Only `--decision cancel` (`JobStatus.CANCELLED`) and `--decision reject` (`JobStatus.REJECTED`) are accepted.
- Transition directly to `SUCCEEDED` is strictly prohibited.
- Reasons cannot be empty or whitespace.
- Operator identity, timestamp, decision, and rationale are recorded in `audit_events`.
- Double review / repeated decisions fail-closed (`IllegalStateTransitionError`).

### 4.3 Account Safety Hold Rules
- `JobRepository.claim_next_job` skips candidate jobs if any job for that account is in `PAUSED_FOR_REVIEW`.
- When an account has multiple paused jobs, cancelling one maintains the safety hold until ALL paused jobs for the account reach terminal states.
- Actions on account A never affect claiming for account B.

---

## 5. Workflow Inspector & Diagnostics Hardening

### 5.1 Zero-Browser Workflow Inspector
`adbap inspect-workflow <path> [--json]` provides a static pre-flight check:
1. Validates YAML/JSON against DSL v1 schema and causal verification mappings.
2. Evaluates `ProviderPolicy` (e.g. rejects Microsoft Rewards or unknown providers without launching Chromium).
3. Prints execution steps, action types, locators, mutation classification, and verification mappings.
4. Alerts on unverified mutating steps or missing postconditions.

### 5.2 Diagnostics Path Traversal Hardening
`resolve_diagnostic_path` in `backend/app/execution/diagnostics.py` ensures:
- Rejection of relative paths containing `..`.
- Rejection of paths resolving outside `backend/var/diagnostics/`.
- File existence verification.
- Safe JSON artifact parsing with full credential redaction.

---

## 6. Modified & Created Files

| File | Type | Changes Made |
|---|---|---|
| [`backend/app/workflows/schema.py`](file:///d:/Projects/AutomationTool/backend/app/workflows/schema.py) | Modified | Added `verifies_step_ids: list[str] \| None` on `VerifyStateStep`; added `task_id` to mutating steps; implemented `is_step_mutating()` and `validate_verification_mappings()`. |
| [`backend/app/workflows/results.py`](file:///d:/Projects/AutomationTool/backend/app/workflows/results.py) | Modified | Added `unverified_mutations: list[str]` and `verified_mutations: list[str]` to `WorkflowExecutionResult`. |
| [`backend/app/workflows/interpreter.py`](file:///d:/Projects/AutomationTool/backend/app/workflows/interpreter.py) | Modified | Replaced FIFO queue popping with explicit causal mapping resolution; added duplicate verification prevention; handled legacy single-mutation/form-fill compatibility; populated execution result mutation lists. |
| [`backend/app/persistence/repository.py`](file:///d:/Projects/AutomationTool/backend/app/persistence/repository.py) | Modified | Added `list_jobs(status, account_id, limit, offset)` to `JobRepository`. |
| [`backend/app/orchestration/job_service.py`](file:///d:/Projects/AutomationTool/backend/app/orchestration/job_service.py) | Modified | Added `list_jobs()`, `get_job_detail()`, and `review_job()` with strict state-machine transition validation and audit logging. |
| [`backend/app/execution/diagnostics.py`](file:///d:/Projects/AutomationTool/backend/app/execution/diagnostics.py) | Modified | Added `resolve_diagnostic_path()` with path traversal rejection and `read_diagnostic_artifact()` with JSON credential redaction. |
| [`backend/app/operator_cli.py`](file:///d:/Projects/AutomationTool/backend/app/operator_cli.py) | Created | Implemented `adbap jobs` (list/show/review) and `adbap inspect-workflow` subcommands with table and JSON output modes and strict secret redaction. |
| [`backend/app/cli.py`](file:///d:/Projects/AutomationTool/backend/app/cli.py) | Modified | Integrated operator subcommands `jobs` and `inspect-workflow` into main CLI entry point. |
| [`backend/tests/test_personal_p2_causal_and_operator.py`](file:///d:/Projects/AutomationTool/backend/tests/test_personal_p2_causal_and_operator.py) | Created | Added 27 dedicated tests covering Cases A–L, duplicate verification prevention, operator console, safety hold invariants, and traversal protection. |

---

## 7. Acceptance Test Evidence

### 7.1 Quality Gates Summary

| Component | Command | Result |
|---|---|---|
| **Backend Lint** | `cd backend && uv run ruff check .` | **PASS** (0 errors) |
| **Backend Tests** | `cd backend && uv run pytest -q` | **PASS** (266 passed, 0 failed in 127.76s) |
| **RewardLab Lint** | `cd rewardlab && uv run ruff check .` | **PASS** (0 errors) |
| **RewardLab Tests** | `cd rewardlab && uv run pytest -q` | **PASS** (64 passed, 0 failed in 2.17s) |
| **Total Test Suite** | Combined Test Pass Count | **330 passed, 0 failed (100% pass rate)** |

### 7.2 Dedicated Test Suite (`backend/tests/test_personal_p2_causal_and_operator.py`)

All 27 dedicated tests passed:
- `test_p2_causal_case_a_verify_before_mutate_yields_unconfirmed`
- `test_p2_causal_case_b_mutate_verify_mutate_yields_unconfirmed`
- `test_p2_causal_case_c_mutate_verify_end_yields_state_verified`
- `test_p2_causal_case_d_multi_mutation_verified_yields_state_verified`
- `test_p2_causal_case_e_duplicate_verification_does_not_clear_other_mutation`
- `test_p2_causal_case_f_schema_rejects_mismatched_task_scope`
- `test_p2_causal_case_g_schema_rejects_nonexistent_referenced_step`
- `test_p2_causal_case_h_schema_rejects_forward_reference`
- `test_p2_causal_case_i_schema_rejects_non_mutating_referenced_step`
- `test_p2_causal_case_j_lost_response_fails_closed_no_blind_retry`
- `test_p2_causal_case_k_legacy_single_mutation_retains_compatibility`
- `test_p2_causal_case_l_legacy_ambiguous_mutations_fail_closed`
- `test_p2_operator_cli_jobs_list_filters_and_formats`
- `test_p2_operator_cli_jobs_list_json_output`
- `test_p2_operator_cli_jobs_show_details_and_redaction`
- `test_p2_operator_cli_jobs_review_cancel_success`
- `test_p2_operator_cli_jobs_review_reject_success`
- `test_p2_operator_cli_jobs_review_rejects_empty_reason`
- `test_p2_operator_cli_jobs_review_rejects_illegal_transition_direct_success`
- `test_p2_operator_cli_jobs_review_repeated_decision_rejected`
- `test_p2_account_safety_hold_blocks_claim_until_reviewed`
- `test_p2_account_safety_hold_persists_when_multiple_paused_jobs_exist`
- `test_p2_account_safety_hold_cross_account_isolation`
- `test_p2_workflow_inspector_prints_plan_zero_browser`
- `test_p2_workflow_inspector_rejects_unauthorized_provider`
- `test_p2_diagnostics_path_traversal_rejected`
- `test_p2_diagnostics_safe_read_redacts_credentials`

---

## 8. Residual Risks & Next Steps

1. **Semantic Postcondition Proof Limits:** Static `verifies_step_ids` ensures causal coupling and step identity, but cannot verify whether the remote server's database assertion fully captures all side effects of an action. Postconditions must be verified against domain APIs (e.g. `rewardlab_api`).
2. **Personal Edition Boundaries Retained:** SQLite single-process file locking, single `LocalWorker` concurrency, no distributed Postgres, no React/Next.js dashboard.
3. **Next Steps:**
   - Milestone Personal P2 is complete and verified.
   - Optional future milestone: P2.2 Lightweight Web UI or Android Agent POC (upon explicit operator direction).
