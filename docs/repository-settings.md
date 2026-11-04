# Repository settings and automatic review

`GET /api/v1/repositories/{id}/settings` and conditional `PUT` require a local
connection and the current personal GitHub administrator permission. Browser
mutations retain the existing session, Origin and CSRF checks. The current
version is an opaque quoted UUID ETag. PUT accepts a complete canonical
replacement with `If-Match`, creates an immutable row and switches the current
pointer under the repository lock. A stale ETag returns
`409 SETTINGS_VERSION_CONFLICT`; the client must preserve its draft and obtain
a fresh baseline before an explicit retry.

The full DTO includes rules, ignores, start/active-job quotas, output language,
surrounding lines and these preferences:

- `auto_review_enabled`: false by default. Enabling associates the saving
  administrator as the server-owned authorizing actor; clients cannot set it.
- `base_branches`: exact Git branch names, empty for all bases. No glob or ref
  expansion. Technical request bounds are 128 names and 1024 UTF-8 bytes per
  name. Invalid ref characters/structure are rejected, without normalization.
- `minimum_severity`: `low`, `medium`, `high` or `critical`, default `low`.
  The UI presets map all→low, warnings and critical→medium, critical→critical.

Missing preference fields in a six-field canonical replacement use the safe
public defaults; they never inherit arbitrary legacy JSON. Exact historical
empty rules/ignores objects are projected for reading without changing storage
or digests. Other unsupported legacy shapes return
`409 LEGACY_SETTINGS_UNSUPPORTED` plus the replaceable ETag, without exposing
raw legacy JSON. GET reports the actual stored nonempty language; PUT's
canonical language remains `ru`. A form unable to preserve a historical
language must explain that unsupported baseline instead of silently coercing
it. Quotas are nonnegative PostgreSQL int32 values; zero keeps its existing
admission refusal meaning.

## Read-only file preview

`GET /api/v1/repositories/{id}/rules-preview` needs a current local connection
and fresh personal repository-read permission, including for a cached SHA.
It resolves the current default branch on each request, then uses its immutable
commit SHA to read only the root `.review/rules.md`. The response includes
parsed file criteria, branch, SHA, blob/content identity, parser state and safe
diagnostic codes/line numbers. It never includes database instructions and
never changes current settings or an admitted job. Clients render criteria as
text or safe Markdown, without raw HTML or executable links.

Absent, empty, valid and invalid content are distinct successful preview
states. Invalid content exposes only fixed diagnostics, not rejected source.
An inaccessible repository/ref, failed authorization, mismatched commit or
provider/network failure is an explicit safe error, never an absent file.
The bounded raw-file cache includes tenant/repository/SHA/parser identity and
reauthorizes every hit. Refresh resolves the moving branch again.

## Automatic admission

Only NEW accepted `opened`/`synchronize` receipts can select the enabled policy.
Acceptance and settings replacement serialize on the same repository lock.
The receipt freezes the policy, source settings UUID/raw identity and the
saving administrator. Old/default-off receipts and duplicate deliveries never
acquire a policy after a later enable. Event senders and head refs grant no
permission.

The HTTP background task and recovery worker use the same non-HTTP admission.
They recheck the captured actor's connected access and fresh personal GitHub
admin permission, provider PR/repository/head/base identities, source settings
and exact verified base branch. Manual starts remain independent of the
automatic switch and base filter. Source changes refuse that selected event;
its intent is never rebased onto newer settings.

New work uses the existing quota, immutable configuration, aggregate,
publication, outbox and queue. The transaction locks repository→receipt→actor
access→change→source settings. It checks the attempt/live database-clock lease
and repeats the fence after flushing writes. Job, quota, outbox, durable
receipt→job binding and Level1 READY snapshot commit together. An exactly
equivalent active manual run can be bound without another quota/outbox;
manual duplicate-active behavior remains 409. The binding survives ordinary
24-hour key expiry and a terminal job. Replays resolve it before current
actor/settings/runtime checks and cannot create another paid run.

Transient preparation failures keep the receipt PENDING with the existing
bounded retry/three-attempt/90-second lease behavior. Permission, stale target,
changed source or excluded base produce a safe final automatic decision with
no job. Losing the attempt/lease rolls back all admission writes. Level1
artifacts remain separate from the analysis worker's own frozen context; this
adds no cross-run cache or review lifecycle.

## Publication and compatibility

The admitted immutable settings freeze the publication threshold. Every
validated finding stays in saved results/history/UI. Before the first retained
publication operation, the publisher filters both inline comments and overview
findings. Above `low` it uses neutral completion/count text, preventing the
unrestricted model summary from leaking excluded findings. No eligible
findings still permits one threshold-aware COMMENT overview. Default `low`
preserves the previous output. Existing operation hashes, UNKNOWN reconciliation
and repeated publication decisions are retained.

Historical NULL policy keeps the exact old source/settings/config digests,
effective UUIDs and policy1/2 readers. New non-NULL policy, including actor,
is bound by source identity, effective snapshot UUID and frozen settings
checksum; saved-policy changes or erasure refuse publication before HTTP.
Migrations `0020`/`0021` are additive nullable columns without backfills.
The binding FK restricts deletion rather than losing the durable association.
Downgrade0021 refuses any retained non-NULL receipt binding; downgrade0020
refuses any retained non-NULL policy row, including saved preferences/history.
Old NULL rows still support a clean0019↔head roundtrip. Rollback never deletes
or resets data to make these guards pass.

Automatic recovery requires the trusted analysis manifest and credential key;
see [staging deployment](staging-deployment.md). Default-off Level1 capture does
not need them. Rollback must retain compatible intake and policy-aware
publication consumers while selected receipts/jobs/operations remain; never
run an L1-only drainer over selected automatic receipts or republish with a
broader threshold. No setting enables global deployment, inference credentials
or real review publication by itself.
