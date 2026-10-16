# GitHub review publication and SSE (#24)

This sprint supports GitHub. GitLab is deferred. Publication consumes retained
analysis; it never calls the LLM. Analysis and publication have separate states.

## Publication boundary

`GithubPublisher.publish(review_job_id=...)` loads the saved summary, coverage,
findings and SHA-pinned contexts through `PostgresPublicationStore`. It validates
anchors against those contexts, maps OLD/NEW to LEFT/RIGHT, preserves multiline
endpoints and uses the new path for a renamed file. Findings outside reviewed
coverage or without a valid changed-line anchor remain in the Overview.

The Overview contains the summary, checked files, risks, skipped files and
context quality. At most 20 unique fingerprints become inline comments, ordered
by severity and location. Text is masked again before publication. This limit
does not discard findings from the retained analysis or hide omitted comments.

Before HTTP, the store persists a review marker and payload hash and serializes
operations by ChangeRequest. An unresolved operation cannot be replaced by a
different review even if its worker lease expires. Every provider request,
including each pagination request, checks the live publish lease and fencing
token. Database transactions finish before provider I/O. The POST uses a
`COMMENT` review with the captured head SHA as `commit_id`.

| Outcome | Meaning / recovery |
| --- | --- |
| `PUBLISHED` | Overview and all selected comments confirmed remotely. |
| `PARTIAL` | Overview confirmed; some selected comments absent. No automatic completion. |
| `SKIPPED` | PR closed or its captured base/head no longer current. |
| `FAILED` | Explicit provider rejection, or reconciliation proved absence. |
| `UNKNOWN` | Remote effect cannot be established. Reconcile before any retry. |

Proof uses the review marker, configured bot ID, captured commit, review state,
and each comment's fingerprint, path, side and range. Provider IDs are retained
in publication metadata. Human text with the same marker is not bot output.
Repeated delivery of a finished cycle returns its retained outcome without HTTP.
Fingerprint deduplication covers a review payload; a new analysis is a new cycle.

`GithubPublisher.reconcile(review_job_id=...)` is a **read-only GitHub operation**
under a fresh publish lease with a higher fencing token. It can confirm presence
by reading complete review/comment lists. An empty read leaves `UNKNOWN`
unresolved: the timed-out POST could still finish after that read. Absence also
cannot release an abandoned `IN_FLIGHT` request. The store's explicit
`confirmed_absent` recovery boundary requires independent proof that the old
request can no longer take effect; an empty list alone is insufficient. Neither
reconciliation nor delivery replay issues a second POST. Queue recovery must
explicitly prepare any permitted retry; retained analysis is reused.

## Credentials and worker composition

Set `GITHUB__WRITE_TOKEN` and the numeric `GITHUB__BOT_USER_ID`. The read credential
`GITHUB__TOKEN` is never used as a fallback. The token must grant access to the
target repository and Pull requests write permission. An installation-token
provider and refresh lifecycle are deployment responsibilities; this module
accepts an already provisioned credential and does not mint tokens.

`make_publication_handler(settings, sessions, completed_outcome)` owns the HTTP
client and creates the real store, publisher and handler. Supply the worker's
actual `DurableTaskOutcome.COMPLETED` singleton; the worker compares identity.
Keep the context open while the handler is used. Credentials are checked before
opening the client. Route only `task_kind="publish"` envelopes to this handler.

The handler returns completion only for `PUBLISHED/PARTIAL/SKIPPED`. For
`FAILED/UNKNOWN` it raises `PublicationRecoveryRequiredError`; it does not ACK or
invent recovery. The queue owner must provide pending recovery and dead-letter
policy. The existing webhook worker is an intake worker, not a review publisher.
No publication worker is started implicitly by importing the API.

## PR #29 integration

Compatibility baseline: PR #29 head
`1a1361d5cd997dbc43aeae9b8d98e13a39f0d295`, based on main
`23b80ff3d34b0be6a84b2fa18533b1bd1dcd2350`. It is a dependency; its analysis,
queue and authentication implementation is not copied into main by #24.

When combining the changes:

The [event writer bridge](integrations/pr29-event-writers.patch) is a reviewable
patch against that pinned #29 head after combining #24. It contains the writer
changes and a real Outbox → Redis → publication worker → durable ACK regression.
Check it with `git apply --check docs/integrations/pr29-event-writers.patch`
before applying. Recheck the patch if #29 changes; it does not merge #29 or
retarget migration `0008` automatically.

1. Preserve #29's `TaskLease.task_kind`, review routes and repositories. Retarget
   migration `0008` from `0005` to #29's `0007`; `0009` remains after `0008`.
   Main alone intentionally has `0005 → 0008 → 0009`.
2. Replace #29's direct `ReviewEvent(...)` inserts in analysis transitions,
   failures and format repair with `append_review_event` in the same transaction.
   Preserve internal phase/attempt/details and record the publication status at
   that transition. A raw insert without `sequence` fails after migration `0008`.
3. Append the publication `PENDING` event in the same unit of work as
   `PublicationRepository.prepare`. Keep the existing atomic publish-outbox
   creation; do not enqueue again in the publisher.
4. Inject the publication handler into the existing `TaskHandlerPort` dispatch,
   reusing worker leases and heartbeat. The completion result is compatible with
   the worker's existing durable proof. Recovery for `FAILED/UNKNOWN` is still a
   queue integration requirement.
5. Bind SSE authentication to an active PostgreSQL session as described below.
   Real session authentication and the production analysis composition root are
   prerequisites for automatic end-to-end operation.

## SSE contract

`GET /api/v1/reviews/{review_id}/events/stream` is additive. The existing
`/events` JSON contract is unchanged. The stream returns `text/event-stream`,
`Cache-Control: no-cache` and `X-Accel-Buffering: no`.

The default `get_current_user` dependency fails closed with `401`. Override it
with the application's session dependency returning
`StreamPrincipal(user_id, revalidate)`. `revalidate` must check session expiry,
revocation and the active user against PostgreSQL; accepting an arbitrary UUID
or relying on the browser alone is insufficient. Repository access is checked
on initial read and each poll. Access loss closes an already started stream.

Events:

- `review.snapshot`: current analysis/publication status and durable high-water
  sequence. It has no SSE `id`, so a snapshot cannot advance the replay cursor
  past historical events that the client has not processed.
- `review.event`: a committed historical transition, with
  `id: v1:<review UUID>:<sequence>`, event UUID/time, statuses, safe reason and
  retryability. Historical publication status can be null for pre-migration
  events; it is not reconstructed from today's status.
- `: heartbeat`: an unpersisted comment every 15 seconds. Polling interval is one
  second and transactions are short; no connection is held for the stream.

Use `Last-Event-ID` to resume. Invalid/cross-review cursors return `422`; a future
cursor or unavailable contiguous history returns `409 EVENT_REPLAY_UNAVAILABLE`.
Reconnect without a cursor to resync. The current snapshot precedes replay;
clients should keep snapshot state separate from historical transition display.
Terminal analysis and terminal publication close the stream after replay;
failed/skipped analysis also closes when publication remains `NOT_READY`.

Only safe public fields are streamed. Source, prompts, provider payloads,
attempts, phase and internal details are excluded. All writers allocate
per-review sequences through `append_review_event` under the ReviewJob row lock
in the same transaction as the state change. If retention is introduced later,
retain the latest sequence anchor or introduce a durable counter before deleting
all history, otherwise a sequence could be reused. No event purge is added here.

The UI nginx location disables buffering/cache and has a 60-second read timeout
(greater than heartbeat). It forwards cookies and `Last-Event-ID` to the API.

## Verification

Use the repository's Ruff, mypy and pytest commands. Set
`DMC268_TEST_DATABASE_URL` to a dedicated PostgreSQL 18 database to run lease,
publication, concurrency, event replay and HTTP integration tests. Apply/check
migrations and run their downgrade/reapply cycle only on that disposable DB.
Set `DMC268_RUN_DOCKER_TESTS=1` to include the production-image check.

Tests replace the GitHub HTTP boundary with GitHub-shaped responses and use real
services and PostgreSQL. Lost POST responses, stale PRs, actor/commit/range
proof, paging, fencing, partial output, reconciliation, auth expiry and replay
are covered. A real-PR acceptance run still requires the integrated worker,
active session auth, a provisioned bot credential and an approved target PR.
Local tests do not claim that external acceptance step was executed.

GitHub API reference: [pull request reviews](https://docs.github.com/en/rest/pulls/reviews),
including `COMMENT`, explicit `commit_id`, inline coordinates and repository
permissions. The conservative reconciliation policy above is the application's
decision for ambiguous writes.
