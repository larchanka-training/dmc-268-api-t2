# GitHub review publication and SSE (#24)

This sprint supports GitHub. GitLab is deferred. Publication consumes retained
analysis; it never calls the LLM. Analysis and publication have separate states.

## Publication boundary

`GithubPublisher.publish(review_job_id=...)` loads the saved summary, coverage,
findings and SHA-pinned contexts through `PostgresPublicationStore`. It validates
anchors against those contexts, maps OLD/NEW to LEFT/RIGHT, preserves multiline
endpoints and uses the new path for a renamed file. Findings outside reviewed
coverage or without a valid changed-line anchor remain in the Overview.

Mixed diffs record coverage in NEW coordinates. An OLD inline range must be
backed by covered NEW additions in each contiguous change block it touches;
unchanged context separates blocks. This preserves OLD/NEW line offsets and
does not treat another covered block in the same hunk as proof. If a touched
deletion block has no NEW replacement, its coverage cannot be established this
way and the finding stays in the Overview. Deleted-only files retain their
OLD-coordinate coverage check.

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

Run the publisher explicitly against the same PostgreSQL and Redis as the
analysis worker, after applying migrations and supplying the write credentials:

```bash
uv run python -m app.publication_worker --once
uv run python -m app.publication_worker --poll-interval 1
```

`--once` performs one bounded dispatch/receive pass. Continuous mode polls new
publish deliveries and stops cleanly on SIGTERM/SIGINT. No analysis runtime
manifest or LLM credential is required by this process. It uses the persisted
canonical result; the analysis worker and its dispatcher are separate processes
described in `docs/examples/analysis-runtime.md`.

`PostgresPublicationDispatcher` sends only useful finished analyses with
`Publication=PENDING`, a future publication deadline and a valid canonical
outbox envelope. It rechecks eligibility under database locks, respects
quarantine, active leases and unresolved operations on the same PR, and reserves
a 90-second dispatch cooldown before broker I/O. The existing review worker owns
the publish lease, heartbeat, durable terminal proof and ACK.

The CLI does **not** run analyze-only pending recovery against the publish
stream. Deliveries left pending by a crash or `FAILED/UNKNOWN` stay unacknowledged
for explicit operator/queue-owner recovery. The continuous process logs only a
safe deferred error type and continues with new deliveries; it never infers
permission for another POST. `--once` exits unsuccessfully for these errors.
Reconciliation still requires a separately granted live read-only authority;
this CLI does not allocate it or expose a retry API.

The default Compose `worker` service remains the existing sleep placeholder.
Starting the API or updating its image does not activate either review worker.
Enable the dedicated processes in the target environment as an explicit deploy
step; browser OAuth also requires its own runtime auth settings.

## Integrated dependencies (18 October 2026)

The implementation is combined with main `6bc9542`, including merged PR #29
(queue, durable analysis runtime, sessions and OAuth), #31 (versioned findings,
rule references and reflection), and #32 (Fernet configuration).

- Preserve the accepted migrations `0001`–`0014`. The previously unmerged #24
  event and operation migrations are now `0015` and `0016`; existing deployed
  migrations are not renumbered. Recreate any disposable database that applied
  the old draft #24 `0008/0009` revisions before testing this branch.
- Analysis transitions and retry audit use `append_review_event` in the same
  unit of work, preserving phase, execution attempt, reason and safe details.
  Publication preparation appends `PUBLICATION_PENDING` atomically with its
  state change; the analysis handler keeps its existing publish outbox enqueue.
- The publisher reuses the current worker, task-kind lease and durable ACK proof.
  Publication transport is separate from the analysis dispatcher: it sends only
  canonical `publish` commands and does not activate analyze recovery for them.
- Both v1 findings and retained v2 `rule_ref` are supported. A saved rule
  reference is included in the inline body; it is never invented by Publisher.
- SSE uses the merged JWT-cookie/PostgreSQL session verifier. No authentication
  override is required in production.

The old pinned-#29 event-writer patch has been removed: its changes now live in
these modules and are checked against the merged dependency, not a scratch-only
copy. The UI nginx companion PR #24 was merged on 14 October.

## SSE contract

`GET /api/v1/reviews/{review_id}/events/stream` is additive. The existing
`/events` JSON contract is unchanged. The stream returns `text/event-stream`,
`Cache-Control: no-cache` and `X-Accel-Buffering: no`.

The `get_current_user` dependency calls the shared `get_verified_session` verifier
using a short database session. The cookie JWT, active session, expiry/revocation
and GitHub identity are checked before streaming and during delivery. Repository
access is checked on initial read and every poll. No auth connection or database
transaction is held for the stream lifetime. Session/access loss closes an
already started stream; database failure before headers remains a safe `503`.

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

## Resolving a parked UNKNOWN (operator runbook)

A lost POST response parks the publication in durable `UNKNOWN` and expires the
lease; no production path ever re-enters that write. The operator CLI is the
way to resolve it without hand-written SQL:

```bash
python -m app.publication_reconcile <review_id> [--owner reconcile-<name>]
```

The command grants an operator claim over the parked lease
(`PostgresTaskLeaseStore.acquire_reconciliation`: allowed only for a completed
review whose publication and operation are both `UNKNOWN`; the fencing token
advances past the operation, so any previous owner loses authority), then runs
the reconciliation seam. It only reads the provider (list reviews/comments,
never a POST) and resolves the durable outcome to `PUBLISHED`, `PARTIAL`, or
`FAILED` (with confirmed absence). Output is one JSON line with the outcome;
exit codes: `0` — resolved, `3` — still `UNKNOWN` (remote absence alone cannot
release the PR-scoped operation; retry later), `1` — error or nothing to
reconcile (not `UNKNOWN`, unknown review). The parked queue delivery is not
touched; after a resolved outcome every later worker pass is a no-op.

## Verification

Use the repository's Ruff, mypy and pytest commands. Set
`DMC268_TEST_DATABASE_URL` to a dedicated PostgreSQL 18 database to run lease,
publication, concurrency, event replay and HTTP integration tests. Apply/check
migrations and run their downgrade/reapply cycle only on that disposable DB.
Set `DMC268_RUN_DOCKER_TESTS=1` to include the production-image check.

Tests replace the GitHub HTTP boundary with GitHub-shaped responses and use real
services and PostgreSQL. Lost POST responses, stale PRs, actor/commit/range
proof, paging, fencing, partial output, reconciliation, auth expiry and replay
are covered. A real-PR acceptance run requires running the integrated processes,
configuring session auth and a bot credential, and an approved target PR.
Local tests do not claim that external acceptance step was executed.

GitHub API reference: [pull request reviews](https://docs.github.com/en/rest/pulls/reviews),
including `COMMENT`, explicit `commit_id`, inline coordinates and repository
permissions. The conservative reconciliation policy above is the application's
decision for ambiguous writes.
