# Incremental review, exact cache and input guards (dmc-268-ui-t2#33)

Normative product policy: `INCREMENTAL_REVIEW_AND_LIMITS_SPEC.md`
(dmc-268-ui-t2 PR #42). This file describes the API implementation.
All three capabilities are off by default and roll back by flag.

| Flag | Scope |
|---|---|
| `INPUT_GUARDS__ENABLED` | analysis worker: pre-LLM file skips |
| `INCREMENTAL__ENABLED` | analysis worker, API admission, publication worker and reconcile |
| `REVIEW_CACHE__ENABLED` | analysis worker: exact result cache in Redis |

## Input guards

`parse_diff(..., guards=...)` (`app/vcs/input_guards.py`) skips a file, cheapest
check first, and records an enumerated `FileSkipReason`:

1. Diff metadata: `removed`, `pure_rename`, `binary`, `too_large`
   (patch section over `max_patch_bytes`, default 1 MiB).
2. Path: repository `ignores.globs` → `ignored`; `include_globs` then bypass the
   defaults; `linguist-generated` / `linguist-vendored` from `.gitattributes`
   at the **base** commit (ADR-0002); lock files, `migration` (only with
   `exclude_migrations`), generated code, bundles/minified assets, vendored
   code, binary extensions.
3. Diff text: NUL bytes in the first `content_probe_bytes` → `binary`;
   generation markers on comment lines of the first `generated_header_lines`;
   minified text (long lines with almost no whitespace).

The context wire keeps the canonical `reason: "policy"`; `ReviewJob.coverage`
keeps `detail` (the public API projection ignores it). The 50-file / 2000-line
admission limits count only files that survive the guards. The overview comment
lists skipped files grouped by reason. Per repository, `RepositorySettings.ignores`
may set `include_globs`, `exclude_migrations`, `max_patch_bytes`,
`minified_line_length`, `minified_avg_line_length` (`schemas/settings/v1.json`).

## Incremental review

* **What is sent.** The PR diff (`merge_base…head`) is split into fragments at
  definition boundaries (`app/review/incremental.py`). A fragment whose identity
  appears in a prior review of the same PR is omitted (`already_reviewed`).
  Priors: same change request, same `config_digest`, analysis
  `COMPLETED`/`PARTIAL` and publication `PUBLISHED`/`PARTIAL` (an unpublished,
  e.g. stale-skipped, result never reached the author), latest
  `INCREMENTAL__MAX_PRIOR_REVIEWS`. Kept fragments stay in PR-diff
  coordinates, so comments anchor to the current head. Nothing left →
  `SKIPPED`, no LLM call.
* **`delta_mode`.** `full` without priors; otherwise Compare API
  `prior_head...head`: `ahead`/`identical` → `incremental`, anything else or
  unknown → `force_push`. Only a metric: fragment identity already makes
  rebases and force-pushes safe.
* **One active review per PR.** `startReview` answers `409 REVIEW_ALREADY_ACTIVE`
  ("A review of this pull request is already active.") while any review of the
  PR is unfinished; the in-flight review is never cancelled. The change-request
  row is locked exclusively for this check.
* **System follow-up.** After a terminal analysis, `SystemFollowUp`
  (`app/adapters/follow_up.py`) reads the live head with the bot token; if it
  moved, it revalidates the initiator's current GitHub permission with their
  own stored credential (`StoredCredentialAuthorizer`; a missing credential
  refuses), then admits one start for the latest head through `persist_start`
  (`trigger_type = system_follow_up`, same initiator and quotas,
  `rerun_of` = latest finished COMPLETED/PARTIAL review). Idempotency scope
  `systemFollowUp:{review_id}` turns repeated terminal deliveries into replays.
  Failures are logged and never block the analysis ACK.
* **Cross-cycle repeats (#53).** The publication store loads fingerprints that
  earlier cycles of the PR posted (`provider_metadata.comment_ids` of
  `PUBLISHED`/`PARTIAL` publications); they are not posted again and the
  overview reports "Уже отмечены в предыдущих ревью: N".
* **Webhook redelivery** stays deduplicated by `(provider_name, delivery_id)` in
  PostgreSQL; the webhook never calls the LLM.

## Exact review cache

`app/review/cache.py`, Redis key `review:cache:v{KEY_VERSION}:{sha256}`, TTL
`REVIEW_CACHE__TTL_SECONDS` (default 14 days). The hash covers the files with
hunks relative to their file's first hunk (line kind and right-trimmed text),
paths, status, output language, related symbols and `config_digest` (model,
prompt, rules, settings version). Changing any of those misses by key. PR
title, body, identities and SHAs are not part of the key.

Value: schema version, summary, limitations, original usage and findings
relative to their file's first hunk; a hit restores them at the current lines,
re-validates against the current diff (a mismatch is a miss) and reports usage
0/0. Only valid results are stored. Single-flight: `SET {key}:lock NX EX`,
other callers poll for the result up to `wait_seconds`, then call themselves.
Every Redis error is fail-open (`cache_error` metric, warning log).

Semantic (embedding) cache is not implemented: no embedding provider exists.

## Metrics

Structured log events `metric` (`app/core/metrics.py`):
`files_skipped_total{reason}`, `delta_mode{mode}`, `llm_calls_total`,
`cache_hit{kind}`, `cache_miss{kind}`, `cache_rejected{kind}`,
`cache_error{kind,operation}`, `llm_tokens_saved_total{kind}` (usage of the
cached original call), `follow_up{outcome}`.
