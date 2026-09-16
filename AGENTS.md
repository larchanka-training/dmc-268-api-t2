# AGENTS.md — DMC-268 API (Team 2)

Always-on brief for coding agents. Longer procedures live in `.agents/skills/` (same catalog as UI; load on demand). Human wiring / skill matrix: `.agents/README.md`. Domain terms: `CONTEXT.md`.

## How to work here

- Make the smallest change that solves the ask. Match existing patterns; do not invent structure without need.
- Prefer lint / typecheck / test over inventing process. Before calling work done, follow **Close** below (quality commands **and** review axes).
- Skills on disk: default-active pair = `grill-me`(+`grilling`) and `tdd` (candidate). Queued: `to-spec`, `to-tickets`, `project-code-review` — load their procedure when the phase below says so, or when the user names them. Do not invent extra process.

## Phases

1. **Align** — Scope empty or the user asks to stress-test a plan → `/grill-me` only. Do **not** open grilling on model whim.
2. **Execute** — User gave go-ahead, or grill outcome / spec / tickets already exist in context → implement. On behaviour change, follow the `/tdd` procedure (red → green at agreed seams) without waiting for a second `/tdd` if they already said implement / approved the plan. If a test fails for a bad fixture/double (false red), fix the test and re-run to a **real** red before writing production code.
3. **Close** — After the slice is green: load `.agents/skills/project-code-review/SKILL.md` and run its Process (pin range → quality commands → four axes with severity → merge blocked yes/no). One-line axis verdicts without that Process are not Close. Lint/test alone is not “reviewed.”

## No re-ask

Ask only when a **product decision** is missing. Do **not** re-ask for seams, AC, ticket scope, test runner, or file layout already stated in conversation, grill, `.scratch/`, Issue/PR, or tickets.

## Parallel / subagents

When tasks are **independent** (no shared write set, no blocking edge, no shared unresolved decisions), do them via **subagents in parallel** — e.g. multiple unblocked tickets, exploratory reads, validation commands, post-impl review vs unrelated cleanup.

**Do not parallelize:** grill (needs the user); red → green on the **same** seam/module; tickets that block each other; edits that would conflict on the same files.

When you keep independent-looking work sequential, state why in one line (shared write set, lockfile, blocking ticket, same seam). Parent agent owns merge order, the final “done” verdict, and any question to the user.

## Where to put outputs

Agreed lasting notes → team tracker (issue or PR). Session drafts → `.scratch/` (gitignored).

## When behaviour changes

Write a failing test first, then the minimal code to pass it. Test through public APIs (HTTP handlers, service interfaces), not private helpers. One small slice at a time. Before more production code, the next AC case at the seam must still fail (real red). If it is already green, the last green overshot — delete speculative code or split the slice. Reuse seams already agreed; full procedure: `.agents/skills/tdd/SKILL.md`.

## Stack

- Python 3.14, FastAPI, SQLAlchemy async, Alembic, uv, Postgres, Redis
- Layout: `src/app/{api,core,db}`; `migrations/` excluded from ruff/mypy
- Liveness: `GET /healthcheck` (see `CONTEXT.md` — not the same as Readiness)

```bash
uv sync
uv run ruff check
uv run ruff format --check
uv run mypy .
uv run pytest
```
