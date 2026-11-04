The JSON file `analysis-runtime.v1.json` is a nonsecret example, not a deployment
configuration. Its `.invalid` endpoints and test model selectors must be replaced
by the approved inventory. Set `ANALYSIS__CONFIG_PATH` to a server-owned read-only
file; HTTP request fields cannot select or override it. No service is activated by
providing this file. Existing session/provider authorization remains required.

Format v1 requires exactly `schema_version`, `profiles`, and `admission`. Profiles
use the existing exact references `eurorouter/review-v1` and `ollama/review-v1`.
Each descriptor requires provider, URL, explicit nonempty model allowlist, and
`credential_env` (an environment variable name for Eurorouter; null for auth-free
Ollama). Eurorouter requires HTTPS. URLs cannot contain credentials, query strings,
fragments, or whitespace. Unknown fields, duplicate JSON keys, incomplete policy,
provider/ref mismatch, models outside the allowlist, and unattestable model pins
are refused. Do not put credential values in the JSON file.

Admission explicitly selects the primary model route, complete typed policy,
prompt version, and verified bundle digest for new jobs. The policy's
`repository_settings_digest` is an explicit template marker in this example;
the trusted resolver replaces that field with the checksum of the selected
repository settings. No historical field is filled from this template.
Historical ReviewJobs retain their own model/digest, routing/fallback, generation,
prompt identity, repository checksum, request/token/deadline and retry policy.
Keep their profiles, model selectors, prompt artifacts and renderers available.

`load_runtime_configuration()` reads only the path and nonsecret file. Its identity
is SHA-256 of the validated document serialized as UTF-8 JSON with sorted object
keys, compact separators and no NaN. Whitespace and object key order do not affect
the identity; list order remains significant. Bind this identity to the immutable
image/release. Loading a descriptor never reads credential environment values.

API validation needs no LLM keys. `runtime.offline_preflight(factory, prompt_root)`
loads verified v1/v2 bundles and their renderer/schema closure, verifies the
selected digest, and checks pending jobs through the real frozen reader. The scan
reads at most `scan_limit + 1` IDs (default100, maximum1000); an incomplete scan is
blocking. Increase the explicit bounded limit or arrange a separately approved
compatible drain when that bound cannot cover the backlog. No rows are updated,
leases acquired, budget reserved, queues read or ACKed, or providers contacted.
A legacy NULL marker is reported as `legacy_zero_http_terminalization` and can use
the existing fail-closed handler. This warning never asserts zero historical cost.
Other unsupported history blocks readiness and must retain a compatible runtime;
do not rewrite saved identities or budgets to make it pass.

Restricted `runtime.deployment_preflight(...)` combines the offline checks with
local credential readiness for admission and required pending-history routes.
Give only that restricted environment and analysis execution the required secret
variables. Keys are comma-separated printable nonblank ASCII values; malformed
or empty entries are refused. This proves local configuration, not provider-side
key validity. Reports contain only nonsecret manifest identity, scan counts,
profile references, job IDs and error codes.

The analysis CLI (Stage 8C-04-01) awaits
`prepare_postgres_analysis_worker(...)` before receiving any delivery.
It validates offline compatibility and credentials, then returns the existing
bounded worker. A direct
`build_frozen_gateway(...)` independently enforces execution credentials before
any inference, including fallback routes. Ollama remains auth-free.

With an approved manifest, required credentials, migrated PostgreSQL and Redis
configured in the process environment:

```bash
uv run python -m app.analysis_worker --help
uv run python -m app.analysis_worker --once
uv run python -m app.analysis_worker --poll-interval 1
```

`--help` requires no settings, manifest, credentials or service connections.
`--once` processes at most one new analyze delivery; an empty or deferred pass
exits successfully. Without `--once`, execution is sequential and idle passes wait
for `--poll-interval` seconds (default 1; finite range 0.01–60). A false result
does not imply an empty pending list. Each process boot uses a fresh owner UUID.
The CLI owns and closes its Redis/HTTP connections and database engine. SIGTERM
and SIGINT interrupt idle waiting and prevent another receive; full in-flight
shutdown is deferred. Startup/runtime failures exit nonzero and log only the error
type and an allowlisted reason, without exception text.

This stage does not schedule pending claiming, periodic PostgreSQL recovery or
transport maintenance, and does not execute publication. Compose/CD do not start
this CLI automatically. Deployment and activation remain gated on independent
acceptance and the remaining lifecycle work.
