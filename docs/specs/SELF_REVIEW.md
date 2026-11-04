# Specification review record — API #22

Checked 2026-11-04 against API main `a0e62496c48710159396ff8dca292a4da020f2e1`. AC: [issue #22](https://github.com/larchanka-training/dmc-268-api-t2/issues/22). This is a review record, **not human approval** of either specification.

## Range and scope

Primary change is PR #68, original head `337c479973466a027f447a391b9b8b5afa65f1c0`, merged forward with current main using ordinary merge. Compare `origin/main...HEAD` plus the current working diff. No runtime prompt, admitted limits, secrets or deployment activation is changed.

The [decision profile](./SPRINT_2_3_DECISIONS.md) is the selected bounded S2/S3 implementation target. The detailed rules/context documents are extended design proposals. Their ADR decisions, numeric targets, new identities and implementation checklists are not evidence of acceptance or implementation. P1/P2 are selected under the user’s explicit delegation on 2026-11-04; full #22 specification review remains separate.

## Traceability

| #22 requirement | Design/evidence |
|---|---|
| Security/Performance/Style and ignore directives | RULES §4–§5; bounded P1 in decision profile |
| System/repository priority and injection defense | RULES §8; CTX Part B; decision profile keeps canonical DB rules and system authority |
| Base-only source | ADR-0002; RULES §2; fork file source corrected in CTX R-A.11 |
| Diff-to-GitHub coordinates | CTX C.1–C.5; strict side/hunk/evidence; renamed OLD comments use new path |
| Multi-pass filtering | CTX Part D; existing deterministic checks and durable Reflection retained as current/follow-up, extended verifier is a proposal |
| Human-approved documents | **Not yet met**; approval record in decision profile |

## Existing PR #57 findings and disposition

Sources: [first review](https://github.com/larchanka-training/dmc-268-api-t2/pull/57#issuecomment-6516851446), [portability/self-check follow-up](https://github.com/larchanka-training/dmc-268-api-t2/pull/57#issuecomment-6541200147).

| Finding | Disposition in primary #68 path |
|---|---|
| Python prototypes excluded from all gates | No exclusions added. Tracked self-check passes mandatory Ruff/format/mypy and runs through pytest. Old prototype package is not transplanted. |
| Python 3.12 claims; separate spike harness | Use project `uv sync --frozen --dev`, Python3.14, existing pytest. No second venv/harness required. |
| Dead ADR links | Retain real ADR-0002 and local document section identities. Do not copy missing ADR references from prototypes. |
| Windows huge pytest node IDs | New tests have short fixed names; large payloads are created in the body, not parametrized IDs. Old spikes are not adopted. |
| Tautological nonce check | Not adopted. Self-check only claims schema/example consistency, no security/model robustness. |
| absent version =1 vs null | Extended rules §4.3 specifies omitted version defaults to1. No nullable parsed version is introduced. |
| GitLab, cross-cycle and enlarged benchmark scope | Explicitly forward/future proposals; GitHub-only S2/S3, 20–30 fixture corpus and current limits remain the delivery scope. |
| Draft documents are not approved | Clearly marked Draft; actual human review still required. #57 remains open pending team disposition. |

Useful #57 invariants retained: verbatim legacy instructions; default-off base-authorized inline ignores; authorized/scoped cache; no anchor relocation; exact anchored evidence; frozen durable verification; no raw-content diagnostics. History preserves original authorship and full drafts. Neither review is reported as approval of the corrected #68 head.

## Self-check and limits of its evidence

Run with the repository interpreter:

```bash
uv sync --frozen --dev
uv run python spikes/spec_selfcheck.py
uv run pytest tests/test_spec_selfcheck.py
```

All original 28 mechanical checks are retained: the three embedded schemas; prototype defaults and example merges/diagnostics; sample finding/verifier shapes; invalid labels; compatible rule-reference patterns. A CLI regression proves an invalid selected schema exits nonzero without printing its contents. Pytest executes the checked-in examples in the normal mandatory suite. The toy parser is **not** the production parser and does not establish complete grammar, authorization, cache correctness, AST extraction, prompt-injection resistance, or LLM quality. Those require #23/#25/#27 tests and a separately authorized real benchmark.

No `pip install`/untracked Python version assertion substitutes for project gates. No quality configuration has been weakened.

## Review axes

- **Acceptance criteria — blocker:** human approval of #22 remains pending. P1/P2 were chosen under explicit user delegation; do not close the issue because checks pass. Extended-design implementation checklists stay unchecked.
- **Tests — technical slice covered:** failed invalid-schema CLI test before implementation, then green; retained example checks run through the existing pytest suite. Target PostgreSQL18/Redis8 and Docker checks require the new published-head CI result; these results are pending. Local skips are not acceptance evidence.
- **Bugs/correctness — corrected:** fork source inversion, renamed OLD path, clamp/neighbor evidence acceptance, legacy-rule coercion, canonical field rename, cache scoping and early/late freeze ambiguity. Future proposals are separated from current contracts.
- **Security — corrected:** inline ignore opt-in defaults, rules stay data, no raw offending content in diagnostic policy, cached reads require authorization. No token values, credential provisioning or live provider/publication action is included.

Merge as “specifications approved / #22 complete”: **blocked on human approval**. Code/config/runtime activation is not authorized by merging these draft documents.
