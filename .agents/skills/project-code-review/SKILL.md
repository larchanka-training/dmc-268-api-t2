---
name: project-code-review
description: Post-implementation review of the agent's own changes — acceptance criteria, tests, bugs/correctness, and security. Follow when user says /project-code-review, asks to review before merge, or AGENTS.md Close phase applies after an implementation slice.
disable-model-invocation: true
---

# Project code review

Review **this agent's recent changes** (working tree, branch vs merge-base, or a fixed point the user names). This is a coding-agent procedure — **not** the product AI Code Reviewer service.

When `AGENTS.md` Close applies, walk these four axes even if the user did not type `/project-code-review`. Quality commands alone are not enough.

## Scope

Check four axes. Report each separately; do not merge findings into a single ranked list.

1. **Acceptance criteria** — Does the diff satisfy the agreed AC / Issue / PR / grill outcome? Flag missing, partial, or wrong behaviour. If no AC source exists, say so and review against the user's stated goal only.
2. **Tests** — Are tests present where behaviour changed? Are they at appropriate seams (public behaviour, not internals)? Do they pass, or is there a clear gap / skip with reason? Cite commands run and results.
3. **Bugs / correctness** — Logic errors, edge cases, race/error handling, regressions vs nearby code. Prefer concrete repro or failing assertion over vague concern.
4. **Security** — Secrets in code/diff, authz gaps, injection (SQL/command/HTML), unsafe defaults, sensitive logging. Skip vibes; flag only plausible issues in the changed surface.

Also apply repo policy from `AGENTS.md`: smallest justified change; match existing patterns; run stack quality commands and cite evidence before claiming "done".

## Process

1. Pin the review range (`git diff <base>...HEAD`, unstaged, or paths the user names). Confirm non-empty.
2. Locate AC source: tracker link, `.scratch/` draft, conversation agreement, or user-provided checklist.
3. Run the repo's quality commands from `AGENTS.md` (lint / typecheck / test as available). Record pass/fail.
4. Walk the four axes. Under each heading: finding, location (file/hunk), severity (`blocker` / `should-fix` / `nit`), suggested fix in one line.
5. End with a one-line verdict per axis and whether merge is blocked.

## Out of scope

- Full Fowler smell catalogs or dual-axis "standards vs spec" frameworks unless they help a concrete finding.
- Product reviewer prompt stubs / `docs/reviewer/`.
- Rewriting unrelated code "while you're there."
