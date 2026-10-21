---
last_updated: 2026-10-20
scope: gate_report
milestone: M2.1
status: accepted
verdict: M2.1 PASSED
---

# Gate Report: Milestone M2.1 — Strict Versioned Workflow DSL

## 1. Executive Summary & Verdict

- **Milestone:** M2.1 — Create strict versioned workflow schema, parser, schema migration strategy and validation errors; ship safe sample YAML.
- **Verdict:** **`M2.1 PASSED`**
- **Scope Verified:**
  - **Pydantic v2 Strict DSL Schema (`app/workflows/schema.py`):**
    - Enforced `extra="forbid"` on all models to reject unknown fields and ambient script parameters.
    - Defined discriminated unions for all Playwright automation actions (`action` discriminator: `navigate`, `click`, `fill`, `wait_visible`, `assert_visible`, `extract_text`, `verify_state`, `save_result`, `notify_user`, `request_human_action`).
    - Defined discriminated unions for locator targets (`by` discriminator: `role`, `test_id`, `text`, `css`, `xpath`), strictly rejecting empty or script-bearing values (`javascript:`).
    - Enforced uniqueness of step IDs across the entire workflow (`validate_unique_step_ids`).
    - Enforced execution bounds (`timeout_seconds`: 1..300, `max_retries`: 0..5, `step_timeout_seconds`: 1..60, `steps`: 1..50).
  - **Schema Versioning & Unsupported Version Policy:**
    - Established `schema_version: 1` (`SUPPORTED_SCHEMA_VERSIONS = {1}`).
    - Any unrecognized version raises `UnsupportedSchemaVersionError` with error code `UNSUPPORTED_SCHEMA_VERSION`.
    - Documented forward compatibility and immutability strategy in ADR 0001.
  - **Hardened Safe Parser (`app/workflows/parser.py`):**
    - Enforced max workflow size of 256 KB (`MAX_WORKFLOW_SIZE_BYTES = 256 * 1024`) via `WorkflowSizeExceededError`.
    - Implemented `UniqueKeySafeLoader` derived from `yaml.SafeLoader` to detect and strictly reject duplicate mapping keys in YAML.
    - Implemented `_json_duplicate_key_detector` using `object_pairs_hook` to detect and strictly reject duplicate keys in JSON.
    - Completely blocked arbitrary Python object deserialization and custom tags (`!python/object`, `!include`).
  - **ProviderPolicy Integration (`app/workflows/validator.py`):**
    - Integrated with `PolicyEvaluator` to reject unauthorized providers (`microsoft_rewards` rejected as companion-only; unknown providers rejected by default with HTTP 403 `POLICY_DENIED`).
    - Statically validated `navigate` URLs against `domain_allowlist`, blocking scheme abuses (`javascript:`, `file:`, `ftp:`), domain spoofing, and metadata addresses.
  - **Sample Workflow & ADR:**
    - Shipped canonical safe sample YAML at `examples/workflows/rewardlab_review_first_task.yaml`.
    - Documented architecture decisions in `docs/adr/0001-strict-workflow-dsl-v1.md`.
  - **Quality Gates:**
    - **117 passing tests across workspace** (53 in `backend/tests/`, 64 in `rewardlab/tests/`).
    - **0 lint errors** in Ruff across both backend and rewardlab.
    - Zero regressions on M0 and M1 acceptance criteria.

---

## 2. Test Execution Breakdown

### 2.1 Workspace Test Summary

| Test Suite | Location | Tests | Duration | Warnings | Result |
|---|---|---|---|---|---|
| **Backend Test Suite** | `backend/tests/` | **53 passed** | 0.14s | 1 (Starlette deprecation) | **PASSED** |
| - Workflow DSL & Parser | `test_workflow_dsl.py` | 24 passed | 0.08s | 0 | `PASSED` |
| - Policy & Static URL Validation | `test_policy.py` | 25 passed | 0.04s | 0 | `PASSED` |
| - Health Endpoint | `test_health.py` | 1 passed | 0.01s | 0 | `PASSED` |
| - Secret Redaction | `test_logging.py` | 3 passed | 0.01s | 0 | `PASSED` |
| **RewardLab Test Suite** | `rewardlab/tests/` | **64 passed** | 1.86s | 1 (Starlette deprecation) | **PASSED** |
| **Workspace Total** | Entire Repo | **117 passed** | **2.00s** | **2** | **ALL PASSED** |

---

## 3. Detailed Verification Matrix (M2.1)

| Requirement | Test Name / Verification | Input / Scenario | Expected Outcome | Observed Result |
|---|---|---|---|---|
| **Valid YAML Workflow** | `test_valid_yaml_workflow_parsing_and_validation` | Multi-step YAML with navigate, wait, extract, click, verify | 200 OK, parsed `WorkflowDefinition` | **PASSED** |
| **Valid JSON Workflow** | `test_valid_json_workflow_parsing_and_validation` | Multi-step JSON with navigate and assert_visible | 200 OK, parsed `WorkflowDefinition` | **PASSED** |
| **Shipped Example YAML** | `test_canonical_example_yaml_file_validates` | `examples/workflows/rewardlab_review_first_task.yaml` | Validates against schema & policy | **PASSED** |
| **Unsupported Version** | `test_unsupported_schema_version_rejected` | `schema_version: 99` | `UnsupportedSchemaVersionError` (422) | **PASSED** |
| **Missing Fields** | `test_missing_required_provider_rejected` | Missing `provider` | `WorkflowValidationError` (422) | **PASSED** |
| **Missing Steps** | `test_missing_steps_rejected` | `steps: []` | `WorkflowValidationError` (422) | **PASSED** |
| **Unknown Action** | `test_unknown_action_type_rejected` | `action: "execute_arbitrary_code"` | Discriminator failure (422) | **PASSED** |
| **Unknown Extra Fields** | `test_unknown_fields_strictly_forbidden` | `malicious_global_hook: "..."` | `extra_forbidden` (422) | **PASSED** |
| **Duplicate Step IDs** | `test_duplicate_step_ids_rejected` | Two steps with identical ID | `WorkflowValidationError` (422) | **PASSED** |
| **Invalid Locators** | `test_invalid_locators_rejected` | Empty `value`, missing `role`, unknown `by` | Pydantic validation failure (422) | **PASSED** |
| **Unsafe YAML Tags** | `test_unsafe_yaml_tags_rejected` | `!!python/object/apply:os.system` | `WorkflowSyntaxError` (422) | **PASSED** |
| **Duplicate YAML Keys** | `test_duplicate_yaml_keys_rejected` | `provider: rewardlab` duplicated | `WorkflowSyntaxError` (422) | **PASSED** |
| **Duplicate JSON Keys** | `test_duplicate_json_keys_rejected` | `"provider"` duplicated in JSON | `WorkflowSyntaxError` (422) | **PASSED** |
| **Oversized Workflow** | `test_oversized_workflow_rejected` | Content > 256 KB | `WorkflowSizeExceededError` (413) | **PASSED** |
| **Execution Bounds** | `test_execution_limits_boundary_validation` | `timeout_seconds: 301`, `max_retries: 10` | Boundary validation failure (422) | **PASSED** |
| **Microsoft Rewards Denial** | `test_policy_denial_for_microsoft_rewards` | `provider: microsoft_rewards` | `PolicyDeniedException` (403) | **PASSED** |
| **Unknown Provider Denial** | `test_policy_denial_for_unknown_provider` | `provider: unauthorized_external_bank` | `PolicyDeniedException` (403) | **PASSED** |
| **Disallowed Destination** | `test_policy_denial_for_disallowed_destination_url` | `url: https://attacker.com/malicious` | `PolicyDeniedException` (403) | **PASSED** |
| **Prohibited URL Schemes** | `test_policy_denial_for_prohibited_url_scheme` | `javascript:alert(1)`, `file:///etc/passwd` | Rejected (403 / 422) | **PASSED** |
| **API YAML Payload** | `test_api_validate_workflow_with_yaml_payload` | `POST /api/v1/workflows/validate` with YAML | HTTP 200 `valid: true` | **PASSED** |
| **API Duplicate Key** | `test_api_validate_workflow_rejects_duplicate_yaml_keys` | YAML with duplicate key to API | HTTP 422 `WORKFLOW_SYNTAX_ERROR` | **PASSED** |
| **API Unsupported Version** | `test_api_validate_workflow_unsupported_version_returns_422` | `schema_version: 42` to API | HTTP 422 `UNSUPPORTED_SCHEMA_VERSION` | **PASSED** |
| **API Policy Denial** | `test_api_validate_workflow_policy_denied_returns_403` | `microsoft_rewards` to API | HTTP 403 `POLICY_DENIED` | **PASSED** |

---

## 4. Architectural Analysis & Invariants

1. **Workflows are Immutable Data, Not Code:**
   - No Python/JavaScript expressions, no `eval()`, no external script injection.
   - Pydantic models with `extra="forbid"` eliminate prototype/field pollution.
2. **Deny-by-Default Policy Integration:**
   - Every parsed workflow passes through `PolicyEvaluator` before acceptance.
   - Disallowed providers, unapproved actions, or out-of-allowlist URLs cannot pass validation.
3. **Defense-in-Depth Parsing:**
   - 256 KB size limit prevents resource exhaustion.
   - Strict mapping uniqueness prevents ambiguous configuration where different parsers resolve duplicate keys differently.

---

## 5. Acceptance Criteria Mapping (M2.1)

| Implementation Plan Acceptance Criterion | Required Verification | Observed Evidence | Status |
|---|---|---|---|
| **Create strict versioned workflow schema** | Pydantic v2 models, discriminated union, versioning | Implemented in `schema.py`; verified via 24 tests | **PASSED** |
| **Safe parser with size and duplicate key checks** | Safe YAML and JSON parsing, duplicate rejection | Implemented in `parser.py`; verified via unit tests | **PASSED** |
| **Schema migration strategy & validation errors** | Unsupported version error, stable error codes, ADR | Documented in ADR 0001; verified via error tests | **PASSED** |
| **Ship safe sample YAML** | Working YAML file for RewardLab | Created at `examples/workflows/rewardlab_review_first_task.yaml` | **PASSED** |
| **Zero regression on M0 and M1** | Maintain 100% existing test pass rate | 29/29 M0 backend tests + 64/64 M1 RewardLab tests passing | **PASSED** |

---

## 6. Handoff Decision

**`Milestone M2.1 is complete, fully verified, and accepted.`**
The project is ready to proceed to **Milestone M2.2: Implement action interpreter for Playwright async** (`navigate`, `wait_visible`, `extract_text`, `click`, `fill`, `assert_visible`, `verify_state`).
