# IMPLEMENTATION_PLAN.md — Execution plan (living checklist)

**Project:** Adaptive Distributed Browser Automation Platform (ADBAP)
**Primary target:** self-owned RewardLab; 12 synthetic accounts for scale test.
**Policy:** Microsoft Rewards companion-only (official links/reminders/manual progress); no automated point earning, anti-bot evasion or unattended redemption.
**Live status:** `docs/ai-context/01_status.md` (single source of current progress). This file owns planned scope and acceptance gates, not measured results.

## How to work with this plan

- Each unchecked item is an independently reviewable vertical slice. Pick the first ready unchecked task; do not implement multiple phases in one AI request.
- Before each task: inspect repo, print plan/files/tests, create tests first where practical. After completion: run tests, check off only proven work, update `docs/ai-context/01_status.md`; add meaningful behavioral changes to `CHANGELOG.md` and detailed evidence to `docs/spikes/` when needed.
- If a task changes domain contracts: update schema, migration, API documentation, fixtures, tests and this plan together.
- Prioritize a working narrow end-to-end path over empty files, mock dashboards or unnecessary boilerplate.

## Milestone M0 — Repository, boundaries and developer workflow

**Goal:** reproducible local setup, explicit safety constraints.

- [x] M0.1 Bootstrap Python backend using `uv`, FastAPI, Pydantic, SQLAlchemy, pytest, Ruff; add `pyproject.toml`, typed config and `GET /api/v1/health`.
- [x] M0.2 Create repository structure in `ARCHITECTURE.md`, `.gitignore` (secrets, Playwright auth state, browser profiles, traces), `.env.example` and `README.md` with Windows/Linux steps.
- [x] M0.3 Implement `ProviderPolicy` schema, default-deny policy registry, permission audit event and unit tests for denied live Rewards automation, denied unknown domains, allowed RewardLab actions.
- [x] M0.4 Add CI for lint and unit tests (no live service credentials); capture setup prerequisites.

**Acceptance:** clean checkout can install/run API locally; `/health` returns OK; unsafe providers/actions are rejected with stable error code; unit tests pass; no secrets committed.
**Demo:** GET `/api/v1/health` and POST a prohibited workflow validation -> `POLICY_DENIED`.

## Milestone M1 — RewardLab owned test website + data

**Goal:** controlled realistic automation target with server-verifiable outcomes.

- [x] M1.1 Build RewardLab minimal web UI: login, dashboard, tasks listing, task details, mark reviewed. Use explicit `data-testid` and accessible labels.
- [x] M1.2 Seed **12 fake test accounts**, independent task/point state; no real Microsoft credentials, real rewards or redemption.
- [x] M1.3 Add server-side task-status API and idempotent completion endpoint (idempotency key, repeat-safe result).
- [x] M1.4 Add deterministic fault injection: delayed render, transient 500, 429 + Retry-After, expired session, browser navigation fault and changed button label.
- [x] M1.5 Add integration tests for task isolation, idempotency and forced failures.

**Acceptance:** two test accounts cannot read/change each other's task state; marking a test task twice has one side effect; fault scenarios can be reproduced deterministically.
**Demo:** login test_01; complete a fake task; verify in API; test_02 remains unchanged.

## Milestone M2 — Workflow DSL + Playwright execution

**Goal:** first reliable end-to-end automation in one browser worker.

- [x] M2.1 Create strict versioned workflow schema, parser, schema migration strategy and validation errors; ship safe sample YAML.
- [x] M2.2 Implement action interpreter for `navigate`, `wait_visible`, `extract_text`, `click`, `fill`, `assert_visible`, `verify_state` using Playwright async.
- [x] M2.3 Implement Browser Manager; one Chromium launch, short-lived independent contexts, cleanup in `finally`, context timeouts, trace-on-failure with redaction.
- [x] M2.4 Integrate permission checks before dispatch and before each step; validate domains/redirects; disable arbitrary scripts/eval.
- [x] M2.5 Implement a CLI `run-workflow --provider rewardlab --account test_01 --file ...` plus integration test.

**Acceptance:** a YAML workflow logs into RewardLab test account, reads and reviews fake task, verifies via test server; denied provider creates no browser action; fail/timeout releases context.
**Demo:** run one workflow on test_01 and show job trace without secrets.

## Milestone M3 — Multi-account local orchestration

**Goal:** 12 synthetic accounts, controlled concurrency and crash-safe local state.

- [x] M3.1 Add SQLAlchemy tables + Alembic migrations for providers, accounts, workflow versions, jobs/steps, session refs and audit events.
- [x] M3.2 Implement per-account encrypted test session store, expiry/reauth, owner-bound access, no plaintext tokens in logs.
- [x] M3.3 Implement job state machine, unique idempotency keys, per-account execution lock, failure classification and bounded backoff.
- [x] M3.4 Add dedicated local worker process + queued scheduler (avoid executing durable jobs inside API request/BackgroundTasks); max concurrency initial 2.
- [x] M3.5 Add graceful shutdown, recovery reconciliation after crash and metric collection (CPU, RSS, p50/p95, duration, pass rate).
- [x] M3.6 Load-test 12 fake accounts at concurrency 1/2/3 on a local machine; record actual measurements and hypotheses.

**Acceptance:** no concurrent job for same account; cross-account state isolated; restart leaves no lost jobs; no duplicate side effect in injected timeout test; reported measurements are from real tests, not estimates.
**Demo:** enqueue a workflow for 12 accounts, show 2 concurrent jobs; restart worker mid-execution; reconcile correctly.

## Milestone M4 — Distributed scheduler and worker nodes (Linux/Windows)

**Goal:** 2 authenticated worker processes on separate hosts / containers.

- [ ] M4.1 Migrate shared job storage to PostgreSQL; implement atomic claiming, lease renewal/expiry, monotonic fencing token and stale-result rejection.
- [ ] M4.2 Implement outbound-pull Node Agent: register, auth, advertise capabilities, heartbeat, claim, progress, result, drain.
- [ ] M4.3 Implement resource-aware dispatch with per-node max concurrency and account affinity; ensure no double-active account lease.
- [ ] M4.4 Implement retry/reconciliation under worker offline, network partition, expired lease and duplicate results; do not blindly replay side effects.
- [ ] M4.5 Deploy 2 Linux/Windows workers against RewardLab; demonstrate failover and audit logs.

**Acceptance:** one and only one current lease token can commit a job result; node crash does not corrupt queue; duplicate claims/results are ignored or reconciled; no secret leakage between nodes.
**Demo:** two workers service 12 fake accounts; unplug one; observe remaining work and safe recovery.

## Milestone M5 — Dashboard and management UX

**Goal:** minimal usable UI after API contracts stabilize.

- [ ] M5.1 Add Next.js + TypeScript frontend, API client and local operator authentication.
- [ ] M5.2 Views: Overview, Providers/Policies, Test Accounts, Workflows, Jobs, Nodes, Metrics/Audit.
- [ ] M5.3 Allow upload/edit/validate versioned YAML workflows; show denied action reason; approve only after validation.
- [ ] M5.4 Add local job action controls (queue, cancel, drain); show live status and selected logs without credentials.
- [ ] M5.5 Add Microsoft Rewards companion only: official links, reminders, optional manual progress entries, policy label.

**Acceptance:** create test workflow in UI, validate, queue for synthetic accounts and observe completion; Rewards UI cannot initiate auto-earning workflow.
**Demo:** screen share end-to-end RewardLab job run via dashboard.

## Milestone M6 — Workflow Recorder

**Goal:** reduce need for hand-authored YAML on RewardLab.

- [ ] M6.1 Implement safe recording of user-driven navigation, role/test-id locators and value placeholders for test site only.
- [ ] M6.2 Convert recorded actions into DSL draft (no coordinates as default); require explicit success condition for side effects.
- [ ] M6.3 Provide visual workflow review/edit, validation, run in isolated test environment, version publish.
- [ ] M6.4 Cover dynamic UI and fragile locator regression tests.

**Acceptance:** recorded test workflow reproduces on independent fake account with no raw credentials embedded; published workflow has a verifier step.

## Milestone M7 — AI-assisted workflow authoring (feature flag OFF by default)

**Goal:** safe goal-to-validated-workflow, not behavior camouflage.

- [ ] M7.1 Define provider-independent LLM interface with strict structured outputs and model-free fake for tests.
- [ ] M7.2 Sanitize DOM/accessibility snapshots; strip secrets, account data and untrusted instructions; scope input to RewardLab.
- [ ] M7.3 Implement AI-generated **draft** workflow and policy/schema validation, sandbox test, human approval and immutable workflow version.
- [ ] M7.4 Implement repair proposal on selector failure; diff and require approval; never silently modify running workflow.
- [ ] M7.5 Evaluate on fixed test benchmark: valid-schema rate, policy-denial recall, task completion and cost/latency. Explicitly reject evasion-oriented goals.

**Acceptance:** AI cannot publish/execute workflow without approved policy and human sign-off; mocked malicious DOM instruction cannot broaden privileges; no secrets sent to LLM.

## Milestone M8 — Android feasibility spike (separate, go/no-go)

**Goal:** assess Android agent *without* assuming full Chromium runtime.

- [ ] M8.1 Prototype Android or Termux outbound-pull agent, authenticated heartbeat, battery/memory/thermal status and safe draining.
- [ ] M8.2 Test persistence across screen-off and OS background constraints; capture device/Android version and measured reliability.
- [ ] M8.3 Evaluate browser capabilities separately on owned test site. If no reliable browser runtime, advertise only `lightweight_tasks` and keep Playwright on Linux/Windows.
- [ ] M8.4 Compare Android vs Linux job success, operational reliability, CPU/RAM and maintenance cost; record go/no-go.

**Acceptance:** no claim of six simultaneous Chromium sessions without empirical demonstration; compatibility and limitations documented; no production use if background execution unreliable.

## Nonfunctional gates before any real deployment

- [ ] Authorized provider registry with reviewed permissions and deny-by-default.
- [ ] Proper HTTPS, node identity, least-privilege access and revocation.
- [ ] Encrypted session storage, secret redaction and backup/restore tests.
- [ ] Trace/log retention policy, account data deletion path, dependency audit.
- [ ] Rate limits, bounds on retries, no CAPTCHA/MFA bypass, stop on 403/restrictions.
- [ ] Two-node chaos tests + repeatable load benchmarks.
- [ ] Clear operator documentation explaining policy and limit of automation.

## Release checkpoints and priority

- **Release R0 (first useful demo):** M0 + M1 + M2. One authorized workflow runs end-to-end.
- **Release R1 (multi-account demo):** M3. Twelve synthetic accounts, local scheduler, measured results.
- **Release R2 (distributed):** M4 + M5. Two worker nodes + UI.
- **Release R3 (adaptive):** M6 + M7. Recorder and AI-assisted proposals.
- **Research spike:** M8. Independent of R0/R1 success.

## Progress, evidence and decisions

- Current progress and last observed tests: `docs/ai-context/01_status.md`.
- Lasting architecture decisions: `docs/adr/`.
- Detailed test/benchmark investigations: `docs/spikes/`.
- Short behavioral change history: `CHANGELOG.md`.
