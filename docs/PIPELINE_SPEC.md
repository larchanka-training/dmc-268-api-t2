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
- Publication deadline: 5 minutes for a publication cycle.
- Context and model token budgets are fixed in the persisted ContextPayload.

Exceeding a deadline records the corresponding reason event and applies terminal
semantics based on whether useful validated output exists. Retry scheduling must
never extend an accepted deadline.

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
when a message is lost after enqueue, without reviving a terminal job.

The v1 serializer emits only the canonical envelope fields. V1 consumers ignore
unknown optional fields. Breaking envelope changes require a new major version
and corresponding consumer.

The dispatcher selects unpublished outbox records. It marks
`broker_published_at` only after the queue adapter confirms enqueue. Consumers reject
unsupported schema versions, unknown task kinds, and otherwise invalid envelopes
before domain work begins.

## 5. Leases and fencing

At most one live `TaskLease` exists per review. A worker must acquire or renew the
lease and use a monotonically increasing fencing token. Every persistence write
for snapshot, context, chunk result, findings, terminal state, or publication
must verify the current lease and fencing token. External calls occur outside an
open PostgreSQL transaction.

Recovery considers missing/expired leases, `retry_at`, lifecycle status, and all
deadlines. It must not revive terminal work or create equivalent pending work.

## 6. Dead-letter behavior

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

Replay is operator-only and accepts a `source_event_id`. The operator service:

1. resolves the authoritative PostgreSQL `OutboxEvent`;
2. rejects unsupported source schema versions;
3. checks review lifecycle, deadlines, and active lease safety;
4. rejects equivalent unpublished pending outbox work;
5. creates a new `OutboxEvent` with a new event ID;
6. preserves review, task, trace, and source schema-version identity;
7. sets `attempt` to the source attempt plus one.

Raw dead messages are never replayed.

## 8. Publication recovery and reconciliation

Analysis and publication lifecycles are independent. Publication retry never
reruns analysis. A retry is eligible only when remote absence is confirmed for a
`FAILED` publication. `UNKNOWN` requires reconciliation; `PARTIAL`, stale
snapshots, active publication, and other ineligible states must not enqueue a
retry.

Reconciliation checks provider state before changing `UNKNOWN` or retrying work.
Provider references are stored as provider metadata but only safe opaque
references may cross the public boundary.

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
