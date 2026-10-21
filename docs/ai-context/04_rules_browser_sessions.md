---
last_updated: 2026-10-19
scope: browser_sessions
status: accepted
---

# Browser & Test Session Rules

## Required

- Playwright Async API and Chromium on Linux/Windows; isolate authorized test accounts with distinct BrowserContexts.
- Create context only after policy, account ownership and session permissions pass.
- Always close page/context in `finally`; define navigation, action and total-job timeouts separately.
- Never use shared cookie/localStorage state across accounts; encrypt authorized test storage state at rest and restrict owner-bound access.
- Validate browser URL origin after navigation, redirects and user-driven links; never follow arbitrary foreign redirects with auth material.
- Use role, label or test ID locators; assert postconditions after meaningful actions.
- Traces/screenshots: off by default, sanitize and retain only for a documented short period, no user credentials in artifacts.
- Stop on MFA, CAPTCHA, 401/403, account restriction or authorization failure; request human review.

## Preferred / avoid

```python
# Preferred: resilient accessible locator in owned RewardLab
await page.get_by_role("button", name="Mark as reviewed").click()

# Avoid: viewport-specific click and fixed delay
# await page.mouse.click(523, 184)
# await page.wait_for_timeout(7000)
```

- Session persistence is not a guarantee of continued authentication: expired or revoked server sessions must be handled.
- Never add stealth, webdriver masking, fingerprint modification or artificial human-behavior variation.
- Benchmark concurrent contexts and clean-up across repeated iterations; do not infer memory by counting accounts.
