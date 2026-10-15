# Pipeline specification — DMC-268 API

This document is authoritative for asynchronous pipeline, lifecycle, retry,
degradation, queue, lease, dead-letter, replay, and publication-recovery semantics.
The HTTP contract and its request/response DTOs are owned exclusively by
`openapi.yaml` and are not repeated here.

## 1. Review lifecycle

`ReviewJob.status` is the only current lifecycle field:

```text
QUEUED
  -> FETCHING_DIFF
  -> PARSING_CONTEXT
  -> LLM_PROCESSING
  -> COMPLETED | FAILED
```

`PARTIAL` and `SKIPPED` are terminal alternatives. `PARTIAL` means useful,
validated output remains but is incomplete. `FAILED` means there is no usable
result or mandatory trustworthy input is unavailable. `SKIPPED` is an intentional
non-run caused by a lifecycle condition.

There is no current `stage`. Validation is internal to `LLM_PROCESSING`.
`ReviewEvent.phase` may record an active phase and is nullable for queue,
terminal, and general events.

Every terminal transition sets `status` and `finished_at` in the same SQL
`UPDATE` or ORM flush. The PostgreSQL CHECK is immediate, so two statements in
one transaction are not sufficient.
Active states require `finished_at IS NULL`; terminal states require it to be set.
Terminal jobs are never silently reopened.

## 2. Errors, retry, and degradation

Accepted reason codes are:

```text
QUEUE_DEADLINE_EXCEEDED
VCS_RATE_LIMITED
VCS_UNAVAILABLE
VCS_ACCESS_DENIED
PR_STALE_OR_CLOSED
DIFF_UNTRUSTWORTHY
SOURCE_BLOB_UNAVAILABLE
AST_PARSE_FAILED
LLM_TIMEOUT
LLM_UNAVAILABLE
LLM_OUTPUT_INVALID
ANALYSIS_DEADLINE_EXCEEDED
```

Only `VCS_RATE_LIMITED`, `VCS_UNAVAILABLE`, `LLM_TIMEOUT`, and
`LLM_UNAVAILABLE` use generic retry. A retryable failure preserves the current
active status; `retry_at` and the deadline fields control rescheduling.

`LLM_OUTPUT_INVALID` permits exactly one format-repair attempt. Format repair is
not a generic retry and does not reset analysis deadlines.
Provider-level retry/failover and persisted worker retry share the same analysis
deadline and request budget; their attempt counts must not multiply silently.

`ReviewEvent` is the append-only audit record for transitions, retry,
degradation, and failure. Each event stores the current status, nullable phase and
reason code, `retryable`, an attempt number starting at one, and optional safe
details. Safe details never contain source, prompt, model reasoning, secrets, raw
provider payloads, or stack traces. `ChunkResult` and `Finding` contain only
validated analysis output.
New reason-code writes use the accepted enum. Historical unknown reason codes
remain in PostgreSQL audit records, but their public projection is `null`, never
the raw legacy value.

## 2.1 Context and result validation boundary

The context wire envelope includes `schema_version`, `review_id`, and `chunk_id`;
`ContextPayload.payload_body` stores only snapshot, metadata, files, related
symbols, coverage, and budget. The backend assembles the wire envelope from
authoritative columns, validates the JSON Schema, then runs semantic validation
before inference or persistence. The public domain helpers in
`src/app/domain/contract_validation.py` are `validate_ranges(payload)` and
`validate_hunks(hunks, public=False)`; HTTP diff projection uses `public=True`
for its `old_lines`/`new_lines` count names. They check ordered 1-based ranges,
hunk counts and exact contiguous old/new coordinates; path normalization is a
separate input invariant. A zero-length hunk side may start at zero; a nonempty
side must start at one or later. New contexts use output language `ru`, while a
frozen legacy settings version may retain another nonempty language value.

The LLM generates only summary, findings, and limitations according to
`schemas/llm-output/v1.json`. The gateway must bundle its local Finding `$ref`
before passing a structured-output schema to a provider. The backend validates
and maps model output to `ReviewChunkResult`, adding the schema version, measured
gateway latency, and usage only when both token counts are available. Review/chunk
identity remains authoritative in context and persisted aggregates; status is a
ReviewJob field, while provider/model provenance, request counts, and safe errors
are backend-owned outside the model output. Any gateway adapter must
align its typed models with the checked-in schemas and shared fixtures in
`schemas/examples/`; textual equality with a separately generated schema is
not the wire contract.

## 3. Deadlines and budgets

- Queue deadline: 15 minutes from creation.
- Analysis deadline: 10 minutes including generic retries and format repair.
- Publication deadline: 5 minutes for starting attempts in a publication cycle.
  It forbids new external publication attempts and PENDING/progress writes after
  expiry, but does not discard the outcome of an attempt started before expiry.
  Recording that outcome still requires the original live publish lease.
- Context and model token budgets are fixed in the persisted ContextPayload.

Exceeding a queue or analysis deadline records the corresponding reason event
and applies terminal semantics based on whether useful validated output exists.
Publication expiry blocks new attempts while preserving the ability to record
an in-flight outcome under its live lease. Retry scheduling must never extend an
accepted deadline.

## 4. Transactional outbox and Redis queue behavior

Creation of a review, its initial publication state, quota reservation, and the
initial analyze `OutboxEvent` belongs to one transaction. Completion of analysis
and creation of publish work also belongs to one transaction.

The normal queue envelope is validated against
`schemas/queue/task-envelope.v1.json`. It contains only schema/event/review/task,
attempt, and trace identity. Source, diff, prompt, and model result are never sent
in the envelope.

`RedisQueue` implements enqueue, receive, acknowledgement and safe dead-letter
quarantine. `PostgresReviewWorker` automatically renews its PostgreSQL lease
while the handler executes. Pending-delivery claiming and periodic review
recovery are not implemented. The v1 Redis-backed queue adapter and workers must
provide these observable guarantees:

- separate analyze, publish, and dead-letter task kinds/channels;
- acknowledged enqueue before marking the PostgreSQL outbox event published;
- at-least-once processing and acknowledgement only after a durable processing
  decision, with recovery of unacknowledged/claimed work;
- normal envelope maximum size 16 KiB and task expiry after 20 minutes;
- dead-letter retention for seven days;
- analyze concurrency 1 and publish concurrency 4;
- recovery interval 30 seconds;
- worker heartbeat 15 seconds and lease duration 90 seconds;
- at most three generic transient attempts per active phase;
- no independent framework-level autoretry outside the persisted retry budget;
- VCS request timeout 15 seconds;
- Ollama request timeout 120 seconds.

The Redis enqueue acknowledgement is not proof that a job completed. PostgreSQL
outbox/job/lease state remains authoritative; recovery re-enqueues eligible work
when a message is lost after enqueue. Analyze recovery must not revive a terminal
job; publish recovery may operate on a completed analysis without reopening it.

The v1 serializer emits only the canonical envelope fields. V1 consumers ignore
unknown optional fields. Breaking envelope changes require a new major version
and corresponding consumer.

The dispatcher selects only analyze outbox records that are not quarantined. Publish
commands remain parked in PostgreSQL with NULL transport markers. It marks `broker_published_at` only after the queue adapter confirms Redis enqueue.
Consumers reject unsupported schema versions, unknown task kinds, and otherwise
invalid envelopes before domain work begins.

### 4.1 PostgreSQL outbox quarantine

PostgreSQL outbox quarantine is implemented by `PostgresOutboxDispatcher`.
It does not change the canonical queue or dead-letter JSON Schemas.

An authoritative `OutboxEvent` whose canonical envelope is permanently invalid
is retained in PostgreSQL with nullable `quarantined_at` and
`quarantine_reason_code`. The only accepted reason codes are
`UNSUPPORTED_SCHEMA_VERSION`, `UNKNOWN_TASK_KIND`, and `INVALID_ENVELOPE`, shared
with the canonical queue/dead-letter contract. Canonical validation occurs before
Redis I/O and reports these known failures with a typed validation error. Other
errors, including unknown `ValueError` sources, Redis failures, and PostgreSQL
failures, propagate without quarantining the event or treating it as delivered.

Quarantine sets both metadata fields atomically while preserving the existing
event ID, review/task/trace identity, attempt, schema version, timestamps, and
all other source data. It stores only the accepted reason code, never payloads,
exception text, stack traces, or secrets. A quarantined event keeps
`broker_published_at IS NULL`; a published event has no quarantine metadata.
PostgreSQL CHECK constraints enforce paired quarantine metadata, accepted reason
codes, and mutual exclusion of publication and quarantine. Quarantine writes
conditionally update only an event that is still unpublished and unquarantined;
receipt finalization also checks the reserved generation,
so repeated quarantine is idempotent and concurrent dispatchers cannot commit
both terminal states for the same event.

Quarantine records a producer rejection before Redis enqueue for that snapshot.
It is not a Redis dead-letter record, does not enqueue a Redis DLQ message, and
does not acknowledge any Redis delivery. A successfully quarantined event is
excluded from normal dispatch selection; dispatch continues to later events in
the selected batch. If the quarantine transaction fails, the event remains ordinary
unpublished work and the database error stops the invocation.

Migration `0007` follows `0006`, adds the nullable metadata and constraints, and
preserves existing rows and publication markers. Stop dispatcher writers before
downgrading to `0006`: downgrade removes quarantine metadata and makes previously
quarantined unpublished records eligible for normal selection again. Downgrade
does not provide a validated operator replay operation.

`PostgresOutboxDispatcher.dispatch_batch()` sends at most 100 genuine initial
analyze commands (positive `batch_size`, capped at 100). Genuine initial means
`dispatch_generation = 0`, NULL `broker_published_at` and NULL `broker_message_id`.
Every selection applies analyze-only filtering before ORDER BY/LIMIT. Publish
outbox and Publication(PENDING) remain durable without a runnable publish path.

Migration `0011` adds transport metadata to the existing OutboxEvent:
`broker_message_id`, `dispatch_generation` (default 0), `dispatch_not_before`,
`recovery_window_started_at`, and `recovery_send_count` (default 0), with
nonnegative generation/count and paired window/count CHECKs. Historical published
rows without message ID and new reserved generations without receipt remain valid.
`broker_published_at` is the DB time of the last confirmed enqueue, not completion.

Initial and recovery sends share one persisted gate. Recovery snapshots take no
row locks. Initial selection briefly uses OutboxEvent-only `SKIP LOCKED`, closing
that transaction before reservation. A separate short transaction locks
ReviewJob → TaskLease → OutboxEvent, rechecks
canonical identity, observed generation/receipt/marker, active state, live lease,
retry/deadline and persisted throttle using DB time sampled after locks. It commits
an incremented generation, clears only the current receipt, and sets a 60-second
cooldown before Redis I/O. Pure canonical validation precedes reservation. An
invalid unpublished command is quarantined; an invalid historically published
command is retained with a safe diagnostic. No source command is reconstructed.

`recover_batch()` scans at most 100 previously reserved/legacy analyze candidates
per tick in `(created_at, id)` keyset order, backed by a partial analyze index.
The cursor advances past present, untrusted and deferred events and wraps at the
end of a pass. Fairness is process-local across calls on one dispatcher; fresh
`--once` processes restart the scan. No restart-persistent cursor is claimed.

Known receipts use exact `XRANGE stream id id` outside PG transactions. Canonical
unexpired PRESENT suppresses XADD for both unread and PEL backlog regardless of
lease/retry age. Missing group is recreated with id0 through the existing policy.
Empty XRANGE proves ABSENT. Redis failures or indeterminate responses are UNKNOWN
and cause no reservation/receipt change; mismatched identity/payload is UNTRUSTED
and causes safe diagnostics without ACK/DLQ. Unknown historical/ambiguous receipt
allows only bounded recovery with an uncertainty diagnostic. Expiry is 20 minutes
from the server-generated XADD ID relative to Redis TIME sampled before probe;
a future ID does not expire. Existing expired transport with an expired accepted
job deadline stays on receive/PEL for the existing zero-HTTP terminal handler.
Missing transport can be replaced even after deadline so that handler can run.
Future retry blocks a send unless the accepted queue/analysis deadline expired.

At most 10 recovery reservations commit per tick, and at most 3/event in a persisted
300-second window. Every send after generation0, including a failed first send
with NULL marker, is recovery. Initial sends have separate limit100 and count0.
Rollover begins at current DB time without clearing cooldown; exhausted windows
defer with safe alert. No lifetime cap or catch-up loop is added. Failed/ambiguous
sends and crashes never refund allowance. One reservation permits one XADD command
attempt through an exclusive connection from the existing pool, bypassing hidden
redis-py command retries. Conditional finalize writes returned message ID and
DB-time marker only for the same generation and nonquarantined analyze event;
a late older generation cannot overwrite a newer receipt.

PG transactions have local lock timeout500ms, statement timeout2s, idle transaction
timeout5s and application transaction timeout5s. Neither Redis probe nor XADD
holds a PG transaction. Authority lock contention (SQLSTATE55P03) rolls back and
defers only that candidate; other DB failures propagate. Initial batches may defer
selected busy authority rows; recovery cursor fairness is process-local. Distributed exactly-once delivery is not promised: crash
after XADD and before finalize may require a later bounded duplicate. Existing
worker fencing/accounting, deadlines and full completion proof remain authoritative.
Terminal/incomplete handoff is deferred with a safe alert and never reopened.

The standalone `python -m app.outbox_dispatcher --once` performs one recovery
sweep followed by one initial analyze batch. Existing continuous polling performs
initial batches only; periodic30s recovery and analysis process lifecycle belong
to Stage8C-04. No background process is activated by this change.
It composes the existing `PostgresOutboxDispatcher`, `RedisQueue`, and database
session factory using `POSTGRES__*`, `REDIS__URL`, and `LOGGING__*` settings.
`--batch-size` defaults to 100 and must be a positive integer;
`--poll-interval` defaults to 5 seconds and must
be finite and positive. Every continuous iteration, including an empty batch,
is followed by that interruptible polling delay. Redis PING precedes each batch
so an unavailable broker is not reported as a successful empty dispatch.
Redis connect/command timeouts are 5 seconds; client retries are disabled.
Any other enqueue or database failure exits nonzero with only the error type
logged, preserving the dispatcher's committed progress and retry eligibility.
Logs contain batch counts and lifecycle metadata, never envelopes or exception
text. SIGTERM/SIGINT finish the current bounded batch, prevent a new iteration,
interrupt the polling wait, close Redis, and dispose the SQLAlchemy engine.
Shutdown waits for the current database/Redis operation; operators must allow
the bounded batch to finish before forcibly terminating the process. A forced
termination after XADD and before the PostgreSQL commit can cause a duplicate.

The standard API Docker image contains this module and its runtime dependencies.
With PostgreSQL and Redis configured, an operator can invoke
`docker compose run --rm worker python -m app.outbox_dispatcher --once`
or omit `--once` for manual continuous execution. This command is documented
for future operation; deployment does not start the dispatcher automatically.
Both Compose worker services retain `sleep infinity`. The runtime performs no
analysis, LLM calls, or Redis task ACKs. Production analysis still depends on
the LLM Gateway integration and a separate production worker bootstrap.

### 4.2 Bounded worker execution

`PostgresReviewWorker.run_once()` receives at most one new Redis delivery for
its configured task kind and consumer group. Construction requires a typed
`TaskHandlerPort`; there is no default handler, CLI, service, or production
worker loop. The generic callback supplies no provider call-start authority;
`PostgresAnalysisHandler` adds the checks described in §4.3. Publisher
call-start checks and serialization remain separate.

`RedisQueue.reclaim(consumer_name=..., cursor=..., min_idle_time=90000)` performs
at most one `XAUTOCLAIM COUNT 1`, without JUSTID, on the analyze stream and the
existing `review-workers` group. The caller owns the consumer and cursor, starting
at `0-0`. The returned `QueueClaim` carries the next cursor, at most one delivery,
and diagnostic deleted IDs from Redis 7+/8. An empty batch still advances the
cursor; `0-0` ends a pass. Deleted PEL entries do not prove completion and cause
no synthetic envelope, handler, DLQ, or ACK. Missing groups are created with the
existing `id=0 MKSTREAM` policy. Redis errors propagate without a delivery or an
internal claim retry.

`PostgresReviewWorker.recover_once(cursor=..., min_idle_time=90000)` explicitly
attempts one analyze recovery and returns `(next_cursor, acknowledged)`. It and
`run_once()` share one delivery processing path and one execution slot; no claim
or receive happens while that slot is occupied. A PostgreSQL identity mismatch
remains pending and returns the next recovery cursor, without DLQ. Invalid
transport envelopes retain that cursor through the existing safe quarantine
protocol, DLQ XADD before source XACK. All existing PostgreSQL guards, durable
attempts, retry clocks, ledger and completion proofs apply unchanged. The caller
must retain/advance the cursor even when no delivery was acknowledged. Scheduling
and periodic invocation are deferred to Stage 8C-04; this API starts no service.

The worker validates every canonical envelope field against the PostgreSQL
OutboxEvent and its ReviewJob. Unparseable UUID identity, a missing command, or
a mismatch raises `QueueIdentityError` and leaves the delivery pending; these
are not JSON/schema errors and must not enter Redis DLQ. A NULL
`broker_published_at` is accepted because enqueue precedes the marker commit.
A quarantined PostgreSQL command remains pending without executing its handler:
outbox quarantine is not proof of a Redis processing decision.

Acquisition uses the task-specific lifecycle rules in §5 and a new fencing
token. After acquisition, the worker rechecks command identity, live authority,
and lifecycle/deadline after awaited locks before invoking the callback. The
callback must use transaction-local fenced persistence and commit before
reporting `DurableTaskOutcome.COMPLETED`. An ordinary successful return (`None`),
an intermediate commit, and even a false COMPLETED assertion cannot authorize
ACK. The worker independently reads committed state in a new locked transaction
and verifies its claim. It attempts fenced release in `finally`; lost authority
or unsuccessful release prevents ACK as the current processing success.

The worker's conservative completion proofs are:

- Analyze FAILED/SKIPPED with terminal `finished_at`.
- Analyze COMPLETED/PARTIAL with prepared Publication (non-NOT_READY and
  non-null cycle start/deadline) and a canonical, nonquarantined publish outbox
  command. The handler must commit terminal analysis, preparation, and that
  command atomically, as required by §5. Successor proof includes the same 16 KiB
  serialized UTF-8 envelope bound as Redis enqueue, including JSON escaping.
- Publish PUBLISHED/PARTIAL/SKIPPED with persisted cycle timestamps and
  `finished_at`. These states require no automatic republishing.

An authoritative existing completion proof also permits ACK of a duplicate
without acquiring a lease or executing a handler. If commit succeeded but ACK
failed, redelivery uses this proof rather than repeating the effect. This is
lifecycle-level deduplication, not a new event processing receipt.

All other decisions retain pending work: contention, lease loss, handler errors,
incomplete handoff, expired PENDING, missing publication/deadline, FAILED or
UNKNOWN publication, and other ineligible states. Invalid Redis envelopes alone
use the existing safe Redis quarantine; failure propagates without an unsafe ACK.
The worker neither deletes stream entries nor changes outbox delivery markers.

There is currently no durable per-event receipt or retry-successor verification
contract. A retry ACK must prove an atomic replacement outbox command and
accepted scheduling/budget state; a `retry_at` write or callback assertion alone
is insufficient. Deadline expiry and UNKNOWN need the separate recovery and
reconciliation decisions described below. Bounded pending delivery recovery is
explicitly callable as above; periodic PostgreSQL review recovery and scheduling
are not implemented.
After acquisition and the locked readiness check, the worker runs a heartbeat
alongside the typed handler: every 15 seconds it renews the same live owner,
task kind, and fencing token to PostgreSQL time plus 90 seconds. Renewal changes
only lease expiry; accepted deadlines, status, attempts, and retry scheduling
are unchanged. Each renewal owns a short transaction outside provider calls.
Lease loss or renewal errors cancel the handler and retain the Redis delivery.
A refused renewal after terminal commit is accepted only when the existing
durable completion proof verifies the same live fenced claim. The worker still
requires the handler's explicit outcome, fresh completion verification, and
successful fenced release before ACK. Both child tasks are cancelled and joined
before release, including on error or worker cancellation. Provider cancellation
is cooperative: a handler that suppresses cancellation is joined, and its writes
remain fenced; a late result cannot override an observed heartbeat failure.
There is no reacquisition on behalf of that handler.
The orchestration seam supplies no provider-call guarantees itself.
`PostgresAnalysisHandler` implements analysis call-start checks; ChangeRequest
publication serialization and publish call-start checks are not implemented
and remain requirements for publisher integration.

### 4.3 Analysis handler and publication handoff

`PostgresAnalysisHandler` implements the existing `TaskHandlerPort`. Composition
must explicitly supply `GithubPort`, `LlmGatewayPort` and the checked-in schemas
directory; there is no default LLM provider or production worker entrypoint.
It checks canonical command identity, live lease and accepted deadlines before
GitHub/LLM invocations, closes all PostgreSQL transactions before external I/O,
and uses existing transaction-local fenced repositories for writes.

The first capture uses the authoritative ChangeRequest base SHA after checking
its open state and requested head compatibility. A captured ReviewJob base SHA
is retained on resumption. No WebhookReceipt supplies implicit analysis input.
The existing GitHub client and diff parser produce the snapshot, hunks and
policy coverage. Level 1 leaves source windows, whole files and related symbols
empty. The complete wire context passes canonical schema and semantic validation
before persistence/inference. Budget uses the Gateway's conservative token
estimator on the complete wire and explicitly supplied input/output limits,
persisted with the context. Prompt framing and few-shot tokens are checked
separately by ReviewService before HTTP.
An empty, fully excluded or wholly over-budget diff records SKIPPED with snapshot
and coverage, without ContextPayload or LLM invocation. Token-budget exclusion
uses the existing coverage reason. A saved context is reused after provider or
result-commit failure; accepted analysis deadlines and budgets are not reset.

Context v1 has only `metadata.rules_version`, populated with the immutable
RepositorySettings UUID, plus the frozen output language. It has no instructions
field. The supplied real gateway must resolve frozen rules/model/prompt by
authoritative review/settings identity. Stage 8B-05 supplies production-capable internal composition through
`build_postgres_analysis_worker` and `FrozenAnalysisExecutionFactory`. It builds
the existing worker/handler, never another queue or lifecycle. After authoritative
`begin`, the factory locks the ReviewJob and TaskLease, binds the accepted attempt
and current execution-local authority, and resolves only saved model/settings,
rules and prompt identities using verified deployment artifacts. Missing profiles,
unsupported history, unattestable model pins and digest mismatches fail closed
before GitHub/LLM HTTP. Runtime supplies transport, credentials and an allowlisted
profile inventory; it supplies no replacement routing/generation/retry policy.

Each execution receives a fresh service/Gateway/adapter with a concrete
`PostgresReviewRunBudget`. There is no memory-budget parameter or default in this
composition. Every retry/fallback/rebuild/repair HTTP needs a separately committed
fenced reservation in `review_llm_attempt`; settlement commits independently of
the result UoW, even for invalid paid output. All recreated objects share the
same per-review ledger. Unknown usage stays reserved; known overruns are retained
and block later calls. Conservative estimates are not a guaranteed billing cap.
The durable repair claim is irreversible across crashes, with a single owner in
ReviewService. The handler never performs another raw-output repair.

`PostgresAnalysisAttemptStore` owns the initial persisted analysis clock and
execution counter. Worker retries retain the original deadline and durable spend;
a future `retry_at` prevents begin/reservation and leaves the delivery pending.
Queue envelope attempt does not replace this persisted execution count. Gateway
whole-call timeout is bounded by saved request policy and remaining authoritative
analysis time. Rolling heartbeat/watchdog cancellation is independent of the
initial lease interval. Admission is database-linearized plus local authority
cancellation; it does not promise absolute remote-wire fencing or exactly-once
provider billing.

The gateway must validate raw model output against `schemas/llm-output/v1.json`
before backend enrichment. The completed `ReviewChunkResult` passes
`schemas/review-result/v1.json`, ordered range checks and path/side/coordinate
checks against the captured hunks. Raw-only constraints are not reapplied to
the enriched backend result. Findings carry backend
SHA-256 fingerprints over canonical path/side/range/category/title fields.
Invalid raw output after Gateway repair, or an invalid backend result, creates
no ChunkResult/Findings. Without usable validated saved results the review is
FAILED; with usable canonical saved results terminal failure preserves them as
PARTIAL, records a safe limitation and prepares the existing publication handoff.
Retained canonical results may outlive their contexts; a retained context must
pass canonical validation before its locations can support a useful result.
Optional result usage is independent of the authoritative cost ledger.
Useful fresh output with substantive limitations or arriving late is PARTIAL;
otherwise it is COMPLETED. A technical fallback provenance note alone is not a
result limitation. Terminal status and finished_at change in one SQL statement;
summary, findings/result, Publication(PENDING) and publish outbox share one UoW.
Publication is never recorded as successful by analysis.

Precise safe budget codes (`budget_requests_exhausted`,
`budget_input_exhausted`, `budget_output_exhausted`) retain public
reason LLM_UNAVAILABLE. Queue/analysis deadlines use the existing corresponding
reason codes; invalid output uses LLM_OUTPUT_INVALID. Auth/config failures and
exhausted attempts remain terminal and retain safe root details without adding
public enum values. Typed transient errors may persist bounded retry_at and
remain pending. Unknown programming/DB errors propagate without ACK. NULL frozen
markers never acquire current defaults or a new zero-cost budget: unknown
historical spend fails closed as `historical_budget_unknown`, terminalizing only
under a valid fence and retaining a useful historical result as PARTIAL.

Real PostgreSQL 18/Redis 8 integration tests use this production composition and
HTTP MockTransport for GitHub/LLM. They cover dispatch → lease/heartbeat → captured
context → frozen service → paid ledger → atomic result/handoff → authoritative
completion proof → ACK, plus terminal/retryable errors, original-ID duplicate
redelivery, lease loss, result rollback, post-settlement crash and failed ACK.
A result rollback never refunds HTTP; a new worker needs a new reservation. A
terminal duplicate skips LLM and ACKs only after existing completion proof.
Migration tests preserve legacy rows/digests/settings through 0001…0010 and the
head/check/downgrade0009/upgrade/check cycle. Accepted migrations, prompt renderers,
v1/v2 digest vectors and canonical schemas remain unchanged.

This is internal composition readiness, not deployment readiness. The builder
returns a bounded worker and starts no process. Production analysis CLI, automatic
worker startup, XAUTOCLAIM, periodic recovery/retry scheduling, complete chunk
planning/checkpoint recovery, real publisher/reconciliation and OAuth/session
integration remain outside 8B-05. Controlled Redis redelivery tests demonstrate
safety when work is delivered; they do not establish automatic recovery liveness.
Issue #13 is not declared complete by this stage.

## 5. Leases and fencing

One `TaskLease` table serves both `analyze` and `publish`, with one persistent row
per ReviewJob and at most one live owner. `TaskLease.task_kind` is a required
persisted `analyze`/`publish` value protected by a PostgreSQL CHECK. Migration
`0006` follows `0005` and backfills existing leases as `analyze`, preserving their
owner, expiry, and fencing token. No permanent database default is retained;
new grants explicitly supply their kind. Acquisition is task-specific:

- `analyze` requires an active ReviewJob (`QUEUED`, `FETCHING_DIFF`,
  `PARSING_CONTEXT`, or `LLM_PROCESSING`) with `finished_at IS NULL`.
- `publish` requires a terminal `COMPLETED` or `PARTIAL` ReviewJob, a `PENDING`
  Publication, and a non-null `Publication.deadline_at` strictly later than the
  PostgreSQL clock. A missing publication/deadline is ineligible. Acquisition
  does not change the terminal ReviewJob status or `finished_at`.

Every new acquisition, including an analyze-to-publish handoff, increments the
persisted fencing token (the first token is one). A live lease cannot be taken
over, even by the same owner or a different task kind. Release expires the row;
it never deletes it or resets the counter. Expired leases are replaced in place.

Lease authority must bind the task kind as well as review ID, owner, token, and
live database expiry. The current ReviewJob lifecycle alone is insufficient to
prove the kind of an existing lease: an analyze lease can remain live after its
terminal write. Merely adding a task kind to an in-memory claim is insufficient,
because the database must reject a claim whose kind was changed by its caller.
An analyze claim cannot authorize a publisher's writes, and a publish claim
cannot authorize analysis writes or reopen a completed ReviewJob.

Renewal keeps the owner, task kind, and token, and extends expiry to database
time plus 90 seconds. Analysis renewal requires an active ReviewJob; publication
renewal requires the same lifecycle/publication/deadline eligibility as publish
acquisition. Release checks the bound live claim independently of lifecycle,
so an analyzer can release after completing analysis and a publisher can release
after writing `PUBLISHED`, `PARTIAL`, `FAILED`, `UNKNOWN`, or `SKIPPED`.

Every persistence write for snapshot, context, chunk result, findings, terminal
state, or publication verifies the appropriate current lease in the same UoW
transaction as the write. Locks remain held through commit/rollback. Mutation
predicates also recheck expiry at the actual SQL write, including after waiting
for a database lock; a prior successful standalone check is insufficient.
Acquisition and renewal evaluate lifecycle and deadline after awaited locks.
External calls occur outside an open PostgreSQL transaction.

The analysis-to-publication handoff initializes `Publication(PENDING)` and its
five-minute deadline under the analysis fencing token, in the same transaction
as terminal analysis completion and the publish outbox event. After release or
expiry of the analysis lease, a publisher must acquire a new publish lease before
recording publication progress, provider metadata, or its outcome. These writes fence in their own
short UoW; they never mutate ReviewJob's terminal lifecycle or extend the accepted
publication deadline. Progress requires a future deadline; terminal outcomes
may be recorded after it under the current live publish lease, as specified below.

Recovery considers missing/expired leases, `retry_at`, task-specific lifecycle,
and all deadlines. It must not restart terminal analysis or create equivalent
pending work. Eligible publication work on completed analysis remains recoverable.

A lease per ReviewJob does **not** serialize publications from different
ReviewJobs for the same PR. Publication workers additionally require serialization
by the authoritative ChangeRequest identity across the external publication
cycle. This is a separate worker requirement; TaskLease acquisition alone is not
permission to make concurrent provider calls for one PR.

The persistence seams are `TaskLeasePort.acquire(..., task_kind=...)` and
`UnitOfWorkPort.publications` (`PublicationRepositoryPort`): `get(review_id)`
returns a snapshot, `prepare(analyze_claim)` allows only `NOT_READY -> PENDING`
after useful terminal analysis, and `update(publish_claim, status=...,
provider_metadata=...)` records progress or an outcome only from PENDING with
COMPLETED/PARTIAL analysis and a non-null accepted deadline.
`prepare` initializes `started_at` and a fixed five-minute deadline; `update`
keeps both timestamps and sets `finished_at` at the actual SQL write for an
outcome. PENDING/progress additionally requires an unexpired deadline both
after awaited locks and in the SQL UPDATE. Terminal targets PUBLISHED, PARTIAL,
FAILED, UNKNOWN and SKIPPED do not require that deadline to remain in the future:
the worker may finish an already-started operation or record that it will make
no further attempt. They still require source PENDING and authoritative publish
kind, owner, fencing token, and live lease in that same SQL UPDATE. An expired
or replaced lease forbids all writes, even a remote success. These methods
never commit internally and raise `LeaseLostError` on refusal. Ordinary
publisher writes cannot set `NOT_READY`, reconcile UNKNOWN, or reset the cycle.

Before every provider attempt, the worker must establish live publish authority
and a future publication deadline, then start external I/O outside a PostgreSQL
transaction. A late terminal write is persistence of that attempt's known
outcome, not permission for another provider call, acquire, renewal, or retry.
Acquire and renewal remain deadline-gated; a late result has only the remaining
lease lifetime in which to be recorded. UNKNOWN still requires reconciliation.
Publication.started_at records cycle initialization, not the time a provider
call starts. This persistence-only seam does not prove external call timing;
the caller owns that precondition. These persistence methods implement neither
a call-start marker nor provider orchestration.

Initial `Publication(NOT_READY)` creation remains part of the start-review
aggregate transaction before any worker lease exists. `PostgresAnalysisHandler`
puts terminal analysis, preparation and the publish outbox event in one UoW
as described in §4.3. Other analysis handlers must preserve that atomic handoff.
Publisher orchestration and PR serialization are not implemented.

The 0006 rollout must replace old lease writers, which cannot insert without the
required kind. Workers must be stopped before downgrading to 0005, because that
downgrade preserves lease rows but removes task-kind isolation.

## 6. Dead-letter behavior

Redis dead-letter quarantine handles invalid messages already present in Redis.
The consumer writes a safe dead-letter record before acknowledging the source
delivery; a Redis failure propagates and cannot be treated as a successful
acknowledgement. This mechanism is separate from PostgreSQL outbox quarantine
(§4.1), which retains an invalid source event before Redis enqueue. The seven-day
dead-letter retention applies to Redis records, not authoritative quarantined
`OutboxEvent` rows.

Dead-letter records use the independently versioned schema
`schemas/queue/dead/v1.json` and one of:

```text
UNSUPPORTED_SCHEMA_VERSION
UNKNOWN_TASK_KIND
INVALID_ENVELOPE
```

They contain only the required quarantine identity, reason, time, logical source
task channel (`source_routing_key` in the versioned schema) and explicitly
allowed copied identity fields. They never contain the
raw/original queue payload, arbitrary unknown fields, raw exceptions/stacks,
prompts, source, or provider responses. A dead record without a valid
`source_event_id` is diagnostic-only and cannot be replayed. Additive optional
fields remain v1-compatible for consumers, while the producer serializer enforces
the documented safe-field allowlist. Breaking changes require a new dead-schema
version. An unsupported dead-schema version is not recursively quarantined.

## 7. Operator replay

Operator replay is not implemented, and PostgreSQL quarantine is not
automatically reopened. The replay contract requires a `source_event_id` from
either PostgreSQL quarantine or a Redis dead-letter record. The operator service:

1. resolves the authoritative PostgreSQL `OutboxEvent`;
2. rejects unsupported source schema versions;
3. checks review lifecycle, deadlines, and active lease safety;
4. rejects equivalent unpublished pending outbox work;
5. creates a new `OutboxEvent` with a new event ID;
6. preserves review, task, trace, and source schema-version identity;
7. sets `attempt` to the source attempt plus one.

Raw dead messages are never replayed.
The original quarantined/published source event and its identity are retained;
replay creates new work rather than clearing its quarantine or publication marker.

## 8. Publication recovery and reconciliation

Analysis and publication lifecycles are independent. Publication retry never
reruns analysis. A retry is eligible only when remote absence is confirmed for a
`FAILED` publication. `UNKNOWN` requires reconciliation; `PARTIAL`, stale
snapshots, active publication, and other ineligible states must not enqueue a
retry.

Reconciliation checks provider state before changing `UNKNOWN` or retrying work.
Provider references are stored as provider metadata but only safe opaque
references may cross the public boundary.

`UNKNOWN` is never eligible for automatic publish acquisition, heartbeat, or a
transition back to `PENDING`. Reconciliation is a separate fenced persistence
operation, not an exception that lets a publish worker reuse its old lease.
Any operator/reconciliation change to Publication must protect both its claim
and the write within the same database transaction. Analysis `PARTIAL` may be
published initially; publication `PARTIAL` is a distinct state and does not
permit automatic republishing.

## 9. Dependency degradation

PostgreSQL is required for authoritative state and session checks; an
unrecoverable PostgreSQL failure makes the service not ready. Other dependency
failures disable only affected capabilities where safe:

- VCS degradation blocks access revalidation, available-repository listing,
  repository connection, pull-request listing, and starts;
- Redis queue degradation blocks analysis dispatch but not stored reads;
- model degradation blocks analysis progress but not stored reads;
- publisher degradation blocks publication while preserving validated results;
- session dependency degradation blocks authenticated operations.

Readiness exposes capability booleans defined by `openapi.yaml`, not raw
dependency names, endpoints, credentials, or errors.
