# Show why a review is partial, and start the GitHub publisher

Decisions for this change are locked. No further grill.

Two slices. They do not share files. The API slice is this worktree. The UI slice is the same change in `dmc-268-ui-t2`, in its own worktree, not in `project/dmc-268-ui-t2` main.

Reviewed against staging review `70de96a6-c38c-41ce-9b70-221d26705679` (5 Nov 2026). Analysis finished `PARTIAL` with “4 из 4 чанков” and a red analysis step. Publication stayed `PENDING` (“Публикуется”) because nothing posts to GitHub.

## Current

### Review page

`GET /api/v1/reviews/{id}` returns `coverage` (`included`, `skipped`, `context_quality`), `usage`, and `error`. `skipped[].reason` is one of `policy`, `unsupported_language`, `unavailable_blob`, `provider_truncated`, `token_budget`, `file_limit`, `line_limit`, `context_error`.

`parseReview` in `dmc-268-ui-t2` `src/entities/pull-request/model/types.ts` drops `coverage` and `usage`. The `Review` type has no coverage field.

`ReviewProgress` prints `review.error` as “Причина: …”. That line exists for a failure reason such as `ANALYSIS_DEADLINE_EXCEEDED`. A `PARTIAL` review whose only gap is `coverage.skipped` has `error: null`, so the page stays quiet. The analysis step still says “N из M чанков”. That count is saved chunks, not skipped files. Skipped files never became chunks.

The audit list already prints `limit_reasons` (`file_limit`, `line_limit`, `token_budget`) in English. The review page does not.

Chunk notes in `chunk_result.limitations` (trimmed pull-request text, dropped rules, fallback provider) are not on this JSON. A `PARTIAL` review can have empty `coverage.skipped` and empty `error`. The page cannot explain that case from this response.

### Publisher

`app.publication_worker` is the process that posts the overview and inline comments. It requires `GITHUB__WRITE_TOKEN` and `GITHUB__BOT_USER_ID`. If either is empty it raises `PublicationConfigError` and exits. The general outbox dispatcher sends only `analyze` tasks (`PostgresOutboxDispatcher.dispatch_batch`: “publish remains parked”). `PostgresPublicationDispatcher` runs inside the publication worker and sends `publish` tasks only while `Publication.deadline_at` is still in the future. `SqlAlchemyPublicationRepository.prepare` sets that deadline to 5 minutes.

`docker-compose.staging.yml` has no `publication-worker` service. The API service already receives both GitHub variables. The analysis `worker` service does not.

`.github/workflows/deploy-staging.yml` copies `docker-compose.staging.yml` to the VPS before the remote script. Before migrations it always runs `compose --profile analysis stop worker outbox-dispatcher`. When `COMPOSE_PROFILES=analysis`, it assigns `analysis_services` to `outbox-dispatcher` and `worker`, and the “Starting workers…” step starts that list. The analysis preflight checks `GITHUB__TOKEN` (read). It does not check the write token or the bot user id.

`docs/staging-deployment.md` says this analysis setup does not publish to GitHub, and that the write token is not needed for analysis.

The write token in GitHub secrets is already passed through `append_optional` into the env file. An empty bot user id secret is omitted. A running API container with those variables still does not publish.

A review that has been `PENDING` for more than 5 minutes is no longer eligible. Starting the worker later does not publish it.

## Desired

### UI slice (`dmc-268-ui-t2`)

Keep `coverage` on the parsed review. Under the progress steps, when `coverage.skipped` is non-empty, show a quiet block titled «Не вошло в анализ». A top border separates it from the stepper. Files that share a reason are one row: the paths first, then a small neutral badge. Badge labels:

| reason | badge |
|---|---|
| `file_limit` | лимит файлов |
| `line_limit` | лимит строк |
| `token_budget` | лимит токенов |
| `policy` | правила |
| `unsupported_language` | язык не поддерживается |
| `unavailable_blob` | файл недоступен |
| `provider_truncated` | дифф обрезан |
| `context_error` | ошибка контекста |

Do not print the long phrase «не вошло из-за…». Do not print line ranges. Do not print `included`, `usage`, `snapshot`, or `context_quality`. Null coverage or an empty `skipped` list adds nothing. The note does not depend on review status, so a `COMPLETED` review that skipped a file shows it too. The demo fixture with `public/logo.png` / `policy` will show the note. The existing red «Причина» line stays as it is, including when both are present. It is not a warning style.

Add `coverage` to the `Review` type. `src/test/fixtures.ts` builds `TEST_REVIEW` by hand, so that fixture needs `coverage: null` or the UI typecheck breaks.

“4 из 4 чанков” stays. The new note is the explanation under that step, not a replacement for the count.

### API slice (this repo)

Add service `publication-worker` to `docker-compose.staging.yml`:

- same image as `worker`
- `profiles: [analysis]`
- `restart: "on-failure:3"`
- `command: ["python", "-m", "app.publication_worker"]`
- same Postgres, Redis, logging, and Sentry environment as `worker`
- also `GITHUB__WRITE_TOKEN` and `GITHUB__BOT_USER_ID`, copied from the `api` service
- `depends_on` db and redis, same network

`tests/test_webhook_deployment.py::test_staging_routes_publication_credentials_only_to_api` currently forbids those two variables on every service except `api`. Update it so `publication-worker` may have them and no other service may.

Do not change the local `docker-compose.yml` sleep placeholder.

In `deploy-staging.yml`:

1. The stop line stays unconditional, as it is today: `compose --profile analysis stop worker outbox-dispatcher publication-worker`. `publication-worker` is one more name in that same command, not a separate stop. A later deploy with analysis turned off must still stop a publisher that the previous deploy started. The compose file is already on the VPS before this command. `stop` of a defined service with no container exits 0. Develop deploys already rely on that for `worker` and `outbox-dispatcher`, and the new name uses that same call. `tests/test_manifest_delivery.py` asserts the old stop string and must expect the new one.
2. Only when `COMPOSE_PROFILES=analysis`, before `compose up` of the workers, run `compose run --rm --no-deps --interactive=false -T publication-worker python -c '<check>' </dev/null`. The `python -c` override is required. Without it, `compose run` starts `app.publication_worker` and would publish during deploy. The check loads `get_settings().github` and exits 1 unless the write token is non-blank after `strip()` and `bot_user_id` is non-empty. A non-numeric or zero-leading bot id already fails settings load (`Github._canonical_bot_id`). The check prints no secret values. A failure aborts the whole deploy, after the old analysis workers are already stopped, the same way the existing preflight does. Analysis stays down until the secrets are fixed and deploy is run again. Do not run `--once`.
3. Only when `COMPOSE_PROFILES=analysis`, `analysis_services` (assigned in that block, used at “Starting workers…”) becomes `outbox-dispatcher worker publication-worker`. The wait loop only checks that the container status is `running`. It can pass before a crash. The check in step 2 is the credential gate, not the wait loop.

When the analysis profile is off, do not start the service and do not run the credential check. Develop stays without publication.

`tests/deploy_remote_fakes.py` treats a command containing `worker python -c` as the analysis preflight. Name the one-off so those fakes do not treat the new check as the old preflight, and do not add a second `<<'PREFLIGHT'` heredoc. The existing preflight test takes the first such heredoc.

Update every statement in `docs/staging-deployment.md` that says this analysis setup does not publish, or that the write token is not needed for the running staging processes. List `publication-worker` next to the analysis worker. Keep the local CLI instructions in `docs/github-review-publication.md`.

## Out of scope

- Extending or resetting `Publication.deadline_at` for reviews already stuck on `PENDING`, including `70de96a6-c38c-41ce-9b70-221d26705679`.
- Returning `chunk_result.limitations` from the review API.
- Changing when the backend sets `PARTIAL`.
- Changing the audit page.
- Publishing from the API process or from `app.outbox_dispatcher`.
- A new retry or reconcile path for `FAILED` / `UNKNOWN`.

## Tests

UI, through the rendered progress (Vitest, testing-library):

- `PARTIAL` with two skipped files of one reason shows the heading «Не вошло в анализ», both paths, then the badge «лимит файлов».
- `PARTIAL` with two reasons shows two rows. The path comes before the badge. The page does not contain «не вошло из-за» or `context_quality`.
- `coverage: null` and `skipped: []` add no note.
- A review with `error.reason_code` still shows “Причина”.

API: update `test_staging_routes_publication_credentials_only_to_api` and the manifest-delivery stop-string assertion. The credential check is the `python -c` one-off above, not a new module. Existing publication worker tests stay the proof that the process itself posts to GitHub. Do not add a test that calls GitHub.

## Done when

- A partial review with skipped files shows the grouped note on the review page.
- A staging deploy with the analysis profile and both secrets reaches a running `publication-worker`.
- A staging deploy with the profile on and a missing write token or bot user id fails the deploy after the old analysis workers are stopped, and does not leave a publisher running.
- A deploy with the profile off does not start it.
