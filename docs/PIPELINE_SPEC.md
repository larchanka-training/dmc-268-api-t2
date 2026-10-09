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
are backend-owned outside the model output. The open PR #18 gateway adapter must
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

The v1 Redis-backed queue adapter must provide these observable guarantees;
issue #13 owns the concrete worker/DLQ implementation:

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

The dispatcher selects unpublished outbox records that are not quarantined. It
marks `broker_published_at` only after the queue adapter confirms Redis enqueue.
Consumers reject unsupported schema versions, unknown task kinds, and otherwise
invalid envelopes before domain work begins.

### 4.1 PostgreSQL outbox quarantine — stage 3B contract proposal

The quarantine rules in this subsection are a change to the shared pipeline
contract proposed for Tech Lead review. They do not change the canonical queue
or dead-letter JSON Schemas.

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
codes, and mutual exclusion of publication and quarantine. Both terminal writes
conditionally update only an event that is still unpublished and unquarantined,
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

The minimal PostgreSQL dispatcher is `PostgresOutboxDispatcher.dispatch_batch()`
in `src/app/adapters/outbox_dispatcher.py`. It accepts the existing session factory
and `QueuePort`, selects oldest unpublished, unquarantined events with
`FOR UPDATE SKIP LOCKED`, and defaults to a batch of 100 (the supplied batch size must be positive). It
copies authoritative event columns into `QueueTaskEnvelope` and closes the
selection transaction before Redis enqueue. Each confirmed enqueue is marked in
its own short transaction, updating only a still-unpublished, unquarantined event.

Selection locks are not claims across Redis I/O: overlapping dispatcher runs may
enqueue duplicates after those locks are released. A crash after enqueue but
before the marker commits also permits a duplicate. Both paths retain the same
event ID and envelope; delivery is at-least-once, not exactly-once. The method
returns the number of confirmed Redis enqueues, including concurrent duplicates;
quarantined events do not increase that count. It continues after a known
canonical validation failure has been durably quarantined and propagates the
first other enqueue or database error. Earlier publication/quarantine markers
stay committed; failed and remaining ordinary unpublished events stay eligible
for the next invocation. There is no transaction spanning PostgreSQL and Redis,
dispatcher retry loop, or long-running process in this slice. A dispatcher that
already copied an envelope is not cancelled by another run's terminal write.
If source columns are concurrently corrected, an in-flight valid snapshot can
still enter Redis after quarantine; its ACK is counted but cannot overwrite the
quarantine marker. These guarantees concern authoritative PostgreSQL states,
not exactly-once broker I/O.

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

The stage 4B persistence seams are `TaskLeasePort.acquire(..., task_kind=...)` and
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
the caller owns that precondition. No call-start marker or provider orchestration
is introduced in stage 4B.

Initial `Publication(NOT_READY)` creation remains part of the start-review
aggregate transaction before any worker lease exists. Worker completion/outbox
orchestration and PR serialization remain separate integration work; callers
must put terminal analysis, preparation and the publish outbox event in one UoW.

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

Replay remains future operator-only work; stage 3B does not implement an operator
service or automatically reopen PostgreSQL quarantine. The proposed replay
contract accepts a `source_event_id` from either PostgreSQL quarantine or a Redis
dead-letter record. The operator service:

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
