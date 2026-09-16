# `.agents/` — Team 2 coding-agent toolkit

Human-only notes (governance, wiring, SHAs). Day-to-day agent brief is repo-root `AGENTS.md`. Skill procedures live in `skills/*/SKILL.md`.

## Governance (not for always-on agent context)

- Skills on disk are **eval candidates**, not proven prod. Concurrent default-active ≤2.
- **Stop-rule:** no lift (or worse cost/tail) after paired eval → remove or disable.
- Do **not** install wholesale Matt / Superpowers / Larchanka / Vercel packs; no marketplace auto-update as sole channel.
- One school per failure mode (don’t stack overlapping align/TDD/review packs).
- Team docs home (proposed, Tech Lead confirm): UI repo `docs/` — API links, does not fork.
- Third-party MIT notices for vendored Matt skills: `THIRD_PARTY_NOTICES.md` (pin alone is not enough).
- **UI autonomy (Sprint 1+):** `AGENTS.md` phase router (Align → Execute → Close). Skills stay slash/`$skill` gated (`disable-model-invocation` + Codex `allow_implicit_invocation: false`); Close loads `project-code-review` Process (not one-line axis verdicts). `grilling` is gated — not model-reachable. API `.agents/skills/` must match this catalog SHA.

## Sync

| Field | Value |
|---|---|
| **Source of truth** | UI repo `dmc-268-ui-t2` |
| **API copy rule** | Same files under `.agents/skills/` as UI (plus sync artifacts below) |
| **Upstream Matt pin** | `3cca18b368ae95cdbdebbff572ccafa662551015` ([mattpocock/skills](https://github.com/mattpocock/skills)) |
| **Shared catalog sync SHA** | see `.agents/SYNC_SHA.txt` (content checksum of `.agents/skills/**`) |
| **Sync checker** | `.agents/check-sync.mjs` (owns the hash algorithm) |
| **Sprint** | 1 candidate convention (not proven prod) |

`SYNC_SHA` is a **non-secret catalog content checksum** for UI↔API equality — safe to commit and push. It is not a credential.

**Order:** edit skills in UI → `node .agents/check-sync.mjs --write` → copy `.agents/skills/`, `SYNC_SHA.txt`, `check-sync.mjs`, and this README sync story to API → `node .agents/check-sync.mjs` must exit 0 in both repos.

Do not re-specify the hash algorithm in prose; the script is the source of truth. Exit `0` = match, `1` = mismatch / missing SHA file.

## Default-active vs queued

| Skill | In folder? | Default-active (≤2)? | Model invoke? | Notes |
|---|---|---|---|---|
| `grill-me` | Yes | **Yes** (with grilling) | `disable-model-invocation: true` | User door |
| `grilling` | Yes | **Yes** (same package) | `disable-model-invocation: true` | Only via grill-me / explicit grill |
| `tdd` | Yes | **Yes** | `disable-model-invocation: true` | `/tdd` or AGENTS Execute phase |
| `to-spec` | Yes | Queued | `disable-model-invocation: true` | Follows `AGENTS.md` outputs |
| `to-tickets` | Yes | Queued | `disable-model-invocation: true` | Follows `AGENTS.md` outputs |
| `project-code-review` | Yes | Queued | `disable-model-invocation: true` | `/…` or AGENTS Close axes |
| `skill-creator` | **No** | — | — | Engineer 4 meta only (skills_talks / personal) |

Promote later = move a queued skill into the ≤2 active set and demote another. Update this matrix and the phase lines in UI `AGENTS.md`.

## Tool wiring

Portable catalog path: `.agents/skills/<name>/SKILL.md`.

| Tool | How to wire / invoke |
|---|---|
| **Cursor** | Symlink or copy into `.cursor/skills/` (Cursor discovers project skills there), **or** point the agent at `.agents/skills/<name>/SKILL.md`. Explicit invoke: `/skill-name` (e.g. `/grill-me`). Prefer symlink so UI remains SoT. |
| **Claude Code** | Point skill roots at `.agents/skills` / copy into the tool's project skills dir. Root `CLAUDE.md` points at `AGENTS.md`. Keep `disable-model-invocation` semantics. |
| **Codex** | Discovers `.agents/skills` natively. Explicit invoke: `$skill-name` (e.g. `$grill-me`). Each explicit-only skill has `agents/openai.yaml` with `policy.allow_implicit_invocation: false` matching Cursor's `disable-model-invocation: true`. |

**Hybrid policy:** default-active skills (`grill-me`/`grilling`, `tdd`) are **user/phase gated** — load when the user names them or `AGENTS.md` phases say so. Tools must **not** implicitly auto-pick those skills.

## Ownership

Each skill folder has `OWNERSHIP.md` (`last_eval_at: none (Sprint 1 candidate)`).
