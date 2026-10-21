---
last_updated: 2026-10-19
scope: workflow
status: accepted
---

# Workflow DSL, Recorder & Execution Rules

## Schema contract

- Canonical v1 shape and allowed actions: `docs/ARCHITECTURE.md`, Workflow DSL section.
- Parse YAML safely (never unsafe object constructors), validate with Pydantic using forbidden extra fields; reject unknown actions and unknown schema versions.
- Workflow definitions are **data**, not Python/JavaScript/bash code; never eval, exec or load unapproved script URLs.
- Published versions immutable. Runtime always records concrete version and validated parameters.
- Account credentials, cookies and sensitive values must never be embedded in a workflow definition or Recorder output.
- Parameters and output references must be typed and scoped to the current job, never cross-account global variables.

## Execution semantics

- Require provider permissions, domain allowlist and action capabilities before dispatch and each step.
- Side-effect step must have idempotency and verified postcondition strategy, or require human review instead of unsafe retry.
- Define retryable vs terminal failure codes. Timeout alone does not establish whether server action occurred.
- A click success is **not** workflow completion; verify against RewardLab test server state when available.
- Recorder produces drafts from role/test-id locators and placeholders; user reviews side effects and success criteria before publishing.
- Document migration strategy when DSL schema changes; do not silently change old version meaning.

## Test gates

- Schema rejects unsupported/unsafe action, unknown provider, embedded secret and foreign navigation.
- Deterministic integration suite covers render delays, changed labels, timeout-after-commit and side-effect deduplication.
