---
last_updated: 2026-10-19
scope: agent_goals
status: accepted
---

# How to Use `/plan` and `/goal` in Codex

`/goal` is optional. `AGENTS.md` supplies stable boundaries; `GOALS.md` records desired outcomes; `IMPLEMENTATION_PLAN.md` defines milestones; `01_status.md` records the live handoff. A slash-command Goal tracks only the active Codex conversation and is not a substitute for repository documentation.

## Recommended interaction

1. Read the repo context and choose one ready task in the earliest incomplete milestone.
2. Use `/plan` to review changes, tests, dependencies and non-goals before coding.
3. For a sufficiently large bounded implementation/investigation, use `/goal <objective>` with **observable acceptance**. Do not use it for a one-line edit.
4. Codex should stop at blockers, policy conflicts, approval-required actions or milestone completion.
5. Review observed test evidence; update `01_status.md` and appropriate records. Explicitly set the next milestone Goal only after acceptance.

## Example: bootstrap M0 only

```text
/goal Complete only M0 of ADBAP using docs/IMPLEMENTATION_PLAN.md.
Read AGENTS.md, docs/ai-context/00_core.md and 01_status.md.
Deliver FastAPI /api/v1/health and deny-by-default ProviderPolicy.
Prove acceptance using actually executed tests and working setup instructions.
Update milestone checklist and 01_status.md with evidence and blockers.
Do not start M1, access live Microsoft Rewards, install paid services,
commit/push/deploy or relax policy. Stop if acceptance cannot be verified.
```

## Example: distributed fault investigation (M4 only)

```text
/goal On RewardLab with two authorized test workers, demonstrate that
stale lease results are rejected and no duplicate side effect is observed
in the prescribed crash/retry test. Record test command and trace location.
Do not modify other milestones or deploy externally.
```

## Manage or stop

- `/goal` displays the current goal.
- `/goal edit` changes the objective if supported; `/goal pause` and `/goal resume` control progress; `/goal clear` ends the goal.
- If a command is unavailable, check the installed Codex version/feature support. A normal scoped prompt remains fully sufficient.

## References

- https://developers.openai.com/cookbook/examples/codex/using_goals_in_codex
- https://developers.openai.com/codex/cli/slash-commands
