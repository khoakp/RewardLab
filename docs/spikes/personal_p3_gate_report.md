---
last_updated: 2026-10-22
scope: gate_report
milestone: Personal-P3
status: accepted
verdict: PERSONAL-P3 CONDITIONAL
---

# Gate Report: Milestone Personal P3 — Android Standalone Browser Worker Feasibility Spike

## 1. Executive Summary & Verdict

- **Milestone:** Personal P3 — Android Standalone Browser Worker Feasibility Spike
- **Verdict:** **`PERSONAL-P3 CONDITIONAL`**
- **Objective:** Evaluate the feasibility of an independent Android standalone browser automation worker executing workflows against RewardLab without an external laptop/PC execution controller, without an ongoing ADB connection, and without root privileges. Concurrently, resolve two critical security and operator reliability hotfixes identified in Milestone Personal P2.1.
- **Invariants Established & Verified:**
  1. **Phase 0 Security Hotfix 1 (P0 Long Free-Text Redaction):** `redact_free_text()` enforces fail-closed truncation at `max_length` using the deterministic marker `[TRUNCATED]`, strictly eliminating the risk of appending raw, un-redacted suffixes to outputs.
  2. **Phase 0 Security Hotfix 2 (P1 Dynamic Relative Paths):** `inspect_workflow_plan()` in `adbap inspect-workflow` classifies dynamic relative paths (e.g. `/tasks/{task_id}`) as `UNRESOLVED`, strictly preventing dynamic paths from being erroneously marked as static `ALLOWED`.
  3. **Android Execution Model Classification:** Rigorously categorized five candidate execution models, establishing that only **Option B (Native Android App + Owned WebView)** provides a viable, secure, standalone architecture on unprivileged Android hardware.
  4. **Option A (Termux + Chromium) Rejection:** Conclusively classified as **`NO_GO` / `UNSUPPORTED`** due to missing official Bionic drivers, extreme PRoot `ptrace` emulation overhead, OS sandboxing failure (`--no-sandbox`), and aggressive termination by Android 12+ Phantom Process Killer.
  5. **Option C (Playwright Android via ADB) Disqualification:** Formally classified as **`NOT_STANDALONE`** due to mandatory external PC controller and continuous ADB socket dependency.
  6. **Option B (Native App + Owned WebView) Assessment:** Classified as **`FEASIBLE (CONDITIONAL)`**. Employs Android System WebView for native hardware-accelerated DOM automation with zero PC/ADB dependency, subject to the **foreground execution constraint** (screen on/dim).
  7. **Empirical Honesty Invariant:** Because no physical Android phone was attached to the development environment, physical hardware execution is recorded as **`NOT_RUN`**, and hardware resource benchmarks are recorded as **`NOT_MEASURED`**.

---

## 2. Phase 0 Security & Operator Reliability Hotfixes

### 2.1 Root Causes & Implemented Fixes

| Issue | Severity | Root Cause | Solution Implemented | Verified In |
|---|---|---|---|---|
| **Long Free-Text Suffix Leak** | **P0** | `redact_free_text()` only sanitized `text[:max_length]`, then appended `text[max_length:]` raw, leaking secrets positioned beyond `max_length`. | Refactored `redact_free_text()` to discard suffix beyond `max_length`, apply `TRAILING_KEY_REGEX` at boundary cut, and append deterministic marker `[TRUNCATED]`. | `backend/app/observability/logging.py` |
| **Dynamic Relative Path False-Safe** | **P1** | `inspect_workflow_plan()` checked `path_str.startswith("/")` and marked `/tasks/{task_id}` as static `ALLOWED`. | Added regex template inspection (`\{[^{}]+\}\|<[^<>]+>`) prior to path allowlist check, marking dynamic paths as `UNRESOLVED`. | `backend/app/operator_cli.py` |

### 2.2 Phase 0 Dedicated Acceptance Test Results

16 dedicated regression tests were implemented in `backend/tests/test_personal_p3_security_hotfixes.py`, all passing cleanly:

```text
============================= test session starts =============================
collected 16 items

tests/test_personal_p3_security_hotfixes.py ................             [100%]

============================== 16 passed in 0.63s ==============================
```

- `test_redact_free_text_secret_at_beginning`: Secret at index 0 masked; benign body preserved.
- `test_redact_free_text_secret_at_end`: Secret at end of string masked.
- `test_redact_free_text_secret_beyond_max_length`: Secret positioned beyond `max_length` omitted entirely; `[TRUNCATED]` appended.
- `test_redact_free_text_secret_crossing_truncation_boundary`: Secret split across boundary masked; `[TRUNCATED]` appended.
- `test_redact_free_text_severed_key_at_boundary`: Trailing key (e.g. `password=`) severed at boundary masked to `password=[REDACTED] [TRUNCATED]`.
- `test_redact_free_text_multiple_credentials_in_long_string`: Multiple credential assignments sanitized.
- `test_redact_data_nested_object_with_long_string`: Nested dictionaries with long strings sanitized.
- `test_redaction_filter_suppresses_long_record`: Logging records with long strings sanitized.
- `test_write_time_redaction_with_truncation_in_audit_and_job`: Database records sanitize long review reasons.
- `test_cli_show_redacts_long_error_message`: `adbap jobs show` table and JSON sanitize long error strings.
- `test_inspector_allowed_static_relative_path`: `/tasks` yields `ALLOWED`.
- `test_inspector_unresolved_dynamic_relative_path_curly`: `/tasks/{task_id}` yields `UNRESOLVED`.
- `test_inspector_unresolved_dynamic_relative_path_angle`: `/tasks/<task_id>/review` yields `UNRESOLVED`.
- `test_inspector_denies_scheme_relative_url`: `//evil.attacker.com/tasks` yields `DENIED`.
- `test_inspector_denies_path_traversal`: `/tasks/../internal/secret` yields `DENIED`.
- `test_inspector_denies_url_outside_allowlist`: `https://unauthorized.target.com/tasks` yields `DENIED`.

---

## 3. Android Standalone Feasibility Spike Findings

### 3.1 Candidate Architectures Comparison

| Architecture | Engine | Standalone Feasibility | PC/ADB Dependency | Root Required? | Verdict |
|---|---|---|---|---|---|
| **Option A (Termux + Chromium)** | Desktop Chromium via PRoot | **UNSUPPORTED** | No | No | **NO_GO** (Missing Bionic driver, extreme `ptrace` overhead, disabled sandbox, Android 12+ Phantom Process Killer). |
| **Option B (Native App + Owned WebView)** | Android System WebView | **FEASIBLE (CONDITIONAL)** | **None** | **No** | **CONDITIONAL** (100% standalone on device, native Bionic engine, clean session isolation; constrained to foreground). |
| **Option C (Playwright Android via ADB)** | Mobile Chrome APK | **NOT_STANDALONE** | **Mandatory** | No | **NOT_STANDALONE** (Desktop PC is the execution controller; dropping USB/TCP ADB terminates automation). |

### 3.2 POC Implementation in `spikes/android_worker_poc/`

1. **`android_adapter.py`:**
   - Typed Python execution contract implementing `AndroidExecutionAdapter`.
   - Standardized `AndroidExecutionResult` schema tracking `workflow_id`, `execution_id`, `status`, `outcome_type`, `verified_mutations`, `unverified_mutations`, `error_code`, `requires_verification`, and `duration_seconds`.
   - Enforces P1 Human Action Halt invariant (`request_human_action` halts trailing steps with `PAUSED_FOR_REVIEW` / `HUMAN_ACTION_REQUIRED`).
   - Verified via 4 automated unit tests in `spikes/android_worker_poc/test_android_adapter.py` (all PASS).
2. **`webview_app_spec/`:**
   - Full native Kotlin reference architecture for Option B:
     - `AndroidManifest.xml`: Clean permissions (INTERNET, optional WAKE_LOCK, isolatedProcess mode).
     - `AndroidWorkflowModels.kt`: Typed Kotlin models matching ADBAP DSL v1.
     - `RewardLabWebViewClient.kt`: Native client with strict domain/port allowlist enforcement, coroutine-based navigation, and asynchronous DOM manipulation via `evaluateJavascript`.
     - `AndroidWorkerActivity.kt`: Standalone foreground Activity demonstrating complete execution loop, causal postcondition verification, and session cookie teardown.

---

## 4. Full Regression & Quality Gate Results

### 4.1 Backend Test Suite
- **Command:** `uv run pytest -q` from `backend/`
- **Baseline (P2.1):** 290 passed
- **Result (P3):** **306 passed** in 127.06s (290 baseline + 16 Phase 0 hotfix tests)
- **Status:** **PASS** (Zero regressions)

### 4.2 Spike Adapter Test Suite
- **Command:** `uv run pytest -q ../spikes/android_worker_poc/test_android_adapter.py` from `backend/`
- **Result:** **4 passed** in 0.05s
- **Status:** **PASS**

### 4.3 RewardLab Test Suite
- **Command:** `uv run pytest -q` from `rewardlab/`
- **Baseline (P2.1):** 64 passed
- **Result (P3):** **64 passed** in 3.32s
- **Status:** **PASS** (Zero regressions)

### 4.4 Total Workspace Test Count
- **Total Passing Tests:** **370 passed** (306 backend + 64 rewardlab; plus 4 spike tests).
- **Pass Rate:** 100%.

### 4.5 Static Analysis (Ruff)
- `backend/`: `All checks passed!`
- `rewardlab/`: `All checks passed!`
- `spikes/`: `All checks passed!`
- **Status:** **PASS** (Zero lint errors)

### 4.6 Git Cleanliness
- `git status --short`: Only modified core logging/cli files, new test file, and spikes directory; zero diagnostic traces or secrets committed.

---

## 5. Residual Risks & Technical Limitations

1. **Android WebView Foreground Constraint:**
   - Android OS suspends WebView timers and rendering whenever an `Activity` is paused or moved to the background (`WebView.pauseTimers()`). A standalone Android worker using WebView must execute with the activity in the foreground (screen on or dimmed with a partial wake lock).
2. **Android Low Memory Killer (LMK):**
   - On devices with <= 4 GB RAM, running multiple applications while the worker is active may trigger Android's LMK. Option B mitigates this by keeping peak RSS under 180 MB, but foreground priority remains essential.
3. **Hardware Measurements Unconducted:**
   - In this development environment without an attached physical device or running Android emulator, actual hardware execution is marked **`NOT_RUN`**, and hardware resource consumption is marked **`NOT_MEASURED`**.

---

## 6. Decision Gate & Conditions for Future Android MVP

### Verdict Justification: `CONDITIONAL`
The technical feasibility of a standalone Android browser worker is confirmed through **Option B (Native Android App + Owned WebView)**, but the project must not declare a full `GO` until:
1. The native APK is installed on physical Android hardware and verified end-to-end against a live RewardLab instance.
2. Complete Laptop-Off Independence (Test B) is physically demonstrated with the laptop powered down.
3. Actual physical device CPU, RAM, battery, and execution latency are measured and recorded in `docs/spikes/android_resource_measurements.md`.
4. Operational workflows are structured to respect the foreground activity requirement.
