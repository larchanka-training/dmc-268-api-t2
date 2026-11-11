# Stop a running review, then start another

Decisions are locked. No further grill.

Two slices, two worktrees, neither on `project/*/main`.

- API: this worktree, branch `feat/cancel-review`
- UI: `.scratch/worktrees/ui-cancel-review`, branch `feat/cancel-review`

API lands first. The UI calls the contract below.

## Current

A review status is `QUEUED`, `FETCHING_DIFF`, `PARSING_CONTEXT`, `LLM_PROCESSING`, `COMPLETED`, `PARTIAL`, `FAILED`, or `SKIPPED`. There is no cancel call.

`POST /api/v1/repositories/{id}/pull-requests/{number}/reviews` starts a review. While `review_job.finished_at` is null for the same pull request, commit, and config, a second start returns `409 REVIEW_ALREADY_ACTIVE` (`uq_review_job_active_equivalent`). The body may include `rerun_of`. The UI start button never sends it.

`persist_start` increments `QuotaUsage` type `starts`. `_check_quotas` reads that row and counts `ReviewJob` rows with `finished_at IS NULL`. Nothing writes a `quota_usage` row of type `active_jobs`. Finishing a job frees the active slot because the count drops. Nothing gives a start back.

`ReviewReasonCode` has no user-cancel value. The reason lives on `review_event.reason_code` (text, no reason checklist in the database). The review detail already returns that code. The UI maps it in `ReviewProgress` and does not know `USER_CANCELLED`.

`AnalysisHandler._infer` calls the model, then saves the chunk, then on the last chunk finishes the run. A failure that already has saved chunks becomes `PARTIAL` and prepares publication. There is no read of “a person stopped this” before the next model call or before the insert.

`POST /api/v1/reviews/{id}/publication-retries` only retries a confirmed-absent `FAILED` publication. It does not rerun analysis. Leave it alone.

The review page has progress and no Stop or Run again. Start review stays on the pull-request list.

## Desired

### Stop

`POST /api/v1/reviews/{id}/cancellation`

Same mutation headers as start: session cookie, `Origin`, `X-CSRF-Token`, `Idempotency-Key`. Body is empty.

Who: `authorize_start` for the review’s repository. That call only checks `has_read_access`. Map `VCS_ACCESS_DENIED` to `403 ACCESS_DENIED`. Do not read the pull request or the diff, and do not set `integration_status = bot_denied`. If `integration_status` is already not `healthy`, return `409 INTEGRATION_UNHEALTHY` and do not update the row.

Unknown review, or a review this person cannot see, is `404`.

`ReviewJob` has no reason column. One transaction updates the job to `status = FAILED` and `finished_at = now()` only where `finished_at IS NULL`, and calls `append_review_event` with `reason_code = USER_CANCELLED`, `retryable = false`, and `publication_status = NOT_READY`. `append_review_event` copies the job status. The idempotent `200` reads that event. `add_start_review` already inserted `Publication` as `NOT_READY`, and review detail inner-joins it, so that row stays `NOT_READY`. Do not call `_prepare_publication`. Leave the `starts` quota row unchanged. The active count drops because `finished_at` is set.

Outcomes:

- Update matched: `200` with `review_id`, `status = FAILED`, `reason_code = USER_CANCELLED`.
- No match, and the terminal event is already `USER_CANCELLED`: `200` with that same body. Nothing is written again.
- No match, and the review finished any other way (`COMPLETED`, `PARTIAL`, `SKIPPED`, or `FAILED` for another reason): `409 REVIEW_NOT_CANCELLABLE`.

A same-key replay returns the outcome already stored for that key. A new key after a successful cancel still returns the `200` above.

Add `USER_CANCELLED` to `ReviewReasonCode`, to the OpenAPI reason enum, and to OpenAPI `ErrorCode` as `REVIEW_NOT_CANCELLABLE`. Add `POST /api/v1/reviews/{id}/cancellation` to `openapi.yaml` and to the exact route set in `test_openapi_is_valid_and_contains_exact_required_route_surface`. No migration. Review detail already returns `error` when the latest event has the same status as the job and a known reason code. Do not add a user id.

### Worker

Do not close the HTTP call to the model.

The job row is `FAILED` with `finished_at` set. The reason is only on the event.

`_ensure_call_authority` and `update_status` raise `LeaseLostError` once `finished_at` is set. `PostgresReviewWorker.run_once` does not acknowledge that exception. A check that only raises does not finish the delivery.

Before `_ensure_call_authority`, before each `llm.review`, and after fetch before the next `_transition`: if `finished_at` is set, return `DurableTaskOutcome.COMPLETED` and insert nothing. Do not call the model. A non-last chunk that already committed stays. Do not call `_finish` or `_prepare_publication`. The status stays `FAILED`.

A later analyze delivery is acknowledged in `PostgresReviewWorker._process_delivery` when `PostgresWorkerTasks.completed` sees analyze plus `FAILED`.

If Stop loses the race and the review has already finished another way, cancel returns `409`. The worker must not overwrite a cancel that committed first: the chunk insert and `_finish` run only while `finished_at` is still null.

### UI

On the review page, next to the progress block:

- **Stop** while status is `QUEUED`, `FETCHING_DIFF`, `PARSING_CONTEXT`, or `LLM_PROCESSING`. It calls the cancel endpoint, then the open review refreshes.
- **Run again** only when the review is settled and the pull-request query has returned an open pull request. Hide it when the pull request is missing or the query failed. `StartReviewInput` and `startReview` currently post only `{ requested_head_sha }`, and `useStartReviewMutation` fingerprints `repositoryId|number|headSha`. Add optional `rerun_of` to the input and the JSON body, and include it in the fingerprint, because `start_review_http` hashes `rerun_of` into the idempotency body. The button sends the pull request’s current `headSha` and `rerun_of` set to this review id, then opens the new review.

`ReviewProgress` shows the existing failed status label and the new reason line «остановлено человеком».

Start review on the pull-request list stays. Publication retry stays unwired, as it is today.

## Out of scope

Refunding the hourly start. A new status. Storing who pressed Stop. Aborting the provider socket. Changing publication retry. Starting the local compose worker (it still sleeps). Staging deploy changes.

## Tests

API, through the HTTP handler and the analysis handler:

- Queued review becomes `FAILED` / `USER_CANCELLED`, `finished_at` set, start quota unchanged, active count down by one.
- Second cancel returns `200` and does not add a second event.
- Completed review returns `409`.
- Saved chunk, then cancel, then the handler’s next pass: no further model call, chunk remains, status stays `FAILED`, publication stays `NOT_READY`.
- Model call returns after cancel committed: that chunk is not inserted.

UI:

- Reason line for `USER_CANCELLED`.
- Stop is shown only for an unfinished status and posts the cancel call.
- Run again is shown only for a settled review with an open pull request, and the start body includes `rerun_of`.
