---
last_updated: 2026-10-20
scope: gate_report
milestone: M1.2
status: accepted
verdict: M1.2 PASSED
---

# Gate Report: Milestone M1.2 — RewardLab Independent Account State

## 1. Executive Summary & Verdict

- **Milestone:** M1.2 — Seed 12 fake test accounts, independent task/point state; no real Microsoft credentials, real rewards or redemption
- **Verdict:** **`M1.2 PASSED`**
- **Scope Verified:**
  - Seeded exactly 12 synthetic accounts (`test_01` to `test_12`) with distinct credentials, labels, and baseline points.
  - Guaranteed independent task state per account: modifying or reviewing a task on `test_01` has zero side effect on `test_02` through `test_12`.
  - Server-side authentication enforcement: all task lookups and state transitions are bound strictly to `get_current_user(request).username`.
  - Cross-account access denial: authenticated users cannot view or review tasks outside their assigned synthetic identity (HTTP 404).
  - Concurrency & idempotency: thread-safe task review (`threading.Lock`) guarantees that simultaneous or repeated review requests award fake points at most once.
  - Deterministic state reset: `store.reset()` restores all 12 accounts and tasks to clean initial state.
  - Test suites: 20/20 RewardLab tests passed, 29/29 Backend regression tests passed (**49 total passing tests**).

---

## 2. Seeded Synthetic Accounts Inventory (12 Accounts)

| Username | Password Pattern | Display Name | Initial Points | Seeded Tasks Count |
|---|---|---|---|---|
| `test_01` | `test_password_01` | Synthetic Test Account 01 | 120 pts | 3 tasks (`task_01`, `task_02`, `task_03`) |
| `test_02` | `test_password_02` | Synthetic Test Account 02 | 50 pts | 3 tasks (`task_01`, `task_02`, `task_03`) |
| `test_03` | `test_password_03` | Synthetic Test Account 03 | 80 pts | 3 tasks (`task_01`, `task_02`, `task_03`) |
| `test_04` | `test_password_04` | Synthetic Test Account 04 | 110 pts | 3 tasks (`task_01`, `task_02`, `task_03`) |
| `test_05` | `test_password_05` | Synthetic Test Account 05 | 95 pts | 3 tasks (`task_01`, `task_02`, `task_03`) |
| `test_06` | `test_password_06` | Synthetic Test Account 06 | 60 pts | 3 tasks (`task_01`, `task_02`, `task_03`) |
| `test_07` | `test_password_07` | Synthetic Test Account 07 | 130 pts | 3 tasks (`task_01`, `task_02`, `task_03`) |
| `test_08` | `test_password_08` | Synthetic Test Account 08 | 40 pts | 3 tasks (`task_01`, `task_02`, `task_03`) |
| `test_09` | `test_password_09` | Synthetic Test Account 09 | 70 pts | 3 tasks (`task_01`, `task_02`, `task_03`) |
| `test_10` | `test_password_10` | Synthetic Test Account 10 | 150 pts | 3 tasks (`task_01`, `task_02`, `task_03`) |
| `test_11` | `test_password_11` | Synthetic Test Account 11 | 85 pts | 3 tasks (`task_01`, `task_02`, `task_03`) |
| `test_12` | `test_password_12` | Synthetic Test Account 12 | 100 pts | 3 tasks (`task_01`, `task_02`, `task_03`) |

> [!NOTE]
> All accounts are synthetic test fixtures for local testing. No real Microsoft emails, OAuth credentials, or cookies are stored or used.

---

## 3. Account Isolation & Concurrency Verification

### 3.1 Task State Isolation
- Each account maintains an isolated array of `SyntheticTask` objects in `store.user_tasks[username]`.
- Modifying `test_01` task state never impacts other accounts. Verified both at store level and via live API requests:
  - `test_01` reviews `task_01`: `test_01` status becomes `reviewed=True`, points: `120 -> 130`.
  - `test_02` through `test_12` remain `reviewed=False`, points unchanged.

### 3.2 Cross-Account Read/Write Protection
- `GET /tasks/{task_id}` queries `store.get_task_for_user(current_user.username, task_id)`. If `task_id` belongs to another user's scope or does not exist for `current_user`, HTTP 404 is returned.
- `POST /tasks/{task_id}/review` evaluates strictly against `current_user.username`. An attacker cannot review or mutate another user's tasks. Verified via `test_cross_account_write_protection`.

### 3.3 Concurrency & Idempotency
- `RewardLabStore._lock` (`threading.Lock`) guards state mutations in `mark_task_reviewed`.
- Verified via `test_concurrent_review_requests_idempotent`: 10 simultaneous threads attempting to review `task_01` at the exact same millisecond result in:
  - Exactly 1 thread awarding +10 fake points.
  - 9 threads returning idempotent response with 0 points awarded.
  - Final user point balance is guaranteed to be `initial + 10` (never multiplied).

---

## 4. Test Suite Execution Evidence

### 4.1 RewardLab Suite (20 Tests Passing)
- **Command:** `uv run pytest -v` (in `rewardlab/`)
- **Result:** `20 passed in 0.30s` (Ruff: 0 errors)

| Test Name | Verified Contract | Result |
|---|---|---|
| `test_12_synthetic_accounts_exist_and_unique` | Verifies 12 accounts, unique usernames, passwords, display names, and tasks | `PASSED` |
| `test_cross_account_task_isolation_on_review` | Reviewing task on `test_01` leaves `test_02`..`test_12` completely untouched | `PASSED` |
| `test_cross_account_task_isolation_via_api` | Inspects task status via API (`test_01` reviewed, `test_02` and `test_03` unreviewed) | `PASSED` |
| `test_cross_account_read_protection` | Account A cannot view private tasks of Account B (returns 404) | `PASSED` |
| `test_cross_account_write_protection` | Account A cannot review tasks of Account B (returns 404) | `PASSED` |
| `test_concurrent_review_requests_idempotent` | 10 concurrent threads review task atomically; points awarded at most once | `PASSED` |
| `test_deterministic_reset` | `store.reset()` restores all 12 accounts and tasks to clean initial baseline | `PASSED` |
| `test_all_12_accounts_can_login_and_access_dashboard` | All 12 accounts can authenticate, acquire session cookie, and load dashboard | `PASSED` |
| *(M1.1 UI Tests - 12 tests)* | Full coverage of health, unauth redirects, login, dashboard, tasks, details, review, 404, logout | `PASSED` |

### 4.2 Backend Regression Suite (29 Tests Passing)
- **Command:** `uv run pytest -v` (in `backend/`)
- **Result:** `29 passed in 0.07s` (Ruff: 0 errors)
- **Total Workspace Tests:** **49/49 PASSED**

---

## 5. Live Acceptance Demo Evidence (PowerShell Execution)

Executed against live RewardLab service (`http://127.0.0.1:8001`):

```powershell
# 1. Baseline API state check
BEFORE_TEST_01: reviewed=False, points=120
BEFORE_TEST_02: reviewed=False, points=50

# 2. Authenticate test_01 via form login
LOGIN_TEST_01: status=303, location=/dashboard

# 3. Complete fake task_01
REVIEW_ACTION_STATUS: 200

# 4. Verify in API (Official Demo Acceptance Criteria)
AFTER_TEST_01 (UPDATED):   reviewed=True,  points=130
AFTER_TEST_02 (UNCHANGED): reviewed=False, points=50
AFTER_TEST_03 (UNCHANGED): reviewed=False, points=80
```

---

## 6. Acceptance Criteria Mapping (M1.2)

| Acceptance Criterion | Milestone Definition | Observed Evidence | Verdict |
|---|---|---|---|
| **12 fake test accounts** | Seed 12 fake test accounts | Seeded `test_01` to `test_12` with unique credentials and labels | **PASS** |
| **Independent task/point state** | Independent task/point state | `test_01` review leaves `test_02`..`test_12` untouched | **PASS** |
| **No real Microsoft credentials** | Synthetic fixtures only, no real credentials | Only synthetic fixtures, prominent warning banner | **PASS** |
| **Task state isolation** | Two test accounts cannot read/change each other's task state | Verified via unit tests and live PowerShell calls | **PASS** |
| **Repeat-safe review** | Marking a test task twice has one side effect | Idempotent review, concurrent thread test passed | **PASS** |
| **Deterministic reproduction** | Fault scenarios can be reproduced deterministically | `store.reset()` cleans state before each test run | **PASS** |
| **Demo** | `login test_01; complete fake task; verify in API; test_02 remains unchanged` | Demonstrated live: `test_01` updated to 130 pts, `test_02` unchanged at 50 pts | **PASS** |
| **Zero M0/M1.1 regression** | Maintain full test suite | 49/49 tests pass across workspace | **PASS** |

---

## 7. Handoff Decision

**`Milestone M1.2 is complete and accepted.`**
The project is ready to proceed to task **M1.3** (`Add server-side task-status API and idempotent completion endpoint`).
