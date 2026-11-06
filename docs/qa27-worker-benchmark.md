# Scripted worker acceptance (QA27 / benchmark15)

This is deterministic integration evidence, **not measured model quality**.
The default Gateway offline/deterministic/live commands remain unchanged.

## Worker report

Use an explicitly disposable, migrated PostgreSQL18 database and Redis8 through
`DMC268_TEST_DATABASE_URL` and `DMC268_TEST_REDIS_URL`. If setting `POSTGRES__*`
for migrations, point them at that same database. The driver creates synthetic
users/repositories and admits real jobs. It reads and validates durable results
and receipts while processing, then serializes that evidence into the report.
Finally it deletes only its created repository/actor UUID graphs, including on
failure, so benchmark fixtures cannot block the existing migration roundtrip or
leak publication work into another test. It never drops a database or resets shared
services. Redis keys use a unique
campaign prefix and only those keys are removed afterward.

```bash
uv run python -m scripts.eval_harness --scope worker --mode deterministic \
  --report-out eval-worker-scripted.json
```

The default worker campaign runs the same24 cases in three passes:

1. Verified initialv4/schema2, AST, absent root rules, Reflection allowance0.
2. Same configuration/code/AST, fixed generic base Security/Performance rules,
   allowance0.
3. Exact same source/rules snapshots as pass2, allowance1.

The original model/routes/temperature/language/ignores/token/request/deadline
limits and pinned verifier remain fixed. Reflection consumes the existing ledger
allowance. `--worker-pass baseline|rules|reflection` or `--fixture <id>` selects a
small troubleshooting slice; no additional paid pass is implied.

The driver reuses shared admission, personal credential revalidation, rules
materialization, outbox, Redis adapter, prepared frozen analysis worker and Gateway.
Only external HTTP is scripted. Every provider response requires a committed
RESERVED ledger row. There is no publication worker in the benchmark; GitHub
writes are rejected. Model title is the existing opaque case hash and body is
empty. Human fixture metadata/labels are not input instructions.

Persisted canonical resultv2 is validated **before** projecting its
summary/findings/limitations into the existing matcher. The report includes
TP/FP/FN/P/R/F1, schema/config/lockfile/prompt/verifier/source identities, actual
stored results, limitations and known/unknown/charged token receipts. Zero USD
means scripted transport: those receipts are not a provider invoice.

`quality_measurement=false` is mandatory for these reports. Golden answers are
scripted provider responses and must never be presented as quality measurements.
The default Gateway live command still measures only its documented initial
Gateway scope. The separately gated worker live command below exercises the same
worker, AST and three-pass comparison with real provider HTTP. Neither actual
provider measurement nor bot publication was executed by this change.

## Prepared live worker command

Use the same disposable migrated PostgreSQL/Redis setup described above. Prepare
an explicit nonsecret v4/schema2 manifest from this pinned checkout; this does not
edit the historical staging manifest or deploy anything:

```bash
uv run python - <<'PY'
import json
from pathlib import Path
from scripts.eval_harness.worker import runtime_values
Path('.scratch/qa27-live-v4.json').write_text(json.dumps(runtime_values(), indent=2) + '\n')
PY
```

Before an actual run, verify current provider prices are at or below the declared
positive input/output price ceilings. The existing approved destination is
Eurorouter `https://api.eurouter.ai/api/v1`, model `mistral-small-4`; credentials
come only through the existing `LLM__EUROROUTER__API_KEYS` mechanism. Do not copy
keys into commands, manifests, journals or reports.

```bash
uv run python -m scripts.eval_harness --scope worker --mode live --allow-live \
  --runtime-manifest .scratch/qa27-live-v4.json \
  --max-cost-usd 1 --input-usd-per-million 0.25 --output-usd-per-million 1 \
  --campaign-journal .scratch/qa27-live-once.json \
  --report-out .scratch/qa27-live-report.json
```

In worker scope `--max-cost-usd` is the **per-pass** ceiling (at most$1); the
three-pass campaign must reserve at most$3. The unchanged full run allowances at
these price ceilings reserve$0.864/pass,$2.592total. This is a conservative ceiling
estimate, not an invoice or a statement of current prices. Gateway scope keeps
its original single-invocation cap semantics.

Live worker requires all24 approved cases/all3 passes, the original corpus digest,
attested source bundle, exact approved v4 runtime/model/routing/limits and verifier.
It rejects altered/partial selections before resolving provider credentials.
Only approved provider HTTP uses the network; GitHub reads remain local public
fixtures and GitHub publication is absent. Golden responses are never dispatched
by the live provider path.

The private execution journal is created exclusively before provider HTTP, reserves
all three passes, and records actor/repository/job identities durably before each
worker invocation. Any existing journal refuses another run, including completed,
interrupted or uncertain campaigns. Do not delete it or choose a different path
to repeat this authorization. There is no automatic campaign recovery/fourth pass;
existing bounded Gateway retries stay inside each job's existing allowance.

Live mode retains durable database results/attempt receipts on success or error.
The journal/report contain identities, aggregate receipts and safe states, never
keys, headers, raw prompts/responses or exception strings. Known terminal invalid
model results contribute invalid-case/FN measurements; every stored canonical v2
result is validated before projection. Shape-validity percentages describe final
canonical-result projection validity, not a raw HTTP JSON-parsing denominator.
Unknown usage stays charged at its retained
reservation, and uncertain execution cannot be replayed. Inspect retained receipts
before making any later decision about an incomplete run.

Only real provider execution can set `quality_measurement=true`. Public CLI tests
inject provider HTTP, label `provider_transport=injected-test`, keep quality false,
and clean only their own synthetic UUID graphs afterward. Both real measurements
and real bot acceptance remain unexecuted:0real calls/$0.

## Source identity

The original24-case corpus/labels/license tree and its digest stay unchanged.
`test-prs-dataset/worker-sources` is a separately attested bundle: licensed full
Requests parent/head bytes and exact full-added-file derivations for23synthetics.
`.py.txt` assets are read/parsed, never imported or executed. The original public
provenance/NOTICE/LICENSE and synthetic mapping are retained.

Transport diffs replace only the original synthetic placeholder blob header with
the attested real blob identity; hunk contents/ranges remain unchanged. Wrapper
trees/commits use actual Git object hashes and are explicitly synthetic, never
claimed to be upstream Requests snapshots. Absent/present rules use paired commits
with identical code blobs; pass2/3 share exact source identities. Code/AST semantic
comparison excludes only declared `commit_sha` revision identity. The full public
Requests file exceeds the unchanged extractor node cap and legitimately uses
degraded bounded context; all passes retain that same behavior.

## Literal full chain and modest load

`tests/test_webhook_review_publication_e2e.py` characterizes four existing states
(absent/valid/empty/adversarial root rules) through a signed webhook's permanently
bound **same job**, real analysis/Gateway, one scripted GitHub COMMENT, duplicate
publish delivery without another POST, retained remote references and real JWT
authenticated terminal SSE. It does not seed/refreeze a replacement review or
substitute manual Start for automatic intake. Existing permission/fence/recovery
matrices remain in their original suites.

The existing load smoke still uses32 unique requests/concurrency4 and16 duplicate
requests/concurrency8, with Level1 automatic-review-off assertions. Optional
`DMC268_LOAD_REPORT=eval-webhook-load.jsonl` saves the existing observed counts and
latencies. These are ASGI/BackgroundTasks characterizations, not a production SLA,
analysis capacity result or soak test.

CI runs the full-chain/load tests in the ordinary suite and then creates the
scripted worker report once. Existing offline/Gateway reports, quality, chaos and
migration checks remain. The existing benchmark artifact additionally includes
`eval-worker-scripted.json` and `eval-webhook-load.jsonl`.

No real LLM call, spend, bot POST, deployment or team notification is performed.
The separately approved ceilings (three passes at≤$1each/≤$3total) and exact bot
acceptance target remain unexecuted real-run gates, not claims made by scripted CI.
