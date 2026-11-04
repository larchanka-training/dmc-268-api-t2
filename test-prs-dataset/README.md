# Gold benchmark corpus and reproducible evaluation (#15)

23 cases: the original 22 synthetic PR diffs (5 security, 4 resource/performance,
5 correctness, 4 maintainability, 4 clean) and one real public security fix,
labelled clean for the defect it fixes. Cases are small curated examples, not a
representative model-quality sample. Synthetic examples and labels were
contributed on `feat/golden-samples` at `12718e24c03904b4efb28f0eb29f687d183c10e4`;
that history is preserved.

The real case is the production hunk from
[psf/requests 74ea7cf](https://github.com/psf/requests/commit/74ea7cf7a6a27a4eeb2ae24e162bcc942a6706d5)
fixing [proxy authorization disclosure (CVE-2023-32681)](https://github.com/psf/requests/security/advisories/GHSA-j8r2-6x86-q33q). `meta.json` pins source
repository, path, commit, URL, license and modifications (file headers added,
production hunk selected). Apache-2.0 is retained in `licenses/`. The fix's removed
insecure line is not a newly introduced bug. Clean labels apply to the selected
change, not the entire upstream project. No private code is included.

## Data and contracts

`manifest.json` fixes case ordering. Each `fixtures/<id>/` contains:

- `pr.diff`: unified Git diff, accepted by production `app.vcs.diff.parse_diff`;
- `meta.json`: title, description, language/category, public source attribution;
- `expected_findings.json`: manually curated path, side, line range, category,
  severity; clean examples use `[]`;
- `golden_response.json`: handwritten **replay**, not recorded model output.

Canonical nullable `rule_ref` and `proposed_diff_fix` were explicitly added to
replay findings when adapting the original corpus to provider schema v2. The
harness never inserts/coerces these fields into model responses.

Schema comes from the verified bundle's `PromptBuilder.output_schema_json`,
including its canonical Finding closure. There is no independent eval schema or
system prompt. Replay/deterministic default to verified v3, provider/result v2;
live uses its explicit runtime manifest's prompt/digest/schema. Historical
prompt bundles and schemas are unchanged.

## Reproduce without model access

From the API repository root after `uv sync --frozen --dev`:

```bash
uv run python -m scripts.eval_harness --mode offline --report-out eval-offline.json
uv run python -m scripts.eval_harness --mode deterministic --report-out eval-deterministic.json
```

`offline` checks handwritten response schema/coordinates and scoring.
`deterministic` sends the real verified prompt through the production Gateway and
OpenAI-compatible client with a local HTTP replay transport. It checks prompt and
provider contract compatibility, without external requests or credentials.
Neither mode measures LLM quality. Perfect fixture P/R is a regression baseline.
CI runs both after the normal application suite, which already includes auth,
CSRF, session, authorized ReviewJob creation and idempotency tests. The report is
uploaded as `gold-benchmark-reports`; CI never enables live evaluation.

The machine-readable report includes per-case and per-category TP/FP/FN,
precision/recall/F1, schema validity and grounding, dataset content SHA256,
prompt/bundle/schema/config identities, matching policy, mode/model/provider,
requests, charged tokens and cost. Dataset SHA covers manifest, per-case inputs,
labels, replays and license files; output location is excluded. Fixed content and
configuration produce identical offline reports. `--filter <category>` selects a
subset; the report records it and retains the full corpus digest.

Matching policy `path-side-category-overlap-greedy-one-to-one-production-anchor/v2`:
only schema-valid predictions with a changed start and entire range within the
same hunk can match (trailing context allowed), using production
`has_changed_anchor`; path, OLD/NEW
side and category must match and inclusive ranges overlap. Manifest/prediction
order defines greedy one-to-one matching; duplicates and ungrounded predictions
are FP, unmatched labels FN. Free text and severity are not used for matching.
This transparent policy is suitable for these one-finding curated cases, not a
semantic-equivalence oracle for arbitrary multi-finding PRs. Zero-denominator
precision/recall/grounding are 1 (100%); counts accompany percentages.

CLI exits nonzero for invalid schema/anchors; offline/deterministic also reject
FP/FN. Live reports measured accuracy without inventing a perfect-quality SLO.

## Live acceptance: explicitly gated, not yet performed

Only run after approval of the exact provider/model, public corpus and spend
limit. Configure an explicit trusted runtime JSON using the existing runtime
manifest contract (profile URL/model allowlist, credential **environment name**,
prompt version/digest, generation and token/request budgets). Do not embed keys
in JSON or use a model alias without recording its version. Credentials resolve
through the runtime's existing environment mechanism; old `EVAL_LLM_*` variables
are not a live authorization path.

```bash
uv run python -m scripts.eval_harness --mode live --allow-live \
  --runtime-manifest /absolute/path/approved-runtime.json \
  --max-cost-usd <approved-cap> \
  --input-usd-per-million <approved-input-price-ceiling> \
  --output-usd-per-million <approved-output-price-ceiling> \
  --report-out eval-live.json
```

All five flags are required. Before credentials or HTTP, the harness rejects a
cap lower than all selected cases' frozen run token allowances priced at the
explicit ceilings. Production Gateway retries share each case's existing
RunBudget/deadline. `cost_usd` estimates charges from actual token receipts or
retained reservations for unknown/failed usage, at the supplied price ceilings;
it is not a provider invoice or a guarantee against provider overbilling. Use
provider account limits as well for live acceptance. Reports never include raw
responses, prompts, source or credentials; schema diagnostics include safe
validator codes only.

Live scope is the **initial verified prompt/Gateway**. It does not claim worker
admission/fencing, format repair, publication, AST or VCS rules loading; those
have separate integration coverage and #23/#25/#27 acceptance. A real model run,
model-specific baseline and rules/no-rules comparison remain unperformed until
an authorized target/spend is supplied and those dependencies are integrated.
