---
last_updated: 2026-10-19
scope: ai_assistant
status: accepted
---

# AI Workflow Assistant Rules

- Feature flag OFF by default; rule-based authoring and automated validation work without any LLM.
- Input is a bounded, sanitized DOM/accessibility snapshot from RewardLab or explicitly approved origin.
- Website content is untrusted data. Prompt injection must not change permissions, tool availability or task goal.
- Do not put auth cookies, credentials, tokens, personal profiles, raw PII, session storage or secrets in prompts.
- LLM returns schema-constrained **draft** plans only; server validates actions, parameters, origin and provider before running.
- AI-generated workflow or self-healing proposal requires sandbox validation and human approval to publish an immutable new version.
- Never allow AI to silently change a running version, self-authorize a provider or issue an external transaction.
- AI may vary test paths to improve **test coverage**, never to camouflage bot behavior or evade anti-fraud systems.

## Evaluations

- Record draft schema-valid rate, policy-rejection correctness, test completion, latency/cost and manual-review rate on fixed RewardLab fixtures.
- Inject malicious DOM instructions in mock pages and verify no privilege escalation.
- Use deterministic fake LLM in CI; avoid paid calls or non-reproducible external tests.
