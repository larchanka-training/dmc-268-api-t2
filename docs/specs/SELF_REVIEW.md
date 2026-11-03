# Self-review: RULES_FORMAT_SPEC and CONTEXT_AND_VERIFICATION_SPEC

| Field | Value |
|---|---|
| Date | 2026-11-03 |
| Specs reviewed | [RULES_FORMAT_SPEC.md](./RULES_FORMAT_SPEC.md) v0.9.1, [CONTEXT_AND_VERIFICATION_SPEC.md](./CONTEXT_AND_VERIFICATION_SPEC.md) v0.9.1 |
| Mechanical check | `spikes/spec_selfcheck.py` — 28/28 checks pass |
| Approvers needed | Tech lead, Security, VCS integration owner |

## 1. Traceability

| Requirement (task brief) | Document → section |
|---|---|
| Phase 0 discovery: pipeline, AST, LLM inputs, diff/posting, gaps | Phase 0 report (conversation); summarized in CTX §1.2, RULES §1.2 |
| Rules: purpose, goals, non-goals, glossary | RULES §1 |
| `.review/rules.md` location, nested files, absent file | RULES §2.1, §2.5, §2.6 |
| Built-in defaults: location, versioning, updates, full content | RULES §3.1, §3.2 |
| Markdown + YAML frontmatter; limits; grammar; JSON Schema | RULES §4.1–§4.8 |
| Sections Security/Performance/Style; Ignore; inline directives; id-less ignore forbidden | RULES §4.4, §5.5, §5.6 (R-5.6) |
| Rule structure: id, severity (blocker/major/minor/info), paths, text | RULES §4.5, §5.1 |
| Precedence: override, add, disable, severity, scope, section merge, nearest-file-wins | RULES §6.1–§6.3 |
| Security floor evaluation as ADR | RULES §6.4 (ADR-R3) |
| Conflict table ≥ 8 examples | RULES §6.5 (15 rows) |
| Validation: drop vs reject, error messages, CLI linter | RULES §7 (ADR-R1, §7.3 codes, §7.4 CLI) |
| Rules loaded from base branch only; cross-ref to injection section | RULES §2.2, §8; CTX Part B T1, B.2.1 |
| Three examples incl. invalid + linter output | RULES §9.1–§9.3 |
| Acceptance checklist + OQ (rules) | RULES §10, §11 |
| Context sources, priority, budget share, truncation (all 9 listed sources) | CTX A.1 table |
| Assembly pipeline, budget algorithm, fallback, caching, Mermaid | CTX A.5, A.2, A.4.4, A.6 |
| Improvements to the tree-sitter implementation | CTX A.4 (greenfield; none exists), A.7 |
| Threat model table (rules, diff comments/strings, title/description, file names, commits) | CTX B.1 (T1–T12) |
| Defense by stage: Ingestion, Parsing, Detection, Assembly (nonce, hierarchy, floor), Output (schema, no tools), Write-back (deterministic, least privilege, content checks) | CTX B.2.1–B.2.6 |
| Red-team corpus ≥ 15 in CI | CTX B.3 (20 cases) |
| Unified diff → line table; labels `R/L/C`; model returns labels | CTX C.1 (ADR-C1) |
| GitHub mapping incl. deprecated `position` | CTX C.2 |
| GitLab `position` mapping incl. `line_range` | CTX C.3 |
| Side-by-side GitHub/GitLab table | CTX C.4 |
| Edge cases: renames, deleted/binary, outside hunks, cross-hunk ranges, force-push/stale head, 422/400, retries | CTX C.5 |
| Idempotency fingerprint `hash(rule_id, path, normalized_line_content)`; changed code under old comment | CTX C.6 (ADR-C2, C.6.2) |
| Pass 1–5 with input/output/reason codes/logging | CTX D.1–D.6 |
| Verifier prompt, schema, threshold, model choice | CTX D.5 (ADR-D1, ADR-D2) |
| Metrics with targets | CTX D.7 |
| Part E checklist + OQ | CTX E.1, E.2 |
| Document standards (header, purpose…, Mermaid, ADRs, examples, checklist, OQ, references) | both documents: header table, §1, diagrams, ADRs, examples, checklists, OQ, References |
| RFC 2119 keywords; cited code symbols; external fields linked | both documents |

## 2. Contradiction check (found → fixed)

| # | Finding | Fix |
|---|---|---|
| X-1 | RULES §9.2 overrode `BMNT-1` under `# Security`, violating RF034 | moved under `# Maintainability` (Phase 1) |
| X-2 | RULES §9.3 expected output missed RF034 for `BPERF-1` declared under `# Security`; narrative said it became `minor` | added RF034 line; narrative corrected (found by `spec_selfcheck.py`) |
| X-3 | RULES §9.3 expected an inline-directive diagnostic but the CLI had no source scan | added `--sources` (Phase 1) |
| X-4 | `effective-v2` `layers.maxItems: 13` < possible 34 records (4 fixed + 10 nested + 20 not-probed) | `maxItems: 34` |
| X-5 | CTX output v3 uses `rule_id`, RULES/v2 use `rule_ref` without mapping | CTX §1.6: `rule_id` renames `rule_ref`, same value space and column |
| X-6 | RULES allows opt-in Style rules; v3 system prompt bans all style remarks; CTX did not amend it | CTX R-B.0 |

Checked and consistent: severity mapping and `info` = summary-only (RULES §4.5 ↔ CTX D.4/D.6); rule-severity override (RULES R-4.11 ↔ CTX D.2); floor immutability (RULES ADR-R3 ↔ CTX R-B.4, RT-04, RT-16); shared injection detector (RULES R-8.5 ↔ CTX B.2.3, AC-B2); base-branch sourcing (RULES §2.2 ↔ CTX B.2.1); freezing with the persisted context (RULES R-2.7/R-6.7 ↔ CTX R-A.16); size limit 64 KiB in both; rule-id regex identical in both schemas (checked mechanically).

## 3. Mechanical validation (`spikes/spec_selfcheck.py`)

Prototype implementation of the RULES grammar and merge algorithm (spike only, not production):

- All 3 JSON Schemas in the specs are valid Draft 2020-12.
- `defaults-v1` parses with zero diagnostics: 2 floor + 16 rules.
- §9.1 and §9.2 (root + nested): no diagnostics; merged documents validate against `effective-v2`; expected effects asserted (BSEC-6 disabled, BPERF-4 major, BMNT-1 info→major under `services/payments`, ACME-40 nested only).
- §9.3: prototype linter output equals the spec's expected output byte-for-byte; the merged document (with diagnostics) validates; effective ruleset = floor + defaults.
- CTX: the C.1 example finding validates against output v3; malformed labels (`R0`, `41`, `C40`, `X1`) rejected; sample verifier output validates.

Run: `python -m venv spikes/.venv && spikes/.venv/bin/pip install 'jsonschema>=4.25,<5' && spikes/.venv/bin/python -I spikes/spec_selfcheck.py` (the project `.venv` is a stale Python 3.12 environment without `jsonschema`/PyYAML; PyYAML must become a direct dependency per RULES R-4.1).

## 4. Injection cases → mitigations

All 20 cases in CTX B.3 name at least one mitigation stage; each stage is specified in B.2.1–B.2.6, and each threat T1–T12 is covered by at least one case:

| Threat | Cases |
|---|---|
| T1 | RT-01 |
| T2 | RT-02, RT-03, RT-04 |
| T3 | RT-05, RT-06, RT-07, RT-18 |
| T4 | RT-08, RT-09, RT-10 |
| T5 | RT-11 |
| T6 | RT-12, RT-13, RT-14, RT-17 |
| T7 | RT-15 |
| T8 | RT-19 |
| T9 | RT-10 |
| T10 | RT-16 |
| T11 | RT-20 |
| T12 | RT-18 |

## 5. `TODO(verify)` inventory

Every remaining `TODO(verify)` is listed in an Open Question with an owner: RULES OQ-R5; CTX OQ-C2, OQ-C4, OQ-C7, OQ-C8, OQ-C9. No unlisted `TODO(verify)` remains (grep-checked).

## 6. Remaining risks

| Risk | Impact | Mitigation / owner |
|---|---|---|
| Budget increase (OQ-C1) not approved | Extended context mostly dropped at 10k input; Part A value limited | Tech lead decision before implementation |
| Golden dataset (≥ 100 labeled PRs) does not exist | Part D thresholds and v4 activation gate cannot be measured | Plan labeling effort; τ defaults until then |
| Verifier adds cost/latency; second model profile needed | Budget/latency targets missed | ADR-D1 selection procedure; batching |
| Heuristic injection detector has false positives on security-related code | Extra verifier calls, noisy summary notices | Flag-not-drop policy (B.2.3); track rate |
| GitHub review POST is all-or-nothing on 422 | Partial publication | C.5 fallback to summary-with-labels |
| GitLab details unverified (OQ-C9, OQ-R5, OQ-C7) | Adapter rework | Verify on live instance before the adapter ticket |
| Many new versioned identities (context v2, prompt v4, output v3, result v3, verifier v1, rules v2) | Migration complexity; frozen-bundle digests | Follow PROMPT_VERSION_CONTRACT; one activation gate |
| Project `.venv` is stale (Python 3.12, missing deps) vs `requires-python >= 3.14` | Local tooling drift | `uv sync` on a 3.14 interpreter (environment issue, not spec) |

## 7. Open Questions awaiting approval

RULES §11: OQ-R1 … OQ-R8. CTX §E.2: OQ-C1 … OQ-C11. Owners as listed there (tech lead, security, VCS integration owner, backend).
