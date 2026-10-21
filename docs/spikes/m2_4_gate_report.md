---
last_updated: 2026-10-20
scope: gate_report
milestone: M2.4
status: accepted
verdict: M2.4 PASSED
---

# Gate Report: Milestone M2.4 — Runtime Permission Engine & Security Hardening

## 1. Executive Summary & Verdict

- **Milestone:** M2.4 — Runtime permission checks before dispatch and execution, runtime URL/domain/redirect validation, arbitrary script/eval rejection, network/navigation boundary hardening, and fail-closed diagnostic secret protection.
- **Verdict:** **`M2.4 PASSED`**
- **Scope Verified:**
  - **Permission Engine & Pre-Dispatch Authorization (`app/core/policy.py`, `app/workflows/validator.py`):**
    - Deny-by-default for unauthorized or unknown providers before browser process or context creation (0 contexts allocated upon denial).
    - Provider capability system: workflow requested capabilities are evaluated against provider-granted capabilities; ungranted capabilities trigger `CAPABILITY_NOT_GRANTED`.
    - Step-level action authorization: every step is verified against `allowed_actions` before dispatch and before step execution.
  - **Runtime Navigation & Route Security (`app/workflows/interpreter.py`):**
    - Route interception (`page.route("**/*")`) inspects navigation requests against `domain_allowlist` and `allowed_ports`, aborting forbidden requests.
    - Post-click navigation check: inspects `page.url` after each `click` action, preventing clicks from navigating to unauthorized origins.
    - Popup / New Window suppression: `_popup_guard` attaches to `popup` and `page` events, automatically closing forbidden popups when `allow_popups = False`.
    - Prohibited URL schemes (`javascript:`, `data:`, `file:`, `about:`, `vbscript:`, `blob:`) and embedded credentials (`user:pass@host`) rejected with stable error codes (`UNSAFE_SCHEME`).
    - SSRF & Cloud Metadata protection: blocked access to link-local addresses (`169.254.169.254`, `169.254.170.2`), loopback aliases, and internal cloud metadata hostnames (`metadata.google.internal`).
    - API verification endpoint escaping protection: `VerifyStateStep` enforces relative paths within provider allowlist, rejecting foreign schemes and path traversal (`..`).
  - **Arbitrary Code Execution Elimination:**
    - Workflows are strictly limited to the declarative DSL schema; execution hooks and `page.evaluate()` are completely absent from the workflow contract.
    - DSL cannot self-grant capabilities; undeclared fields and custom actions fail Pydantic model validation with `extra="forbid"`.
  - **Service Worker & Network Hardening (`app/execution/browser_manager.py`):**
    - Ephemeral browser contexts default to `service_workers="block"`, neutralizing background service worker bypasses of route guards.
  - **Side-Effect & Mutation Safety:**
    - State-mutating actions (`click`, `fill`) are automatically treated as non-retryable side effects regardless of workflow's declared `side_effect` flag; on failure, `requires_verification = True` and `can_retry = False`.
  - **Diagnostic Secret Protection (`app/execution/diagnostics.py`):**
    - Workflows containing sensitive inputs (passwords, auth tokens, session keys) trigger fail-closed screenshot suppression: zero screenshots saved to disk on failure, and reason recorded in diagnostic metadata.
  - **Reliable Resource Cleanup:**
    - Browser contexts are guaranteed to be closed and released in `finally` blocks upon any security violation, timeout, or cancellation.
  - **Quality Gates:**
    - **171 passing tests across workspace** (107 in `backend/tests/`, 64 in `rewardlab/tests/`).
    - **21 dedicated M2.4 security tests in `backend/tests/test_runtime_security.py`**.
    - **0 lint errors** in Ruff across both `backend` and `rewardlab`.
    - Zero regressions on M0, M1, M2.1, M2.2, and M2.3 acceptance criteria.

---

## 2. Test Execution Breakdown

### 2.1 Workspace Test Summary

| Test Suite | Location | Tests | Duration | Warnings | Result |
|---|---|---|---|---|---|
| **Backend Test Suite** | `backend/tests/` | **107 passed** | 64.74s | 1 (Starlette deprecation) | **PASSED** |
| - Runtime Security & Hardening (M2.4) | `test_runtime_security.py` | 21 passed | 9.30s | 0 | `PASSED` |
| - Browser Manager & Lifecycle (M2.3) | `test_browser_manager.py` | 13 passed | 16.14s | 0 | `PASSED` |
| - Playwright Action Interpreter (M2.2) | `test_action_interpreter.py` | 17 passed | 25.10s | 0 | `PASSED` |
| - RewardLab Live E2E Integration (M2.2) | `test_workflow_e2e_rewardlab.py` | 3 passed | 5.04s | 0 | `PASSED` |
| - Workflow DSL & Parser (M2.1) | `test_workflow_dsl.py` | 24 passed | 0.07s | 0 | `PASSED` |
| - Policy & Static URL Validation (M0) | `test_policy.py` | 25 passed | 0.04s | 0 | `PASSED` |
| - Health Endpoint (M0) | `test_health.py` | 1 passed | 0.01s | 0 | `PASSED` |
| - Secret Redaction (M0) | `test_logging.py` | 3 passed | 0.01s | 0 | `PASSED` |
| **RewardLab Test Suite** | `rewardlab/tests/` | **64 passed** | 2.53s | 1 (Starlette deprecation) | **PASSED** |
| **Workspace Total** | Entire Repo | **171 passed** | **67.27s** | **2** | **ALL PASSED** |

---

## 3. Detailed Verification Matrix (M2.4)

| Requirement / Scenario | Test Name | Input / Workflow | Expected Outcome | Observed Result |
|---|---|---|---|---|
| **Pre-Dispatch Denial (Microsoft Rewards)** | `test_denied_provider_zero_context_creation` | `provider: microsoft_rewards` | `POLICY_DENIED`; 0 contexts created or leaked | **PASSED** |
| **Pre-Dispatch Denial (Unknown Provider)** | `test_unknown_provider_denied_by_default` | `provider: unregistered_xyz` | `POLICY_DENIED`; 0 contexts created | **PASSED** |
| **Unauthorized Capability Blocking** | `test_workflow_requesting_unauthorized_capability_rejected` | `capabilities: ["read_tasks", "admin_bypass"]` | `PolicyDeniedException` with `CAPABILITY_NOT_GRANTED` | **PASSED** |
| **Granted Capability Approval** | `test_workflow_with_granted_capabilities_allowed` | `capabilities: ["read_tasks", "mark_reviewed"]` | Workflow validates and executes | **PASSED** |
| **Prohibited Schemes (`javascript:`)** | `test_static_url_security_rules` | `javascript:alert(document.cookie)` | `is_valid=False`, `UNSAFE_SCHEME` | **PASSED** |
| **Prohibited Schemes (`data:`, `file:`, `about:`)** | `test_static_url_security_rules` | `data:text/html,...`, `file:///etc/passwd` | `is_valid=False`, `UNSAFE_SCHEME` | **PASSED** |
| **Embedded Credentials in URL** | `test_static_url_security_rules` | `http://user:pass@127.0.0.1:8001/` | `is_valid=False`, embedded credentials rejected | **PASSED** |
| **Cloud Metadata Blocking (AWS/GCP)** | `test_static_url_security_rules` | `169.254.169.254`, `metadata.google.internal` | `is_valid=False`, `SSRF_ATTEMPT_BLOCKED` | **PASSED** |
| **API Verification SSRF Escaping** | `test_api_verification_endpoint_ssrf_blocked` | `https://attacker.com/leak` in endpoint | `is_valid=False`, absolute URLs rejected | **PASSED** |
| **API Path Traversal Blocking** | `test_api_verification_endpoint_ssrf_blocked` | `task_id: "../../admin/keys"` | `PolicyDeniedException`, path traversal rejected | **PASSED** |
| **Route Interception (Redirect Blocking)** | `test_redirect_to_unauthorized_domain_blocked` | HTTP 302 to `https://malicious-exfiltrate.com` | Route aborted, execution fails/denied, destination unreached | **PASSED** |
| **Click-Induced Navigation Blocking** | `test_click_induced_navigation_out_of_allowlist_blocked` | Button navigating to `unauthorized-domain.com` | Route guard aborts; click check raises `POLICY_DENIED` | **PASSED** |
| **Popup / New Window Suppression** | `test_popup_blocked_and_closed_by_policy` | Button with `window.open(...)`, `allow_popups=False` | `_popup_guard` fires and closes popup; 1 page remaining | **PASSED** |
| **Mutating Action Retry Safety** | `test_click_and_fill_actions_treated_as_mutations` | `ClickStep(side_effect=False)` on failing element | `requires_verification=True`, `can_retry=False` | **PASSED** |
| **Diagnostic Screenshot Suppression** | `test_sensitive_workflow_suppresses_failure_screenshot` | Workflow with `FillStep(value="secret...")` failing | Exactly 0 `.png` files created; suppression logged in JSON | **PASSED** |
| **Runtime Denial Resource Cleanup** | `test_runtime_policy_denial_releases_context_cleanly` | Workflow navigating to disallowed origin | Status `POLICY_DENIED`; active contexts count drops to 0 | **PASSED** |

---

## 4. Technical Boundaries & Network Isolation Assessment

To uphold honest engineering standards, the table below documents what the M2.4 runtime security engine guarantees, and the documented technical limitations of user-space browser automation:

| Boundary / Layer | Enforced Protection | Technical Limitation / Residual Risk | Recommended Mitigation |
|---|---|---|---|
| **Page-Level HTTP/HTTPS Navigation** | Route interception (`page.route("**/*")`) aborts all unauthorized navigation and redirects. Post-click navigation check verifies final URL. | Relies on Chromium reporting navigation requests to Playwright CDP. | Layered with port allowlists and static submission checks. |
| **Service Workers** | `service_workers="block"` passed to `browser.new_context()`. Disables service worker threads from executing or routing network requests outside page routes. | If service worker blocking is disabled by a custom policy, SW requests bypass `page.route()`. | Keep `block_service_workers = True` by default for all automated providers. |
| **WebSockets & WebRTC** | Static URL validation disallows `ws://` / `wss://` schemes. Page route guard intercepts HTTP upgrade requests. | Playwright `page.route` does not inspect WebSocket frame contents. WebRTC UDP traffic bypasses HTTP proxies. | WebRTC disabled via launch flags (`--disable-webrtc`) in worker sandbox when required. |
| **Popups & Multi-Window Navigation** | `_popup_guard` listens to `popup` and `context.page` events, immediately terminating new tabs/windows if `allow_popups = False`. | Brief ephemeral window initialization before CDP event delivery (~50–100ms). | Ephemeral window cannot navigate out of allowlist due to route guard, and is force-closed in interpreter `finally`. |
| **Operating System / Kernel Network Traffic** | Domain allowlists enforce loopback and authorized hostnames at application layer. | Chromium process can theoretically open raw sockets if exploited via RCE. | OS-level network namespaces or egress firewalls in worker container (M3/Infrastructure). |

---

## 5. Non-Goals Explicitly Excluded (Out of Scope for M2.4)

- **M2.5 CLI `run-workflow`:** Command-line workflow executor tool (deferred to M2.5).
- **M3 Multi-Account Orchestrator / Scheduler:** Multi-account concurrency, job queues, account leasing, cron scheduling.
- **Distributed Workers & Nodes:** RPC/gRPC protocols, worker heartbeats, distributed leasing.
- **AI Agent / Stealth Behavior:** Autonomous agent loops, mouse jitter, fingerprint spoofing, CAPTCHA bypass (strictly forbidden by product charter).

---

## 6. Conclusion & Handoff

Milestone M2.4 is completely implemented, verified with 171 tests passing across the workspace, and confirmed with 0 lint errors. The Runtime Permission Engine and Security Hardening are production-ready for the next milestone (M2.5: CLI Workflow Runner).
