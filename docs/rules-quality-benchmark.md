# QA-04B: rule-specific Precision/Recall

This independent experiment uses 12 original public synthetic examples under
`tests/fixtures/rules-benchmark/`: SEC-1 (tenant isolation), PERF-1 (explicit HTTP
timeout), STYLE-1 (public error codes prefixed `APP_`). Each has two violations
and two clean controls. STYLE-1 uses the production `maintainability` category.
Source `.py.txt` assets are data, never imported. Labels were written by hand;
exact old/new bytes, diff, labels and scripted responses are independently
validated. The original 24-case corpus, Gold scorer and QA-04A remain unchanged.

## Reproduce without provider network

Use only an explicitly disposable migrated PostgreSQL18 and Redis8 on loopback.
Do not tunnel staging/production services through localhost. Export their test
DSNs as `DMC268_TEST_DATABASE_URL` and `DMC268_TEST_REDIS_URL`; migrations must use
the same disposable database through `POSTGRES__*` settings.

```bash
uv run python -m scripts.eval_harness.rules_runner \
  --mode deterministic --disposable --output-dir .scratch/qa04b/report
```

This executes 24 separate jobs through real admission, frozen rules
materialization, outbox, Redis queue, production Worker, Gateway and PostgreSQL
accounting. Only external HTTP is scripted. A committed RESERVED debit is checked
before every provider dispatch. GitHub reads use synthetic local snapshots;
GitHub writes fail closed and no publication worker runs. Scripted execution
cleans only its own UUID graphs and Redis prefix. CI runs the dataset/scorer tests
and this command against its disposable services, uploading a separate
`rules-quality-report` artifact. It performs zero paid model calls.

A and B use identical code/diff, model, prompt v4, provider and canonical schemas,
temperature, token/request limits, output language and disabled Reflection.
Only root rules and resulting source/settings identities vary. The runner checks
actual frozen rules digest, parser state/content digest, frozen base SHA and
analysis snapshot. The report refuses missing/duplicate pairs, missing source
context, code/AST semantic drift, diff drift and policy drift. AST comparison
excludes only revision `commit_sha`; full source identities remain recorded.
Neutral repository names and PR titles do not encode fixture IDs. Labels, IDs,
fixture metadata and scripted answers are never provider input or metadata.

## Scoring and interpretation

`provider_quality` evaluates the **final observed provider response before Worker
filtering** as a model diagnostic. `canonical_quality` evaluates the **persisted
canonical findings** as the delivered result. Each includes detection and
attribution scores per case and per pass, rule-attribution scores in `by_rule`,
fixture cohorts in `by_category`, plus B-minus-A deltas. A
provider detection TP that Worker drops is a canonical FN, never a canonical TP.
This preserves evidence of unknown/wrong rule references that production correctly
rejects without crediting them as persisted findings. All attempt
schema/grounding diagnostics are retained, including repair attempts; only the
final response contributes quality counts. The bounded experimental runner
rejects multi-chunk results instead of accidentally scoring only the last chunk.

Detection matches path, diff side, category and overlapping changed-line ranges.
Schema-invalid responses produce FN and explicit errors. Ungrounded findings
produce FP and cannot consume labels. Maximum one-to-one matching prevents
duplicates increasing TP. Attribution independently maximizes exact `rule_ref`
matches; ties maximize remaining location matches to classify missing/wrong refs.
A correct ordinary finding with null reference contributes detection TP and
attribution FN, without inventing a rule-specific FP. A wrong non-null reference
contributes attribution FP/FN; an unmatched rule-bearing finding is unjustified.
Clean cases and cases with false positives are counted separately. All zero
Precision/Recall/F1 denominators produce **0**, including empty clean cases;
aggregate metrics are micro counts, not an average of case ratios.

`by_rule[rule_ref][pass].attribution` distributes individual findings using the
same one-to-one attribution matching as the overall counts. Exact matches give
TP to their reference; every nonempty prediction without an exact match gives
FP to its predicted reference; every expected label without an exact match gives
FN to its expected reference. This includes duplicates, clean-case predictions
and ungrounded predictions (which cannot match). Unknown nonempty references
remain explicit keys under their literal values, without a separate bucket.
Missing/null/empty references add no rule-specific FP and preserve expected FN
and existing missing diagnostics. An omitted required field still fails schema
validation, so that response contributes FN and schema errors, as before.
For each target and pass, summing `by_rule` TP/FP/FN equals the overall attribution
micro-counts; precision/recall/F1 are recomputed from each rule's counts.

For example, expected SEC-1 predicted PERF-1, followed by expected PERF-1
predicted PERF-1, produces SEC-1 TP/FP/FN = 0/0/1 and PERF-1 = 1/1/0
(Precision = 0.5). Both predictions can remain detection TP regardless of refs.
`by_rule` no longer includes detection, clean-case or validation metrics based on
fixture category. Those complete-case aggregates are retained in `by_category`,
keyed by the fixture's declared category; a cohort's attribution FP may cite
another rule. Overall detection, per-case diagnostics and pass-level
missing/wrong/unjustified diagnostics keep their existing meaning. Both report
formats include `per_rule_semantics` explaining this separation.

Both JSON and Markdown explicitly separate the two quality targets and include
`filtered_findings`: provider findings absent from the canonical result, matched
by location, category, masked title and rule reference with duplicate counts
preserved, using the existing production secret masking for title comparison.
This records removals, including deduplication; it does not infer a rejection
reason. Severity and evidence normalization alone do not count as removals.
The legacy JSON `score`, top-level `passes`, `by_rule` and `delta` remain aliases
for the provider diagnostic, identified by `scoring_target`.
The new top-level `by_category` is also a provider diagnostic alias.

Both JSON and Markdown include per-case A/B scores, per-rule aggregates, deltas,
canonical validation, provider attempt diagnostics, frozen identities, model,
prompt/config/schema/dataset/source/rules digests and actual/reserved token usage.
Serialization is stable for the same evidence; new runs have fresh review/chunk
UUIDs and observed latency, so complete JSON files need not be byte-identical.
Metrics and frozen content/configuration identities are reproducible.
Reports have `quality_measurement=false` for deterministic or injected-provider
runs. Scripted equal or improved scores establish no model quality improvement.
The report makes no statistical significance claim: 12 curated examples are a
limited experimental evaluation, with unresolved uncertainty and no universal
model benchmark claim. Even a real-provider positive delta needs independent,
larger held-out replication. A model alias with no provider model digest cannot
guarantee unchanged provider weights over time.

Example deterministic Markdown excerpt (scripted findings, not measured quality):

```text
mode: deterministic; quality_measurement: false

## provider_quality

| Pass | TP | FP | FN | Precision | Recall | F1 |
| baseline | 6 | 0 | 0 | 1.000 | 1.000 | 1.000 |
| rules | 6 | 0 | 0 | 1.000 | 1.000 | 1.000 |
```

Scripted transport deliberately repeats its prepared answer in both passes,
including rule references; baseline canonical filtering appears in
`canonical_quality` and `filtered_findings`.
A zero detection delta from these answers is pipeline evidence only.

## Prepare a future live run — not executed by this change

The existing $3 authorization belongs to the other 24-case campaign. It is not
inherited here. The original `run_worker()` allowlist/digest/cost checks remain
untouched. This runner requires **new explicit campaign authorization**, current
verified positive price ceilings and disposable infrastructure.

Prepare a nonsecret authorization JSON with these exact identity fields and
independently approved limits. Placeholders must be replaced after inspection;
this example is deliberately disabled:

```json
{
  "allow_live": false,
  "dataset_sha256": "<dataset_digest(dataset)>",
  "source_bundle_sha256": "<load_sources(dataset/sources, dataset)[1]>",
  "rules_sha256": "<SHA256 of rules.md bytes>",
  "runtime_sha256": "<digest(runtime_values())>",
  "model": "mistral-small-4",
  "destination": "https://api.eurouter.ai/api/v1/chat/completions",
  "input_usd_per_million": "<verified-positive-input-price-ceiling>",
  "output_usd_per_million": "<verified-positive-output-price-ceiling>",
  "prices_verified_at": "<timezone-aware-ISO-timestamp-within-24-hours>",
  "price_evidence": "<reviewed-provider-price-source-and-observation>",
  "max_cost_usd": "<newly-approved-total-campaign-cap>",
  "max_requests": 192,
  "journal_path": "<absolute-once-only-journal-path>"
}
```

The maximum conservative reservation is 24 times the frozen per-job input/output
token allowances priced at the approved positive ceilings. `max_cost_usd` must
cover that reservation; `max_requests` is a positive integer at most 192 and is
checked before each HTTP dispatch, including retries. Declaring prices is an
operator attestation, not automatic verification of the provider's price page.
Credentials remain in the existing `LLM__EUROROUTER__API_KEYS` environment source;
never put them in approval JSON, commands, reports or journals.

After **separate explicit authorization** sets `allow_live` to true, the future
command is:

```bash
DMC268_TEST_DATABASE_URL='<disposable-local-postgres-dsn>' \
DMC268_TEST_REDIS_URL='<disposable-local-redis-dsn>' \
uv run python -m scripts.eval_harness.rules_runner \
  --mode live --disposable \
  --approval '<approved-campaign-json>' \
  --journal '<absolute-once-only-journal-path>' \
  --output-dir '<private-report-directory>'
```

Live reports contain complete provider responses. The writer creates or tightens
the output directory to `0700` and both report files to `0600` before writing
their content, including when replacing existing reports. Deterministic CI
artifact paths and ordinary file permissions are unchanged.

The fsynced no-overwrite journal is created before execution, bound to the
approval's exact path and updated with owned resources/jobs before Worker calls
and request reservation before provider HTTP. Existing journals refuse replay,
including uncertain or completed campaigns. Do not edit/delete journals or issue
a replacement approval to replay the same authorization. Live database results
and usage receipts are retained on success/error. Unknown usage remains charged
at its reserved upper bound and prevents a successful measurement report.
Only confirmed real-provider dispatch can set `quality_measurement=true`;
injected test transport remains false. Provider endpoint/model are checked at
dispatch and redirects are disabled. There are no real GitHub publication POSTs.
