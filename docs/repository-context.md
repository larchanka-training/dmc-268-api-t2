# Base repository rules and source context (issue #23)

This implements the selected bounded P1 profile from issue #22/PR #68. It is
explicitly selected by a trusted runtime's verified **v4** prompt bundle.
Historical v1/v2/v3 jobs, settings checksums, renderer bytes and result schemas
retain their original meaning. Merely installing this code does not switch the
configured runtime or activate a worker. Deployment is a separate operation.

## Root Markdown rules

Only `.review/rules.md` in the upstream/base repository at the admitted PR's
immutable `base_sha` is read. A rules change in the PR head affects later review
cycles. The file is UTF-8, at most 64 KiB; GitHub responses identified as directories or symlinks and unsupported
content encodings are rejected (the Contents API may resolve an in-repository
symlink to a regular file). Nested discovery, YAML, head-side author
instructions and arbitrary path exclusions are not implemented.

```markdown
# Security
## SEC-1: Tenant isolation
- severity: high

Bind tenant_id from the authenticated session on every tenant-data query.

# Performance
## PERF-1: Bound outbound work
Use bounded concurrency and request timeouts.

# Style
## STYLE-1: Domain terminology
Use established domain terminology for public APIs.

# Ignore
- STYLE-1
```

Sections are optional, case-sensitive H1 headings: `Security`, `Performance`,
`Style`, `Ignore`. H2 entries are `## REF-1: Title`, with an uppercase alphanumeric
prefix of 2–10 characters and 1–4 decimal digits. Following Markdown is the
criterion; triple-backtick/tilde fenced examples remain literal data. Unclosed
fences and unknown H1 sections reject the file. Severity metadata accepts exactly
`critical`, `high`, `medium`, `low`; omission preserves model severity. Invalid
severity drops that entry. Duplicate refs, including collisions with existing DB
instructions, are dropped with safe code/line diagnostics. File criteria append
to existing DB instructions verbatim and may not exceed their existing combined
65536-character bound. A combined overflow rejects the file and preserves the
original instructions. Existing system criteria, schema and budgets prevail.

`Ignore` lists explicit refs and suppresses only matching file criteria. It never
suppresses system checks, DB instructions or unrelated model findings. Existing
authenticated settings remain the authority for path ignores. Inline directives
in PR content remain disabled. Only declared and actually rendered references
are accepted in model findings; labels embedded in criterion examples cannot
create new refs. A declared severity is applied deterministically to an accepted
finding with that ref, after coordinate/evidence validation.

## Defaults, errors and reproducibility

Missing and empty files use existing system and DB rules. The result summary
contains a fixed informational default note; these normal states alone do not
make a review partial. Invalid content or dropped criteria produce safe
code/line diagnostics, a visible summary warning and limitations through the
existing partial-result mechanism. Offending source text is never included in
those diagnostics. Authentication, denied access, missing repository/revision,
rate limits and provider/transport failures do not become absence: admission
refuses/retries through the existing safe error path.

The positive raw-file cache has 128 entries. Its identity includes tenant/
repository, external repository ID, owner/name, immutable revision, root path
and parser version. Each hit revalidates provider access. Missing files and
failures are not cached; a new base SHA naturally selects a new entry. Concurrent
eviction does not invalidate an already captured immutable hit.

Admission materializes an immutable effective `RepositorySettings` row with
canonical instructions and `rules_source` provenance: original settings ID and
identity, repository/base/blob identity, content SHA-256, parser version, state,
diagnostics, declared refs and severities. Its deterministic UUID covers these
values, excluding creation time, so equivalent active starts still deduplicate.
The repository's current settings pointer is unchanged. Under the existing
admission lock, the source settings identity is checked again before job/quota/
outbox persistence. A rejected admission may leave a harmless immutable snapshot;
it creates no job, quota or outbox event. Frozen execution verifies the effective
snapshot identity, then uses its saved instructions/provenance rather than
rereading current settings or the rules file.

Migration `0018` adds nullable JSONB provenance. Historical rows remain NULL and
retain their old identity. Downgrading removes provenance and therefore is only
safe after draining/removing newly admitted v4 jobs in an explicitly isolated
rollback; never reinterpret such jobs as historical settings.

## Tree-sitter in the analysis path

For v4, the existing analysis handler enriches the real ContextPayload v1 before
persisting it and before any model call. OLD source comes from the upstream
merge-base SHA; NEW source comes from the head repository/head SHA, including
forks. Blob identity is checked when the captured diff supplies it. Recovery
reuses the saved payload and saved effective settings. Original changed-line
anchors remain the only valid finding coordinates.

Maintained Python, TypeScript/TSX, JavaScript/JSX and Go grammars extract imports,
enclosing classes/functions/methods (including declared arrow functions),
signatures and surrounding source windows. The extraction does not execute code
or perform whole-project/call-graph lookup. Per-file source is bounded to64 KiB,
2048 syntax nodes,32 symbols,2048-character excerpts and512-character signatures.
Source windows preserve complete line ranges within8192 bytes and80-line spans;
when a parent is too large, up to16 complete changed-line windows are retained.
At most16 source reads and30 seconds cover enrichment. Parsing has a50ms progress
callback deadline (hard ceiling1s for explicit test configuration). These are
technical resource caps, not product SLOs or new model budgets.

The traversal uses an iterative stack and byte offsets. With Tree-sitter0.26 on
local macOS/Python3.14, repeated native `Node.start_point`/`end_point` access was reproduced
crashing a subprocess; no Point getter is used. Regression coverage exercises the
real public extractor repeatedly in a child process and a deeply nested tree.
The node cap is a separate resource bound, not a claim that a thread contains
native crashes. Unsupported languages, unavailable optional source, syntax/
resource/deadline failures degrade visibly to bounded windows/diff.

The new verified v4 renderer sends saved source windows/symbols as untrusted
`<source_context>` data. Protected delimiters and chat role markers are
neutralized. Optional AST context drops before diff/rules when the existing
frozen token budget is tight; overflow and schema repair preserve the smaller
ceiling. Provider/result schema2, `side`/line coordinates and `rule_ref` remain
unchanged. Existing durable ledger, leases, retry and fencing own every call.
Reflection in the durable path belongs to issue #25; this change does not enable
that extra inference stage.

## Local verification

Use the quality and isolated PostgreSQL18/Redis8 instructions in README.md. The
focused public regressions are:

```bash
uv run pytest tests/test_repository_context.py tests/review/test_prompt_v4.py
uv run pytest tests/test_repository_context_start.py
```

The integration file admits through the real authorized HTTP route and runs the
actual analysis worker, frozen reader, PostgreSQL request ledger and HTTP model
adapter with synthetic provider responses. It asserts saved AST, actual gateway
messages, default/invalid UI summary projection, severity/ref enforcement and
stable equivalent-start identity, and worker retry without new VCS reads or
changed payload/configuration. It does not measure live model quality, make
paid calls or publish a GitHub review.
