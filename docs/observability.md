# MVP observability

Metrics are off by default. Existing JSON logs and Sentry remain the error/logging
path; this change does not introduce Loki, another tracing platform or a new
business queue/schema.

## Internal metrics

`METRICS__ENABLED=true` opens the Prometheus listener for the process lifetime.
Defaults: `METRICS__HOST=127.0.0.1`, `METRICS__PORT=9100`. API, analysis,
publication, webhook recovery and outbox dispatcher each expose their own process
metrics. `publication_worker --check-config` stays offline and opens no listener.
Use one process per container; this is not a multiprocess collector setup.

The optional staging override sets the listener host to `0.0.0.0` **inside the
backend network** and publishes no metrics port. Never route it through Caddy or
expose it publicly: there is no authentication on the internal listener.

- `dmc_http_requests_total`: completed HTTP requests, including interrupted streams
  and unhandled failures. Labels are registered route template, bounded method and
  status class; unknown paths share `unmatched`. Bodies/SSE frames are not buffered.
- `dmc_llm_attempt_seconds`: latency around each dispatched provider-client await,
  including errors/timeouts/cancellation. It excludes budget reservation and DB
  settlement. Histogram count is the attempt count; labels are finite provider,
  call kind (`initial`, `repair`, `overflow_rebuild`, `reflection`, `other`) and
  success/error. This is not total review latency or a provider invoice.
- `dmc_queue_lag` and `dmc_queue_pending`: not-yet-delivered entries and pending
  deliveries for the two fixed Redis streams/group. These are not distinct jobs.
  API samples only `XINFO GROUPS` every 15 seconds, with bounded transport timeouts.
  It does not create groups, receive/claim/ACK deliveries or inspect envelopes.
  `dmc_queue_sample_success=0` and NaN values mean unavailable/missing metadata;
  Redis NULL lag is also NaN. ACKed history/XLEN is never called backlog.
- Standard Python process CPU/RSS metrics are available on Linux. The dashboard's
  node CPU/RAM collector uses no host filesystem/PID mounts. On Docker Desktop its
  kernel counters describe the Linux VM, **not macOS**. Process CPU uses one core
  as 100% and can exceed 100%.

No review/request/user IDs, raw URL/query, model names, source code, rules, tokens,
credentials or exception messages are metric labels.

## Optional dashboard

The separate `docker-compose.monitoring.yml` is not used by deployment automation.
It pins verified multi-platform image digests for Prometheus 3.13.5 LTS, Grafana
13.3.0 and node_exporter 1.12.1. `prometheus-client` is pinned to 0.26.0.

After an operator approves the runtime change, supply the existing staging env
file and a dedicated `GRAFANA_ADMIN_PASSWORD` through the normal environment:

```bash
docker compose --env-file /path/to/approved-runtime.env \
  -f docker-compose.staging.yml -f docker-compose.monitoring.yml \
  --profile monitoring up -d prometheus grafana node-exporter
```

This command starts only monitoring services. Apply the opt-in metrics environment
to API/workers through a separately approved ordinary rollout. It does not enable
analysis/publication profiles, admit work, run an LLM or publish a review. Keep the
existing frozen runtime and credentials unchanged.

Grafana is bound only to `127.0.0.1:3000` (use an SSH tunnel for a remote host),
anonymous access/signup/reporting are disabled. Log in as `admin` with the supplied
password; the provisioned **DMC MVP operations** dashboard includes RPS, LLM p95,
queue lag/pending and process/host CPU/RAM. Disabled workers and unknown queue data
show N/A, not zero. Prometheus/Grafana data use separate telemetry volumes, with
seven-day Prometheus retention; business storage is unchanged.

Validation commands (no provider call):

```bash
docker run --rm --entrypoint promtool \
  -v "$PWD/monitoring:/etc/dmc:ro" prom/prometheus:v3.13.5 \
  check config /etc/dmc/prometheus.yml
docker run --rm --entrypoint promtool \
  -v "$PWD/monitoring:/etc/dmc:ro" prom/prometheus:v3.13.5 \
  check rules /etc/dmc/alerts.disabled.yml
```

## Sentry stages and privacy

The existing optional `SENTRY__DSN` controls errors and tracing. A claimed review
delivery owns an isolated `review.analyze` or `review.publish` transaction tagged
with its trusted backend review UUID. Analysis emits direct Diff, Context, LLM and
core persistence DB spans; the DB spans include context-manager commit exit.
Resuming saved context skips work and does not fabricate new Diff/Context spans.
The transaction's acknowledged/deferred outcome is a delivery decision, not a claim
that analysis succeeded. No observer changes leases, fencing, ledger or ACK rules.

Correlation uses the existing review UUID/request logs. This is correlated worker
stage timing, **not** continuous HTTP→Redis distributed trace propagation.

Only the existing logging plus explicit FastAPI/Starlette Sentry integrations are
enabled automatically; outbound HTTP, SQLAlchemy and Redis payload instrumentation
is omitted. HTTP/SQL library log breadcrumbs/events/Sentry logs are suppressed to
avoid reintroducing URL/query/statement contents. Existing body/query/AI/DB-param,
queue-data and locals restrictions remain. Manual spans carry fixed stage names,
not SQL, prompts, diffs or provider responses. Empty DSN remains a no-op.

## Backups, alerts and rollback

See [backup and restore](backup-restore.md) for the existing PG schedule and new
Redis RDB script, private retention, Linux CI restore proof and coordinated restore
limits. No Redis persistence-volume/AOF migration or real schedule installation
is performed here. Backups on the same disk do not survive disk loss; off-host
storage is a separate operator decision.

`monitoring/alerts.disabled.yml` is valid but deliberately not loaded/routed.
No Alertmanager/contact point/channel/secret is provisioned and no notification is
sent. An operator must authorize and configure the team destination, then validate
delivery before enabling rules/Sentry project routing. Dashboard/source CI does
not prove live notification or server rollout acceptance.

Rollback: remove the monitoring override, set metrics disabled and restart affected
processes through the normal approved rollout. Existing business schema/queues and
frozen review configurations are unchanged. Keep backup artifacts and telemetry
volumes until an operator explicitly decides retention/deletion.
