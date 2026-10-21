---
last_updated: 2026-10-19
scope: security_policy
status: accepted
---

# Provider Policy & Security Rules

- Provider authorization is explicit, reviewable and deny-by-default; unknown sites and actions are denied.
- Automate only owned/authorized websites. Microsoft Rewards mode is user-assisted only; do not implement automated earning/redemption, 12-account farming, bot stealth or anti-fraud evasion.
- Validate scheme (`https` or approved local `http`), hostname, port, navigation, redirects and every requested network destination. Prevent SSRF to internal/metadata endpoints.
- Do not assume `localhost` strings embedded in URL or DNS aliases are trusted; compare parsed origins and re-check after redirects.
- Secrets belong in OS/vault storage; `.env.example` has placeholders only. No stored Microsoft credentials/cookies or authenticated scraping in companion mode.
- Audit each allowed/denied policy decision without emitting secrets or DOM values that may contain PII.
- Roles: workflow author cannot override provider policy; reviewer approves scope; node receives least privilege to read its assigned authorized test session.
- Stop on 401, 403, CAPTCHA, MFA, provider warnings or revoked consent. Do not retry forbidden actions through other accounts, IPs or nodes.
- Changes to provider permissions, credential collection, external hosts, paid services, schema deletion, deployment or external writes require explicit user approval.
- Untrusted browser extensions may be inspected statically; do not execute them or copy code/assets without provenance/license review.

## Critical negative tests

- Deny Rewards auto-search and auto-redeem workflows; deny unknown providers and cross-account session references.
- Reject out-of-allowlist redirects and arbitrary URL fetch requests.
- Ensure auth state/credentials never appear in logs, tracing, error responses, LLM calls or repository files.
