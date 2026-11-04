# Repository analytics, review audit and existing quotas

Companion API for [UI #31](https://github.com/larchanka-training/dmc-268-ui-t2/issues/31).
The source of the wire contract is `openapi.yaml`.

All endpoints require the existing cookie session and `RepositoryAccess` for the
requested repository. Missing and inaccessible repositories both return safe
`404 RESOURCE_NOT_FOUND`; anonymous requests return `401`. These are read-only
projections and do not start an analysis or call GitHub/LLM providers.

## Period and navigation

`GET /api/v1/repositories/{id}/reviews` and `/analytics` accept `days=7|30|90`
(default 30), `date_from` and `date_to` as ISO calendar dates. Dates are inclusive
UTC dates. `date_to` defaults to today; `date_from` defaults to `days - 1` days
before it. An explicit range must contain 1–366 days. Runs are selected by their
`created_at`, not by completion or finding creation time.

History also accepts `status`, `pull_request_number`, `limit` (1–100, default 20)
and an opaque `cursor`. Ordering is `created_at DESC, id DESC`. The cursor is
bound to repository, resolved dates, status and PR filter. Status transitions of
the anchor run do not invalidate pagination. Changing filters requires restarting
pagination, as does crossing a UTC day boundary with a relative `days` period;
explicit dates keep the period fixed. Invalid input returns canonical
`422 VALIDATION_FAILED`. This is live keyset pagination, not a frozen snapshot.

## Audit row

Rows preserve separate analysis `status` and `publication_status`. They include
PR number, requested head SHA, creation/start/finish timestamps, `trigger_type`,
and an optional actor `{id, login}`. A missing identity/login stays null. No
credentials, raw provider metadata, prompts or source text are projected.

`usage` comes from **all durable `ReviewLlmAttempt` rows** for the run, including
failed attempts and repair/retry calls. It deliberately does not reuse the review
detail endpoint's chunk-result usage, which is not a complete billing ledger.

- `input_tokens`/`output_tokens`: sum of known actual usage for each dimension;
  null when none is known. Zero means a measured zero.
- `reserved_input_tokens`/`reserved_output_tokens`: conservative allowances for
  calls with unknown actual usage in that dimension, including settled failures.
  These are not measured charges and must not be added to an actual-token label.
- `status=complete`: every recorded attempt has both actual dimensions;
  `incomplete`: some are unknown, so known totals are lower bounds;
  `unavailable`: there are no durable attempts (for example a queued or historical
  run). Historical usage is not backfilled or assumed to be zero.

`limit_reasons` contains unique `file_limit`, `line_limit`, `token_budget` reasons
from saved coverage, in that order. Empty means no such saved reason, not proof
of full coverage. These indicate skipped analysis coverage. They do not assert
that a whole PR was rejected by admission or a daily token quota was exhausted.

`cost={amount:null,currency:null,status:"unavailable"}` is intentional. The user
approved delivering only existing limits for this slice. Monetary rate policy,
price snapshots and daily token quotas remain dependencies of
[UI #28](https://github.com/larchanka-training/dmc-268-ui-t2/issues/28); neither a
tariff nor daily enforcement is invented here. UI should display “Unavailable”,
not a free or zero-cost run.

## Analytics

The response includes zero-filled UTC daily buckets for all four canonical
categories: `security`, `correctness`, `performance`, `maintainability`. These
are saved finding counts **per run**, across runs selected by creation date,
without deduplicating across repeated reviews. They are not a count of unique
bugs or a measured code-quality score. `maintainability` is not silently renamed
to `Style` and `performance` is not discarded.

`totals.reviews` counts selected runs; `totals.findings` counts their saved
findings. `average_duration_ms` averages `finished_at - started_at` only for
COMPLETED/PARTIAL runs with both timestamps and a nonnegative interval. It
excludes queue waiting and publication time. `duration_sample_count` gives the
denominator; an empty sample produces null, never zero. Counts and durations
are independently aggregated to avoid multiplying durations by finding count.
The response is a live operational projection, not an immutable financial report.

## Quotas

`GET /api/v1/repositories/{id}/quotas` reads current repository settings and the
same counters used by admission:

- `starts_per_hour`: repository + signed-in user, current UTC hour; includes
  limit, used, nonnegative remaining and `resets_at`.
- `active_jobs`: all unfinished jobs for the repository, independent of actor;
  includes limit, used and nonnegative remaining. It has no clock reset.
- `daily_tokens={status:"unavailable"}`: no daily limit is implemented here.

The snapshot is advisory. The start-review endpoint remains authoritative under
concurrency and may return existing `429 QUOTA_EXCEEDED`. That error does not
identify a daily-token failure. No settings-editing endpoint is added.

## Verification

`tests/test_repository_insights_api.py` exercises the HTTP surface with real
PostgreSQL: auth/access isolation, filtered keyset pagination and status changes,
known/unknown attempt accounting, per-category aggregates and duration samples,
safe coverage warnings, and current hourly/active quota semantics. Successful
responses are validated against the checked-in OpenAPI schemas.
