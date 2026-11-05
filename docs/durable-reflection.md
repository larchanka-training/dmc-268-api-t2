# Durable Reflection (issue #25)

The analysis worker can verify deterministic-valid findings with one structured
logical operation per chunk. It uses the job's existing frozen primary/fallback
routes, requests/tokens/deadline, PostgreSQL ledger and fenced worker lifecycle.
It never creates or rewrites findings: only validated rejected indexes are removed.
Repository rule filtering/severity projection happens before verification.

## Explicit identity and activation

Historical model policy `schema_version: 1` retains its original interpretation
and serialized fields. Mutable environment flags never enable Reflection for it.
The outer frozen configuration envelope remains version1; its digest already
covers the inner model policy. Policy2 requires every existing field and this
additional explicit block:

```json
{
  "schema_version": 2,
  "reflection": {
    "max_reflections_per_run": 1,
    "max_reflections_per_chunk": 1,
    "failure_mode": "fail_open",
    "verifier_version": "v1",
    "verifier_digest": "e583048928ca98e39db959b710a5b0506682159e14412a826814804b2007d690"
  }
}
```

This is a fragment, not a complete runtime manifest. Allowance0 disables the
pass. Positive allowance must not exceed the frozen chunk limit. Policy2 is
explicitly compatible with initial promptv4 only. The current staging manifest
remains policy1/promptv2; this PR does not activate or deploy anything.

The independent `.agents/reviewer/reflection/manifest.json` pins system text,
canonical output schema and the project-import-free renderer by SHA-256. Its
canonical descriptor digest above is checked during admission, retained-job
resolution and preflight. Renderer/path/import checks reuse the initial bundle's
attestation machinery. Initial bundlesv1-v4 are unchanged. Retained incompatible
jobs keep the existing preflight policy: reported as nonblocking safe failures,
then rejected before provider HTTP; admission incompatibility blocks readiness.

## Execution and recovery

The verifier receives the actually rendered final context/rules after any main
format repair or overflow rebuild, plus indexed, validated and secret-masked
findings. v4 neutralizes untrusted metadata/rules/source; the verifier renderer
also neutralizes XML and provider role markers in generated finding strings.
Messages **plus canonical schema** must fit before reserving any allowance.

The first committed `reflection` ledger row claims one logical operation. Under
the existing job/ledger lock, distinct operation IDs are bounded per run and per
chunk. Provider retries/fallback retain that operation ID and the existing
three-attempt cap; each HTTP attempt is separately committed before dispatch.
Unknown usage remains conservatively reserved. A restarted/fenced worker cannot
reset an allowance or settle another execution's receipt. A lost verifier answer
is never replayed against regenerated finding indexes: the chunk safely retains
its new deterministic findings instead of paying for another logical verification.

Exhausted enabled budgets, oversize input, timeout/provider/schema failure keep
the valid deterministic findings and save a visible limitation. Policy2 off adds
no failed-verification limitation. Omitted/unknown indexes retain unmentioned
findings and partial coverage is visible. No independent verifier format repair
is performed. Lease loss, cancellation and configuration/programming errors still
propagate through existing authority checks.

The pinned JSON Schema constrains the verdict/reason enums independently; typed
`ReflectionVerdict` validation then requires `confirmed` + `verified`, or
`rejected` + one of `no_real_consequence`, `pre_existing`, `overflagged_style`.
`ReflectionOutput` also rejects duplicate indexes. A mismatched pair fails typed
validation before findings are filtered and follows the existing fail-open path;
the JSON Schema alone does not enforce that semantic pairing. No schema identity
or verifier digest changes are needed for this existing validation.

For **all durable chunks**, including policy1 and policy2 with Reflection off,
per-chunk result usage comes from the authoritative ledger, including paid invalid
responses and previous attempts. This replaces the partial sum of successful
in-process calls for durable execution. If any input or output usage remains
unknown, that dimension is `None` (wire `null`), not a partial known total;
requests count all recorded attempts. In-memory `RunBudget` keeps its existing
call-based usage calculation. Primary generation provenance remains primary; verifier
provider/model/operation/fence/state/known-or-reserved usage remain in the existing
ledger, with verifier identity in the immutable job policy. Logs use safe codes
and counts, never raw verifier answers, prompts, source or credentials.

## Budgets and rollback

No admission limit increases: requests8, input80000/output16000 per run,
input10000/output2000 per call, context16384, chunks5 and deadline600s remain the
existing defaults. Enabled verification shares these totals; allowance is not a
promise that every chunk can be verified. No paid quality improvement is claimed
by mocked deterministic tests.

Migration0019 widens the policy-shape and attempt-kind CHECKs without backfilling
historical data. Rollback activation first: select policy1 for **new** admission,
retain a compatible reader/worker for policy2 jobs and keep the forward schema.
Draining active jobs is insufficient for a schema downgrade: completed policy2
jobs and historical `reflection` attempt rows also violate the old CHECKs. Do not
delete accounting history or rewrite frozen policies to force a downgrade.

`alembic downgrade 0018` performs the ordinary CHECK narrowing; it is not a
no-op. It succeeds only when all retained rows satisfy the old constraints:
frozen jobs use policy1 and attempt kinds exclude `reflection`. Empty accounting
is sufficient for the attempt-kind constraint, but retained policy2 jobs still
prevent downgrade; nonempty accounting with only old attempt kinds is compatible.
Incompatible retained rows make constraint creation fail. Keep the forward schema
and a compatible reader/worker in that case; disabling new Reflection or draining
active jobs does not make historical rows compatible.
