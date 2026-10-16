# Prompt identities during PR29 / main integration

Status: integrated local candidate. V3 is registered and supported by explicit
trusted admission and preflight. Production worker/dispatcher activation is unchanged.
Source pins: PR29 `fe08f5f962ef7f165567eebdf719db64ce6ca712`,
main `33b7a4e452368cd39c1b626e3130dc87669925ff`.

## Historical identities

| Prompt | Renderer | Provider schema | Result schema | Bundle digest |
| --- | --- | --- | --- | --- |
| PR29 v1 | `review-renderer-v1` | `llm-output/v1.json` | `review-result/v1.json` | `9a11bd52d3a375546bbeaec5c946a4127522d68b2ccc8d812f34964520afc199` |
| PR29 v2 | `review-renderer-v2` | `llm-output/v1.json` | `review-result/v1.json` | `a09dffb11fdd1481888eab77d2530e135e998765e22f9fc254a80cc94faef50b` |
| v3 | `review-renderer-v3` | `llm-output/v2.json` | `review-result/v2.json` | `ffa277ffd51c61bcfd354413e8845a57493c93780b67d319685809d1f612ec4e` |

Historical system prompts, few-shots, repair inputs, renderers, schemas and
manifest descriptors retain their original bytes and SHA256. This includes
`v1/few_shots.json`, which Git merged without a conflict. Both historical
few-shot files have SHA256
`10979e736d31cc99ba35b2ce9b926a153d030ee7e6462c98616beddc1f0fd15a`.
PR29 v2 renders captured instructions as a JSON array inside
`<repository_instructions>`; it does not acquire main's `<repo_rules>` behavior.

## Provider parsing

Provider schema identity is independent of prompt version. The explicit
`provider_output_model(schema_path)` selector returns the historical
`LLMReviewOutputV1` for `schemas/llm-output/v1.json`, and the existing main
`LLMReviewOutput` for `schemas/llm-output/v2.json`. Unknown identities fail closed.
Historical `FewShot` uses the v1 wire model, after the loader's unchanged verified
JSON Schema validation. The selector alone does not perform JSON Schema
validation; a caller must validate the original payload against its selected
verified schema before model parsing.

Provider v1 forbids `rule_ref`. Provider v2 requires `rule_ref`, including an
explicit null when no rule applies. Do not insert, remove, rename or coerce fields
to make another schema accept a payload; do not infer versions from field
presence, try parsers in sequence, or use a permissive union of wire models.
Runtime selects the parser from the verified provider schema. Only after raw JSON
Schema validation and version-specific parsing does the deterministic validator
project v1 findings into the canonical internal model with a null rule reference.

## V3 verified closure and activation

New v3 system/few-shot source material comes from pinned main v2. V3 repair
inputs and renderer source have their own attested identities. Historical
renderers and artifacts remain byte-identical to PR29, including repair inputs.
The loader enforces exact prompt/renderer/provider/result mappings and rejects
mismatched descriptors. Few-shot parsing follows the verified provider schema.

V3 renders frozen canonical repository instructions inside `<repo_rules>`.
Metadata, diff, rules, invalid answers and Reflection finding text neutralize
all six protected delimiter names. Rules drop whole only after all metadata
reductions fail, and the resulting prompt carries a limitation and reports the
rules actually sent. The final repaired/overflow prompt owns the `rule_ref`
whitelist. Unknown labels are rejected by deterministic validation.

`AdmissionSelection` accepts v1/v2/v3, trusted manifests pin a bundle digest, and
preflight verifies all supported closures and saved active-job configurations.
Historical jobs resolve their saved prompt/policy/settings identity. No default
switch enables v3: use an explicit trusted admission selection and matching
verified digest. Existing LLM defaults remain v1; resolver defaults remain v2.
Neither worker nor dispatcher is enabled in production by this integration.

For supported in-memory execution, `review_chunk(..., rules=...)` binds a new
immutable verified v3 builder. Durable execution rejects rules overrides and
uses only frozen configuration instructions. Reflection requires verified prompt
identity v3 and an in-memory budget; historical v1/v2 never run the verifier.
Reflection uses its own schema
and the context/rules actually sent, including after repair; durable composition
disables Reflection and retains the PostgreSQL budget ledger. The frozen policy
DTO and its digest have not acquired a Reflection field.

## Result persistence and recovery

`ReviewChunkResult.to_wire()` dispatches by the declared result schema version.
V1 omits null `rule_ref` and rejects non-null references; v2 emits required
nullable references. Unknown versions fail closed. The adapter propagates the
verified bundle's result version. The analysis handler validates both canonical
result versions and keeps their original schema versions, usage and fingerprints.
Migration 0014 adds nullable `finding.rule_ref` after 0013; historical migrations
are unchanged. Retained v1/v2 results can be read and reused without another LLM
call, including terminal recovery after ContextPayload retention has removed
its source payload. No stored result, fingerprint, policy or digest is rewritten.
