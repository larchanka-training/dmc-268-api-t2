# `.agents/` — Team 2 coding-agent toolkit

Human-only notes (governance, wiring, SHAs). Day-to-day agent brief is repo-root `AGENTS.md`. Skill procedures live in `skills/*/SKILL.md`.

## Governance (not for always-on agent context)

- Skills on disk are **eval candidates**, not proven prod. Concurrent default-active ≤2.
- **Stop-rule:** no lift (or worse cost/tail) after paired eval → remove or disable.
- Do **not** install wholesale Matt / Superpowers / Larchanka / Vercel packs; no marketplace auto-update as sole channel.
- One school per failure mode (don’t stack overlapping align/TDD/review packs).
- Team docs home (proposed, Tech Lead confirm): UI repo `docs/` — API links, does not fork.

## Sync

| Field | Value |
|---|---|
| **Source of truth** | UI repo `dmc-268-ui-t2` |
| **API copy rule** | Same files under `.agents/skills/` as UI |
| **Upstream Matt pin** | `3cca18b368ae95cdbdebbff572ccafa662551015` ([mattpocock/skills](https://github.com/mattpocock/skills)) |
| **Shared catalog sync SHA** | `06cce159093ddbf4f83f8688aa90c601e7fa00c531b9834b67883f24eb39406f` (also `SYNC_SHA.txt`) |
| **Sprint** | 1 candidate convention (not proven prod) |

Recompute after skill edits (hash sorted `path + file SHA256` lines of `.agents/skills/**`). UI and API must match. API is a copy — edit skills in UI first, then re-copy and refresh this SHA in both READMEs.

## Default-active vs queued

| Skill | In folder? | Default-active (≤2)? | Model invoke? | Notes |
|---|---|---|---|---|
| `grill-me` | Yes | **Yes** (with grilling) | `disable-model-invocation: true` | User door |
| `grilling` | Yes | **Yes** (same package) | Allowed (primitive) | Only intentional model-reachable skill |
| `tdd` | Yes | **Yes** | `disable-model-invocation: true` | Rewritten `/tdd` |
| `to-spec` | Yes | Queued | `disable-model-invocation: true` | Follows `AGENTS.md` outputs |
| `to-tickets` | Yes | Queued | `disable-model-invocation: true` | Follows `AGENTS.md` outputs |
| `project-code-review` | Yes | Queued | `disable-model-invocation: true` | Project-owned; AC/tests/bugs/security |
| `skill-creator` | **No** | — | — | Engineer 4 meta only (skills_talks / personal) |

Promote later = move a queued skill into the ≤2 active set and demote another. Update this matrix and the short “prefer grill/`/tdd`” line in both `AGENTS.md` files.

## Tool wiring

Portable catalog path: `.agents/skills/<name>/SKILL.md`.

| Tool | How to wire |
|---|---|
| **Cursor** | Symlink or copy into `.cursor/skills/` (Cursor discovers project skills there), **or** invoke by pointing the agent at `.agents/skills/<name>/SKILL.md`. Prefer symlink so UI remains SoT. |
| **Claude Code** | Point skill roots at `.agents/skills` / copy into the tool's project skills dir. Keep `disable-model-invocation` semantics. |
| **Codex** | Map `disable-model-invocation: true` → `allow_implicit_invocation: false` for the same skills. |

## Ownership

Each skill folder has `OWNERSHIP.md` (`last_eval_at: none (Sprint 1 candidate)`).
