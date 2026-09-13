# AGENTS.md — DMC-268 API (Team 2)

Always-on brief for coding agents. Longer procedures live in `.agents/skills/` (same catalog as UI; load on demand). Human wiring / skill matrix: `.agents/README.md`. Domain terms: `CONTEXT.md`.

## How to work here

- Make the smallest change that solves the ask. Match existing patterns; do not invent structure without need.
- Prefer lint / typecheck / test over inventing process. Before calling work done, run the commands below and say what passed.
- Prefer `/grill-me` when scope is unclear, and `/tdd` when changing behaviour test-first. Other skills (`to-spec`, `to-tickets`, `project-code-review`) only when the user asks for them.

## Where to put outputs

Agreed lasting notes → team tracker (issue or PR). Session drafts → `.scratch/` (gitignored).

## When behaviour changes

Write a failing test first, then the minimal code to pass it. Test through public APIs (HTTP handlers, service interfaces), not private helpers. One small slice at a time. Full procedure: `/tdd`.

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
