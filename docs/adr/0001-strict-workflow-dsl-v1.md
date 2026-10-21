---
last_updated: 2026-10-20
scope: architecture_decisions
status: accepted
---

# ADR 0001: Strict Versioned Workflow DSL v1

## Context

In ADBAP, browser automation scenarios must be executed reliably across heterogeneous environments (Windows, Linux, and experimental lightweight agents) against authorized providers like RewardLab. Previously, workflow validation was performed at a high level using an unversioned draft DTO that lacked schema constraints on locators, action parameters, step identity, timeout boundaries, and safe parsing guarantees.

Executing arbitrary code (Python, JavaScript, or shell scripts) within workflow files would create severe remote code execution (RCE) and privilege escalation risks. Furthermore, standard YAML parsers default to allowing arbitrary object constructors and silent key overwrites (duplicate keys), and lack input size limits, exposing the system to denial-of-service (Billion Laughs) and non-deterministic behavior.

## Alternatives Considered

1. **Python/TypeScript Code Workflows (Code as Config):**
   - *Pros:* Maximum flexibility, familiar programming constructs.
   - *Cons:* Extremely high security risk; requires sandboxing; impossible to statically validate actions against `ProviderPolicy` prior to execution; violates the core project boundary: *"Workflow definitions are data, not code"*.
2. **Generic Unconstrained JSON Schema:**
   - *Pros:* Simple to write.
   - *Cons:* Lacks typed discriminating unions for actions; does not enforce locator semantics or Playwright interpreter contracts; permits ambient field pollution.
3. **Pydantic v2 Strict Discriminated Union with Safe Custom YAML/JSON Parser (Chosen):**
   - *Pros:* Compile-time and runtime type safety; `extra="forbid"` blocks field injection; discriminated unions (`action` and `by`) provide precise, actionable error messages; custom safe loader rejects duplicate keys and unsafe tags; enforces 256 KB max size and 50-step bounds; seamless integration with `ProviderPolicy`.

## Decision

1. **Declarative Data Format:** Workflows are strictly declarative YAML or JSON documents. No code execution, script injection, or dynamic `eval` is permitted.
2. **Pydantic v2 Models with Strict Forbid:** All models (`WorkflowDefinition`, `LocatorTarget`, `ExecutionLimits`, and each `WorkflowStep` subclass) enforce `extra="forbid"`.
3. **Action Discriminated Union:** Each supported action (`navigate`, `click`, `fill`, `wait_visible`, `assert_visible`, `extract_text`, `verify_state`, `save_result`, `notify_user`, `request_human_action`) is modeled as an explicit subclass discriminated by `action`.
4. **Locator Strategy:** Locators are discriminated by `by` (`test_id`, `role`, `text`, `css`, `xpath`), rejecting empty or script-injected values.
5. **Schema Versioning & Immutability:** `schema_version: 1` is strictly enforced (`SUPPORTED_SCHEMA_VERSIONS = {1}`). Unrecognized versions trigger `UnsupportedSchemaVersionError` (`code: "UNSUPPORTED_SCHEMA_VERSION"`). Workflows are immutable once published.
6. **Safe Parser Hardening:**
   - Strict size limit of 256 KB (`MAX_WORKFLOW_SIZE_BYTES`).
   - `UniqueKeySafeLoader` rejects duplicate dictionary keys in YAML mappings.
   - `_json_duplicate_key_detector` rejects duplicate keys in JSON objects.
   - Disables all non-standard YAML tags and object constructors.
7. **Policy Integration:** Every parsed workflow is validated against `ProviderPolicy`:
   - Rejects unpermitted providers (e.g. `microsoft_rewards` is denied as companion-only; unknown providers are denied).
   - Rejects unpermitted actions.
   - Statically validates `navigate` URLs against `domain_allowlist`, blocking scheme abuses, embedded credentials, metadata IPs, and domain spoofing.

## Consequences

- **Positive:**
  - Complete security isolation against script injection and YAML deserialization exploits.
  - Predictable, deterministic step execution for the upcoming Playwright interpreter (M2.2).
  - Clear, user-friendly validation error messages with exact paths (`loc`) and error codes.
  - Backward compatibility preserved with existing M0 validation endpoints.
- **Negative / Tradeoffs:**
  - Authors cannot write arbitrary loop constructs or dynamic conditional branching inside the YAML file (complex logic must be handled at the orchestration/job state machine level in M3).
  - Workflows must adhere strictly to predefined locator schemas.

## Verification

Verified via comprehensive test suite in `backend/tests/test_workflow_dsl.py`:
- Valid YAML and JSON workflows for RewardLab.
- Negative tests: invalid schema version, missing fields, unknown actions, duplicate step IDs, duplicate YAML/JSON keys, unsafe YAML tags, oversized content (>256 KB), invalid locator syntax, invalid timeouts, and policy denials for Microsoft Rewards and unknown providers.
- Regression verification: All 29 existing M0 tests pass with zero regressions.

## Status

**Accepted**
