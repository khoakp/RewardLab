---
last_updated: 2026-10-20
scope: gate_report
milestone: M2.5
status: accepted
verdict: M2.5 PASSED
---

# Gate Report: Milestone M2.5 — CLI Workflow Runner

## 1. Executive Summary & Verdict

- **Milestone:** M2.5 — Command-Line Interface (`run-workflow`) for validating and executing authorized YAML/JSON workflows against synthetic accounts on RewardLab with runtime security, context isolation, stable exit codes, sanitized diagnostics, and orchestration-ready JSON output.
- **Verdict:** **`M2.5 PASSED`**
- **Scope Verified:**
  - **CLI Command Interface (`app/cli.py`):**
    - Commands registered via `pyproject.toml` (`run-workflow`, `adbap run-workflow`, and `python -m app.cli`).
    - Accepts positional and flagged workflow paths (`-f`, `--file`).
    - Allows specifying synthetic account (`-a`, `--account`, default: `test_01`).
    - Optional provider assertion (`-p`, `--provider`) ensuring workflow provider matches operator expectations.
    - Validate-only / Dry-run mode (`--validate-only`, `--dry-run`) performs full syntax, schema, and policy validation with **zero** browser allocations and zero side effects.
    - Machine-readable JSON output mode (`--json`) emitting structured output for integration with automated runners and schedulers.
    - Deterministic process exit codes:
      - `0`: `SUCCESS`
      - `1`: `FAILED`
      - `2`: `POLICY_DENIED`
      - `3`: `TIMEOUT`
      - `4`: `CANCELLED`
      - `5`: `USAGE_OR_SCHEMA_ERROR`
    - Security-first credential handling: no passwords in CLI arguments; auto-authentication via environment variables (`REWARDLAB_PASSWORD` or `REWARDLAB_SESSION_ID`) or explicit `--session-id`.
    - No unsafe bypass flags (`--ignore-policy` and `--unsafe-eval` are strictly omitted).
    - Comprehensive secret redaction across terminal stdout and stderr for passwords, tokens, cookies, and embedded URL userinfo credentials (`http://[REDACTED]:[REDACTED]@host`).
  - **Engine Reuse & Execution Guarantees:**
    - Reuses Strict Workflow DSL v1, Safe YAML/JSON Parser, ProviderPolicy Evaluator, BrowserManager, and Playwright Async Action Interpreter without engine reimplementation.
    - Pre-dispatch policy evaluation guarantees zero Chromium launch when workflows target prohibited providers (Microsoft Rewards) or request unapproved capabilities.
    - Every run generates a unique execution ID (`UUIDv4`).
    - Step-level granularity: tracks individual execution status, duration, and sanitized error messages.
    - Resource cleanup guaranteed in `finally` blocks across success, step failure, context timeout, and cancellation (SIGINT/Ctrl+C).
    - Mutating actions (`click`, `fill`) enforce side-effect safety (`requires_verification=True`, `can_retry=False`).
  - **Quality Gates:**
    - **198 passing tests across workspace** (134 in `backend/tests/`, 64 in `rewardlab/tests/`).
    - **27 dedicated M2.5 tests in `backend/tests/test_cli.py`**.
    - **0 lint errors** in Ruff across both `backend` and `rewardlab`.
    - Zero regressions across M0, M1, M2.1, M2.2, M2.3, and M2.4.

---

## 2. Test Execution Breakdown

### 2.1 Workspace Test Summary

| Test Suite | Location | Tests | Duration | Warnings | Result |
|---|---|---|---|---|---|
| **Backend Test Suite** | `backend/tests/` | **134 passed** | 70.07s | 1 (Starlette deprecation) | **PASSED** |
| - CLI Workflow Runner (M2.5) | `test_cli.py` | 27 passed | 6.68s | 0 | `PASSED` |
| - Runtime Security & Hardening (M2.4) | `test_runtime_security.py` | 21 passed | 9.30s | 0 | `PASSED` |
| - Browser Manager & Lifecycle (M2.3) | `test_browser_manager.py` | 13 passed | 16.14s | 0 | `PASSED` |
| - Playwright Action Interpreter (M2.2) | `test_action_interpreter.py` | 17 passed | 25.10s | 0 | `PASSED` |
| - RewardLab Live E2E Integration (M2.2) | `test_workflow_e2e_rewardlab.py` | 3 passed | 5.04s | 0 | `PASSED` |
| - Workflow DSL & Parser (M2.1) | `test_workflow_dsl.py` | 24 passed | 0.07s | 0 | `PASSED` |
| - Policy & Static URL Validation (M0) | `test_policy.py` | 25 passed | 0.04s | 0 | `PASSED` |
| - Health Endpoint (M0) | `test_health.py` | 1 passed | 0.01s | 0 | `PASSED` |
| - Secret Redaction (M0) | `test_logging.py` | 3 passed | 0.01s | 0 | `PASSED` |
| **RewardLab Test Suite** | `rewardlab/tests/` | **64 passed** | 1.89s | 1 (Starlette deprecation) | **PASSED** |
| **Workspace Total** | Entire Repo | **198 passed** | **71.96s** | **2** | **ALL PASSED** |

---

## 3. Detailed Verification Matrix (M2.5)

| Requirement / Scenario | Test Name | Input / Workflow | Expected Outcome | Observed Result |
|---|---|---|---|---|
| **CLI Help Output** | `test_cli_help` | `--help` | Exit code `0`; synopsis, options, and security notes printed | **PASSED** |
| **Short Help Flag** | `test_cli_short_help` | `-h` | Exit code `0`; synopsis printed | **PASSED** |
| **Missing Workflow File** | `test_cli_argument_missing_workflow` | No arguments | Exit code `5` (`USAGE_OR_SCHEMA_ERROR`); sanitized error | **PASSED** |
| **Unrecognized CLI Flag** | `test_cli_argument_unknown_flag` | `--nonexistent-flag-xyz` | Exit code `5`; usage error printed | **PASSED** |
| **Path Resolution Combinations** | `test_cli_path_resolution` | `-f`, positional, `run-workflow <file>` | Correct `Path` resolved across all syntaxes | **PASSED** |
| **Non-Existent File Path** | `test_cli_nonexistent_workflow_path` | `-f /missing/wf.yaml` | Exit code `5`; file not found message | **PASSED** |
| **Non-Existent File Path (JSON)** | `test_cli_nonexistent_workflow_path_json` | `-f /missing/wf.yaml --json` | Exit code `5`; valid JSON with `error: FILE_NOT_FOUND` | **PASSED** |
| **Malformed YAML Syntax** | `test_cli_malformed_yaml` | Broken YAML indentation | Exit code `5`; syntax error reported | **PASSED** |
| **Malformed YAML Syntax (JSON)** | `test_cli_malformed_yaml_json` | Broken YAML indentation `--json`| Exit code `5`; valid JSON with `error: SYNTAX_ERROR` | **PASSED** |
| **Unsupported Schema Version** | `test_cli_unsupported_schema_version` | `schema_version: 99` | Exit code `5`; validation error | **PASSED** |
| **Provider Mismatch** | `test_cli_provider_mismatch` | `--provider other_provider` | Exit code `5`; provider mismatch error | **PASSED** |
| **Pre-Dispatch Denial (MS Rewards)**| `test_cli_denied_provider_microsoft_rewards` | `provider: microsoft_rewards` | Exit code `2` (`POLICY_DENIED`); 0 browser launched | **PASSED** |
| **Pre-Dispatch Denial (Unknown)** | `test_cli_denied_provider_unknown` | `provider: malicious_site_xyz` | Exit code `2`; 0 browser launched | **PASSED** |
| **Pre-Dispatch Denial (JSON)** | `test_cli_denied_provider_json_output` | `provider: microsoft_rewards --json` | Exit code `2`; valid JSON with `error: POLICY_DENIED` | **PASSED** |
| **Unauthorized Capability** | `test_cli_unauthorized_capability_rejected` | `capabilities: ["unapproved_xyz"]` | Exit code `2`; unapproved capability rejected | **PASSED** |
| **Validate-Only (Dry-Run)** | `test_cli_validate_only_valid_workflow` | Valid YAML `--validate-only` | Exit code `0`; "VALID" printed; 0 browser contexts | **PASSED** |
| **Dry-Run Alias** | `test_cli_dry_run_alias` | Valid YAML `--dry-run` | Exit code `0`; behaves identically to `--validate-only` | **PASSED** |
| **Validate-Only JSON Mode** | `test_cli_validate_only_json` | Valid YAML `--validate-only --json` | Exit code `0`; valid JSON with `valid: true` | **PASSED** |
| **Validate-Only Rejects Denied** | `test_cli_validate_only_rejects_denied_provider` | Denied provider `--validate-only` | Exit code `2`; policy evaluated and denied | **PASSED** |
| **Execution Timeout Handling** | `test_cli_timeout_handling` | `--timeout 0.001` | Exit code `3` (`TIMEOUT`); browser stopped cleanly | **PASSED** |
| **Execution Cancellation** | `test_cli_cancellation_handling` | `asyncio.CancelledError` | Exit code `4` (`CANCELLED`); browser stopped cleanly | **PASSED** |
| **Secret Redaction in Output** | `test_cli_secret_redaction_in_output` | URL with embedded credentials | Credentials masked (`[REDACTED]`) in stdout/stderr | **PASSED** |
| **Live Chromium E2E (Human Mode)**| `test_cli_live_e2e_rewardlab_execution` | Live RewardLab server, `test_01` | Exit code `0`; all 4 steps succeeded; points +10; `test_02` isolated | **PASSED** |
| **Live Chromium E2E (JSON Mode)** | `test_cli_live_e2e_rewardlab_json_output` | Live RewardLab server, `test_02` `--json` | Exit code `0`; valid JSON; points +10; duration recorded | **PASSED** |
| **Subprocess CLI Help** | `test_cli_subprocess_help` | `python -m app.cli --help` | Process exit code `0`; stdout contains help | **PASSED** |
| **Subprocess Validate-Only** | `test_cli_subprocess_validate_only`| `python -m app.cli ... --validate-only` | Process exit code `0`; valid JSON | **PASSED** |
| **Subprocess Denied Provider** | `test_cli_subprocess_denied_provider` | `python -m app.cli ... rewards.yaml` | Process exit code `2`; stderr contains policy denial | **PASSED** |

---

## 4. Security Review & Boundary Analysis

| Security Boundary | Enforced CLI Behavior | Known Technical Limits | Mitigation / Charter Guard |
|---|---|---|---|
| **Pre-Dispatch Evaluation** | Evaluates policy and capability allowlists before any browser process or context creation. | None at CLI level. If policy denies, 0 Chromium processes or CDP sessions exist. | Enforced by `validate_workflow_definition` before `BrowserManager.start()`. |
| **Credential Exposure** | No CLI password flags. Auto-authentication from environment variables. Stdio redaction filter masks tokens and URL credentials. | Process environment variables can be inspected by local root/admin users. | Recommended for operators to use session tokens or secure secrets manager (M3/Infrastructure). |
| **Unsafe Bypass Prevention** | No `--ignore-policy`, `--unsafe-eval`, or `--no-verify` flags exist. | Operator cannot disable security rules via CLI flags. | Architectural invariant: security cannot be turned off via flags. |
| **Process Lifecycle Cleanup** | Browser and all active contexts are terminated in `finally` blocks upon exit, crash, timeout, or SIGINT. | Kill -9 (SIGKILL) terminates process immediately without running Python signal handlers. | Contexts are ephemeral and do not persist on disk unless authorized auth storage is enabled. |
| **Browser Network Boundary** | Reuses M2.4 route guards, popup auto-close, and post-click redirect verification. | User-space route guard cannot inspect raw TCP/UDP or bypass OS network stack. | Container network isolation or namespace sandbox for multi-tenant worker environments. |
| **Anti-Bot / Evasion Exclusions**| Zero stealth plugins, zero fingerprint spoofing, zero CAPTCHA solvers, and zero Microsoft Rewards earning. | None. Invariant strictly preserved. | Canonical product constraint: platform is for owned/authorized targets only. |

---

## 5. Explicitly Unverified Conditions (Out of Scope for M2.5)

To uphold transparent engineering standards, the following aspects remain unverified and are scheduled for future milestones:
1. **Multi-Account Local Orchestration (Milestone M3):** Concurrency limits across 12 synthetic accounts, transactional database leases, persistent job state machine, worker crash recovery reconciliation.
2. **Distributed Worker Nodes (Milestone M4):** Outbound pull agents, network partition recovery, PostgreSQL lease fencing, multi-host heartbeats.
3. **Android Platform Feasibility (Milestone M8):** Android Termux agent, device thermals, screen-off power management, Android Chromium constraints.
4. **External Providers:** Only `rewardlab` is authorized for automated browser execution; external third-party providers remain unauthorized.

---

## 6. Conclusion & Handoff

Milestone M2.5 is completely implemented, verified with **198 passing tests across the workspace** (134 in `backend/tests/`, 64 in `rewardlab/tests/`), and confirmed with **0 lint errors** in Ruff. The CLI Workflow Runner satisfies all acceptance criteria, and Release R0 (M0 + M1 + M2) is fully achieved and ready for Milestone M3 (Multi-account local orchestration).
