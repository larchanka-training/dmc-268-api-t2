# Proposal: Legal Actions secret names for bot GitHub credentials

Status: accepted (with review fixes)  
Repos: API `dmc-268-api-t2` (primary); UI docs that name the illegal Secrets  
Related: bot-connect PRs API #61 / UI #39; staging Deploy workflow

## Problem

GitHub Actions rejects repository secret names that start with `GITHUB_`.

Operators cannot create:

- `GITHUB__WRITE_TOKEN`
- `GITHUB__BOT_USER_ID`

(and likewise could not create `GITHUB__TOKEN` as an Actions secret).

The Deploy workflow and staging docs currently tell people to use those illegal names for write token and bot id. Read token was already fixed in the workflow as `STAGING_GITHUB_READ_TOKEN` → container `GITHUB__TOKEN`, but the secrets table in `docs/staging-deployment.md` still lists `GITHUB__TOKEN` as the Actions secret name. UI `docs/proposals/github-bot-connect-proposal.md` still lists the illegal write/bot secret names.

## Desired

Keep **container / Compose / local `.env`** names unchanged (app settings):

| Container env | Meaning |
|---|---|
| `GITHUB__TOKEN` | Bot read PAT |
| `GITHUB__WRITE_TOKEN` | Bot write PAT |
| `GITHUB__BOT_USER_ID` | Numeric bot user id |

Use **Actions secret** names that do not start with `GITHUB_`, matching the existing read-token pattern:

| Actions secret | Maps to container |
|---|---|
| `STAGING_GITHUB_READ_TOKEN` | `GITHUB__TOKEN` (already wired) |
| `STAGING_GITHUB_WRITE_TOKEN` | `GITHUB__WRITE_TOKEN` |
| `STAGING_GITHUB_BOT_USER_ID` | `GITHUB__BOT_USER_ID` |

No application Python settings rename. No UI runtime env change.

Write/bot remain **optional** passthrough: if the Actions secrets are missing, Deploy still succeeds and omits those keys from the VPS env file; Connect Check access / publication stay off until the secrets are set and a deploy rewrites the env file.

## Touchpoints

### API repo (must change)

1. `.github/workflows/deploy-staging.yml`  
   - `secrets.GITHUB__WRITE_TOKEN` → `secrets.STAGING_GITHUB_WRITE_TOKEN`  
   - `secrets.GITHUB__BOT_USER_ID` → `secrets.STAGING_GITHUB_BOT_USER_ID`  
   - Keep `append_optional 'GITHUB__WRITE_TOKEN'` / `'GITHUB__BOT_USER_ID'` (VPS env file keys).

2. `tests/test_webhook_deployment.py`  
   - Assert Deploy reads `secrets.STAGING_GITHUB_WRITE_TOKEN` and `secrets.STAGING_GITHUB_BOT_USER_ID` (not the illegal names).  
   - Keep assertions that the **container / env-file** keys remain `GITHUB__WRITE_TOKEN` and `GITHUB__BOT_USER_ID`.

3. `docs/staging-deployment.md`  
   - Secrets table: document Actions names `STAGING_GITHUB_*` → container `GITHUB__*`.  
   - Fix the “same name as container” convention for this family: Actions names must not start with `GITHUB_`; mapping is explicit in Deploy.  
   - Do **not** change the analysis checklist note that write token is not required for analysis enablement; only fix the secrets table + convention.

4. This proposal file.

### API repo (do not change)

- `.env.example`, `.env.staging.example`, `docker-compose.staging.yml` — keep container names.
- `src/app/core/config.py` and runtime modules — keep `GITHUB__*` settings.
- Runtime docs that only name container env (`docs/github-review-publication.md`, README) — optional one-line Actions pointer; not required unless a sentence tells operators to put `GITHUB__*` into Actions Secrets.

### UI repo (docs only; no runtime)

5. `docs/proposals/github-bot-connect-proposal.md`  
   - Replace illegal Actions secret names with `STAGING_GITHUB_WRITE_TOKEN` and `STAGING_GITHUB_BOT_USER_ID`.  
   - Fix the “No new secret names” wording: Actions uses `STAGING_GITHUB_*`; containers stay `GITHUB__*`.  
6. SYSTEM_DESIGN docs name container env only — leave unless they claim Actions secret names.

### Meta / skills_talks (optional follow-up)

- `ai_code_reviewer/devops/04-current-pipeline.md` may still list outdated Actions names; update in a separate meta commit if desired. Not required to unblock staging.

## Operator steps (after merge + deploy of this fix)

1. In API repo → Settings → Secrets and variables → Actions, create (or update):
   - `STAGING_GITHUB_READ_TOKEN` (if missing)
   - `STAGING_GITHUB_WRITE_TOKEN`
   - `STAGING_GITHUB_BOT_USER_ID`
2. Do **not** create secrets named `GITHUB__*`.
3. Run Deploy so the VPS env file is rewritten — for **staging** and, if used, **develop** (same Actions secret names feed both).
4. Without write + bot id secrets, deploy still works; publication / bot-id checks stay inactive until those secrets exist and Deploy has rewritten the env file.

## Out of scope

- Changing PAT scopes / fine-grained vs classic (ops choice).
- Renaming `AUTH__GITHUB_*` (those do not start with `GITHUB_`; Actions accepts them).
- Built-in `secrets.GITHUB_TOKEN` (GHCR login; unrelated).
- Inventing new container env names.
- UI application code.

## Risks

- Illegal Actions names never existed in the Secrets UI — no value migration. Enter values under the new names.
- Docs that still say “secret name equals container name” will mislead until updated; that is why the staging doc convention paragraph is in scope.
- UI proposal left stale would re-teach illegal names — include UI doc fix on the paired branch.
- Skipping the deployment test update fails CI even if the workflow looks correct.

## Acceptance

- [ ] Deploy workflow references only legal Actions secret names for bot credentials.
- [ ] `tests/test_webhook_deployment.py` expects `secrets.STAGING_GITHUB_WRITE_TOKEN` and `secrets.STAGING_GITHUB_BOT_USER_ID` and stays green.
- [ ] Staging deployment doc table + convention match the workflow.
- [ ] UI bot-connect proposal lists the same Actions names and no longer says “no new secret names” for this mapping.
- [ ] Local Compose / app still use `GITHUB__WRITE_TOKEN` and `GITHUB__BOT_USER_ID`.
- [ ] Operator can add the three `STAGING_GITHUB_*` secrets in the GitHub UI without the `GITHUB_` prefix error.
