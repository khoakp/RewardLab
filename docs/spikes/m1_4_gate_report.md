---
last_updated: 2026-10-20
scope: gate_report
milestone: M1.4
status: accepted
verdict: M1.4 PASSED
---

# Gate Report: Milestone M1.4 — RewardLab Deterministic Fault Injection

## 1. Executive Summary & Verdict

- **Milestone:** M1.4 — Add deterministic fault injection: delayed render, transient 500, 429 + Retry-After, expired session, browser navigation fault and changed button label.
- **Verdict:** **`M1.4 PASSED`**
- **Scope Verified:**
  - Designed and implemented `FaultController` in `rewardlab/faults.py`:
    - Thread-safe control via `threading.RLock`.
    - Protected by `REWARDLAB_TEST_MODE` environment variable; completely disabled outside test mode.
    - Pure ASGI middleware (`FaultInjectionMiddleware`) in `rewardlab/app.py` preventing BaseHTTPMiddleware stream deadlocks.
    - Two trigger mechanisms: Request headers (`X-Test-Fault-*`) for per-request test injection and programmatic / test control endpoints (`/_test/faults/*`).
  - Implemented all six required fault injection scenarios:
    1. **Delayed HTML/DOM rendering:** Configurable seconds delay before response; exhausts after specified count.
    2. **Transient HTTP 500:** Simulated internal server error returning JSON or HTML; confirms zero side-effects on business state and safe idempotent retry.
    3. **HTTP 429 Rate Limiting with `Retry-After`:** Standard HTTP 429 status code with RFC-compliant `Retry-After: <seconds>` header.
    4. **Expired Authentication Session:** Session or account invalidated dynamically; returns HTTP 401 for API and HTTP 302 redirect to `/login` for UI without polluting other sessions.
    5. **Browser Navigation Failure:** Simulated 503 Service Unavailable / 502/504 errors on designated paths.
    6. **Changed Button Label / DOM Variant:** Dynamic UI label variation (`"Confirm Review"` vs `"Mark as reviewed"`) with preserved `data-testid="mark-reviewed-btn"` and intact review functionality.
  - Zero regression on existing features:
    - 40/40 RewardLab tests passed (`rewardlab/tests/`).
    - 29/29 Backend tests passed (`backend/tests/`).
    - **Total: 69 passing tests across workspace** (0 lint errors).

---

## 2. Fault Scenarios Specification

| Scenario | Trigger Header / Method | Expected Behavior | Scope / Isolation | Reset Mechanism | Acceptance Status |
|---|---|---|---|---|---|
| **1. Delayed Render** | `X-Test-Fault-Delay: <sec>` or `set_delay(path, sec, count)` | Middleware delays response by specified seconds | Scoped to path or request header | Count exhaustion or `reset()` | **PASS** |
| **2. Transient 500** | `X-Test-Fault-500: 1` or `set_transient_error(path, 500, count)` | Returns HTTP 500 (`INTERNAL_SERVER_ERROR`); zero side-effect on task/points | Scoped to path or request header | Count exhaustion or `reset()` | **PASS** |
| **3. HTTP 429 + Retry-After** | `X-Test-Fault-429: <sec>` or `set_rate_limit(path, retry_after, count)` | Returns HTTP 429 with `Retry-After: <sec>` header | Scoped to path or request header | Count exhaustion or `reset()` | **PASS** |
| **4. Expired Session** | `X-Test-Fault-Expire-Session: 1` or `expire_session(token)` | Invalidates session: API returns 401, Web UI redirects (302) to `/login` | Scoped to target session / username | Re-login or `reset()` | **PASS** |
| **5. Navigation Fault** | `X-Test-Fault-Navigation: <status>` or `set_navigation_fault(path, status, count)` | Returns HTTP 503/502/504 with error message | Scoped to path or request header | Count exhaustion or `reset()` | **PASS** |
| **6. Changed UI Label** | `X-Test-Fault-UI-Button: <label>` or `set_ui_variant("button_label", <val>)` | Renders button with alternate text and `aria-label`; preserves `data-testid` | Scoped to UI template rendering | `set_ui_variant(..., None)` or `reset()` | **PASS** |

---

## 3. Security & Test Mode Protection

- **Environment Gate:**
  - Controlled by `REWARDLAB_TEST_MODE`.
  - When disabled (`REWARDLAB_TEST_MODE=false`):
    - All fault headers (`X-Test-Fault-*`) are completely ignored.
    - All fault injection middleware logic is bypassed.
    - Control endpoints `GET /_test/faults/state`, `POST /_test/faults/configure`, `POST /_test/faults/reset` return **HTTP 403 Forbidden** (`code: "FORBIDDEN"`).
    - Inspection endpoints `GET /api/accounts/{username}` and `GET /api/accounts/{username}/tasks/{task_id}` return **HTTP 403 Forbidden**.
- **Side-Effect Safety Invariant:**
  - Injected transient errors on completion (`POST /api/tasks/{task_id}/complete`) abort execution before state mutation:
    - Task reviewed status remains `False`.
    - Account points balance remains unchanged.
    - Subsequent retry using the same `Idempotency-Key` completes cleanly, awarding points exactly once.

---

## 4. Test Suite Execution Evidence

### 4.1 RewardLab Test Suite (40 Tests Passing)
- **Command:** `uv run pytest -v` (in `rewardlab/`)
- **Result:** `40 passed, 1 warning in 0.88s` (Ruff: 0 errors)

| Test Module | Tests | Description | Result |
|---|---|---|---|
| `test_rewardlab_faults.py` | 11 tests | Delayed render, transient 500, 429 + Retry-After, expired session, navigation fault, changed UI label, test mode 403 protection | `PASSED` |
| `test_rewardlab_api.py` | 8 tests | Unauthenticated 401, Cookie/Bearer auth, 404s, Idempotency flow, cross-account isolation, 10-thread concurrency, Idempotency collision 409 | `PASSED` |
| `test_rewardlab_isolation.py` | 9 tests | 12 accounts uniqueness, review isolation, API inspection security (test mode 403), deterministic reset, login/dashboard | `PASSED` |
| `test_rewardlab_ui.py` | 12 tests | Full UI regression (login, dashboard, tasks, task details, mark reviewed, 404, logout) | `PASSED` |

### 4.2 Backend Test Suite (29 Tests Passing)
- **Command:** `uv run pytest -v` (in `backend/`)
- **Result:** `29 passed in 0.06s` (Ruff: 0 errors)
- **Total Passing Tests:** **69 / 69 PASSED** across workspace.

---

## 5. Live Demonstration Evidence

Executed via Python TestClient against the full RewardLab ASGI application:

```text
1. DELAY_FAULT: status=401, elapsed=0.174s
2. TRANSIENT_500_FAULT: status=500, code=INTERNAL_SERVER_ERROR
3. RATE_LIMIT_429_FAULT: status=429, retry_after=3, code=RATE_LIMITED
4. EXPIRED_SESSION_FAULT: status=401, code=SESSION_EXPIRED
5. NAVIGATION_FAULT: status=503, code=NAVIGATION_FAILED
6. UI_VARIANT_FAULT: status=200, has_confirm_review=True
VERIFICATION_COMPLETE: ALL 6 FAULTS VERIFIED
```

---

## 6. Acceptance Criteria Mapping (M1.4)

| Acceptance Criterion | Milestone Definition | Observed Evidence | Verdict |
|---|---|---|---|
| **Delayed HTML/DOM rendering** | Reproduce timeout / slow render | Implemented via header & controller; verified with elapsed time check | **PASS** |
| **Transient HTTP 500** | Simulated server error with safe retry | 500 error returned; 0 side effects; retry succeeds and awards points once | **PASS** |
| **HTTP 429 with Retry-After** | Rate limiting with backoff header | 429 returned with `Retry-After` header; subsequent call recovers | **PASS** |
| **Expired authentication session** | Session invalidation test | 401 / 302 returned for expired session; other accounts unaffected | **PASS** |
| **Browser navigation failure** | Page failure simulation | 503 error returned for designated page; recovers on retry | **PASS** |
| **Changed button label / DOM variant** | Dynamic locator test fixture | Renders alternate label with intact `data-testid` and working submission | **PASS** |
| **Test mode security** | No public fault exposure | 403 Forbidden returned when `REWARDLAB_TEST_MODE=false` | **PASS** |
| **Zero regression** | Maintain M0, M1.1, M1.2, M1.3 | 69/69 tests passing across workspace | **PASS** |

---

## 7. Known Limitations & Architecture Tradeoffs

1. **In-Memory Fault State:**
   - Active fault rules and expired sessions are maintained in process memory via `FaultController`.
   - Suitable for local development and single-worker test runners. Multi-node distributed workers (M4) will coordinate fault states through database/configuration fixtures.
2. **Deterministic Count Exhaustion:**
   - Rules with `count: 1` automatically self-destruct after one trigger. If an unexpected extra request occurs, the rule may expire early. Always pair with explicit `reset()` in test fixtures.

---

## 8. Handoff Decision

**`Milestone M1.4 is complete and accepted.`**
The project is ready to proceed to task **M1.5** (`Add integration tests for task isolation, idempotency and forced failures`) or **M2** (`Workflow DSL + Playwright execution`).
