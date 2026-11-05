# Sprint 2–3 delivery profile and approval record

Status: **P1/P2 selected under explicit user delegation**, 2026-11-04. AC source: API [#22](https://github.com/larchanka-training/dmc-268-api-t2/issues/22), [#23](https://github.com/larchanka-training/dmc-268-api-t2/issues/23), [#25](https://github.com/larchanka-training/dmc-268-api-t2/issues/25), [#27](https://github.com/larchanka-training/dmc-268-api-t2/issues/27), UI [#20](https://github.com/larchanka-training/dmc-268-ui-t2/issues/20).

Selected P1/P2 is the binding implementation scope for S2/S3. The extended designs in [RULES_FORMAT_SPEC](./RULES_FORMAT_SPEC.md) and [CONTEXT_AND_VERIFICATION_SPEC](./CONTEXT_AND_VERIFICATION_SPEC.md) remain proposals, not additional mandatory S3 requirements. Conflicting extended-draft requirements do not apply to this delivery. This profile distinguishes existing decisions from selected product semantics. It does not record an approval on behalf of a person. An unchecked implementation checklist in either extended spec is a future verification plan, not evidence of delivered behavior.

## Existing authority retained

- GitHub-only, OAuth App plus bot-user, publication `COMMENT`; no GitHub App migration, approvals, Checks, GitLab adapter or cross-cycle cache work is required here.
- ADR-0002: `.review/rules.md` comes from the upstream/base repository at captured immutable `base_sha`. A rules change in the reviewed PR affects later cycles only.
- Historical prompt bundles/renderers/schemas/frozen settings retain their identities and original bytes. `rule_ref` and wire severity `critical/high/medium/low` remain compatible. Do not rewrite saved jobs to make a new parser accept them.
- Existing transactional outbox, queue, leases, fencing, request accounting and publication reconciliation remain authoritative. Analysis completion and publication completion are separate.
- The checked main runtime uses context 16384, input10000, output2000, 8 calls, 5 chunks, 600s run/120s request. The extended draft's 32k/16call/second-model proposal is **not approved** and is not an implementation prerequisite.
- Existing webhook captures a snapshot; manual authorized StartReview creates the job. Tests must state that boundary honestly until the selected opt-in automation below is implemented.

## Selected bounded rules profile (decision P1)

1. Implement the root `.review/rules.md` carrier first. Support Security, Performance, Style and Ignore sections, heading entries `## SEC-1: Title` followed by bounded Markdown criteria. Optional severity uses the existing canonical wire vocabulary; absence leaves model severity unchanged. Sections may be omitted. No new built-in defaults, nested discovery, YAML inheritance, linter product, synthetic labels or v4 label-coordinate protocol is required for S3.
2. Keep canonical DB instructions verbatim. Add valid base-file criteria with stable optional `rule_ref` labels. Reject duplicate labels (including collisions with DB labels) with a visible diagnostic rather than invent override semantics. System instructions, output schema, budgets and authorization always prevail over repository criteria.
3. Absence or an empty file is a supported default state: existing system criteria plus existing DB instructions. Invalid entries are dropped with safe code/line diagnostics; invalid encoding, size or structure rejects the file. Provider/access errors do not become “file absent”. Record fallback/limitations in the snapshot and result.
4. Ignore directives name explicit rule references and can suppress only matching repository criteria, never system checks or arbitrary model findings. Author-controlled inline directives default **off** and require explicit base-file per-rule opt-in. Whole-path exclusions reuse the existing authenticated settings ignore policy; a PR author cannot add a head-side directive that changes it.
5. Read at immutable repo/ref/path identity; include tenant/repository identity and parser version in cache keys and revalidate access on hits. A new base SHA naturally invalidates the lookup. Capture content/provenance/digest durably before the first model call; recovery reuses that snapshot. Never change an admitted frozen digest or the repository's current settings pointer to retrofit file rules.
6. Limits derive from existing settings/context bounds and actual prompt budget; propose 64KiB maximum file. AST is bounded to changed files for Python, TS/TSX/JS and Go. Unsupported or failed parsing degrades visibly to source windows/diff; it does not silently claim full context.
7. Render enriched context under a new verified prompt identity if its bytes differ. Retain current side/line anchors and exact evidence; reject cross-side/cross-hunk or ungrounded locations instead of moving them. Render Markdown as untrusted data and never execute repository instructions or log their contents.

Examples for the selected profile:

```markdown
# Security

## SEC-1: Tenant isolation
- severity: high

Queries on tenant data must bind tenant_id from the authenticated session.

# Performance

## PERF-1: Bound outbound work

Outbound requests need a timeout and bounded concurrency.

# Style

## STYLE-1: Public naming convention

Public error names should follow this repository's established naming convention.
```

Exact parser grammar/schema/ignore syntax will be committed with #23 tests against this profile under this decision. The large extended format in RULES_FORMAT_SPEC remains an unapproved design alternative, not a second simultaneously active format.

## Selected automation/settings semantics (decision P2)

- Persist `auto_review_enabled=false` by default; it affects only newly accepted webhook events, and never bypasses repository/bot authorization, limits or idempotency.
- Base-branch allowlist: empty means all base branches; otherwise exact Git branch names only, evaluated against the PR base branch. Glob patterns are rejected in the selected S3 profile; glob support is a possible future proposal. Manual StartReview remains available even when auto review is disabled or filtered.
- Minimum severity controls **publication**, retaining all validated findings in the UI/audit. It does not hide stored findings or alter the model's schema. Default `low` preserves current publication behavior.
- Rules preview is read-only, fetched from an explicitly identified base revision, with provenance/default/error states. Settings writes require the already declared admin permission, canonical immutable version and If-Match contract.
- QA covers both default manual and opt-in automatic paths, including replay, access revocation, branch filtering and rules source provenance. No manual StartReview may be presented as proof of automatic webhook processing.

## Measurement and operational acceptance

- #15 corpus remains 20–30 real/public and synthetic fixtures. Synthetic replay measures the harness; mocked E2E measures plumbing; a provider run measures that model/config only. Preserve model/prompt/schema/config/dataset identities, TP/FP/FN, matching policy, usage and price basis.
- #27 reports measured load characterization. The extended draft's 100PR dataset, latency/quality thresholds and nightly provider jobs are proposed future targets, not approved SLOs or permission to spend.
- Code/config and isolated restore tests may proceed. Actual LLM runs require exact target/model and spending cap; external review POST requires an explicitly named sandbox PR; deployment/host restore/team notification requires target authorization.

## Reconciliation of the two existing PRs

Primary review vehicle: [API #68](https://github.com/larchanka-training/dmc-268-api-t2/pull/68). [API #57](https://github.com/larchanka-training/dmc-268-api-t2/pull/57) remains a useful design/prototype reference and stays open pending its author's/team's disposition. No third duplicate PR is created.

Useful #57 invariants retained: verbatim historical rules, default-off author directives, scoped/authorized caches, strict anchors and exact evidence, frozen durable verifier policy, no raw-content diagnostics. Its experimental parser, provider extensions, old passing counts and quality exclusions are not transplanted. The original #68 detailed design remains available below this profile and in its original commit `337c479`; no proposal becomes approved through that preservation.

## Human approval record

| Decision | Proposed choice | State / evidence |
|---|---|---|
| P1 bounded base-rules profile | Root sections + canonical refs, additive DB compatibility, system priority, explicit safe ignores; limits unchanged | Selected by the implementation owner under user instruction “Выбери сам как правильнее и сделай”, 2026-11-04 |
| P2 settings/automatic review | Off by default; exact-name base-branch allowlist; minimum severity gates publication | Selected by the implementation owner under user instruction “Выбери сам как правильнее и сделай”, 2026-11-04 |
| Full #22 specification approval | This profile plus corrected applicable sections of both specs | **Awaiting human review; not approved by the agent** |
| Other PR disposition | Keep #57 open as reference until team agrees | No closure performed |
