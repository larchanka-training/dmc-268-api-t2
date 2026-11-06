# PostgreSQL and Redis backup / restore

These scripts create private, host-local recovery files. They do not install a schedule,
copy backups off-host, encrypt them, or provide a cross-service transaction. Protect the
backup directory and arrange separate off-host storage before relying on host recovery.
The PostgreSQL procedure in [the approved proposal](proposals/postgres-backup.md) remains
unchanged. Redis backup does not enable AOF, a volume, or automatic replay of jobs.

## Daily backup

Run on the Linux staging host with Bash, GNU date, gzip, flock, Docker and Docker Compose.
Both scripts accept the Compose project and environment-file path. They use the existing
`db` / `redis` services in the rendered `docker-compose.staging.yml`. The Redis
script supports both the flat installed layout alongside Compose and a checkout's
`scripts/` directory one level below it. The existing deploy workflow installs only
the PostgreSQL script/schedule; copying and scheduling Redis remains an explicit
operator step, not an automatic deployment feature.

```bash
# After approving the host and deployment directory:
install -m 755 /path/to/checkout/scripts/backup-redis.sh /path/to/deployment/backup-redis.sh
/path/to/deployment/backup-postgres.sh dmc268-staging /path/to/staging.env
/path/to/deployment/backup-redis.sh dmc268-staging /path/to/staging.env
```

PostgreSQL retains its existing daily/predeploy behavior. Redis captures an RDB using
`redis-cli --rdb`, then validates the received bytes with the Redis service's
`redis-check-rdb`. Only a validated file replaces
`backups/redis/redis-YYYY-MM-DD.rdb` under the selected Compose directory. The last seven date-named Redis snapshots
remain after a successful backup; other files are untouched. A failure keeps the previous
snapshot and removes the partial file. A busy per-script lock skips that run. Directories
are mode 0700; newly created snapshot, lock and status-log files are mode 0600. The Redis
log keeps the last 30 fixed status/stage/exit-code lines and excludes raw tool output
and credentials. Failed capture/validation reports `snapshot` / `validate-rdb`;
unavailable `flock` fails with `flock exit=127` rather than a successful busy-lock
skip. Standard tool directories are appended to PATH for cron, preserving an
explicitly supplied executable path. Redis/Docker stderr remains suppressed deliberately.

Example cron entries below are documentation only; install them deliberately as the
same account that owns the deployment. The environment file must already be protected.
There is no password in either command line. Check both exit status and backup age.

```cron
15 2 * * * /path/to/deployment/backup-postgres.sh dmc268-staging /path/to/staging.env
25 2 * * * /path/to/deployment/backup-redis.sh dmc268-staging /path/to/staging.env
```

## Restore deliberately into fresh targets

A PostgreSQL dump and a Redis RDB taken at different times are not an atomic pair.
Before a real recovery, stop all writers/consumers: API, analysis worker, publication
worker, webhook worker and outbox dispatcher. Keep the forward-compatible application
image/schema and inspect retained job, receipt, outbox and publication state before
resuming. Restoring Redis must never authorize another paid call or blindly repost a
review whose external outcome is unknown. Reconcile those outcomes through the existing
ledger/publication recovery process.

Use new disposable targets first. Verify the selected paths and ownership; never restore
over a running/shared instance. Keep the source backups immutable during the drill.

For PostgreSQL, create an empty database compatible with the dump and run the approved
plain-SQL restore with `ON_ERROR_STOP` and one transaction. Check gzip integrity first:

```bash
gzip -t /private/backups/dmc-YYYY-MM-DD.sql.gz
set -o pipefail
gzip -dc /private/backups/dmc-YYYY-MM-DD.sql.gz |
  docker exec -i OWNED_FRESH_POSTGRES psql -U dmc -d dmc -v ON_ERROR_STOP=1 --single-transaction
```

For Redis, use the matching Redis 8 image to check the RDB, place the validated file as
`dump.rdb` in the fresh target's private data directory, and start that target with
`--appendonly no --dir /data --dbfilename dump.rdb`. With AOF enabled Redis can prefer AOF
instead of the selected RDB; do not reuse an old data directory/AOF. Check startup success,
key values, stream entries, group position and pending entries before permitting consumers.
The CI drill below demonstrates this fresh-container sequence, including claiming a restored
pending entry with its original stream ID. It never flushes a shared Redis or drops a
shared database. Remove only the drill's own containers/volumes after inspecting results.

## Opt-in real restore drill

On Ubuntu/Linux with Docker Compose, existing `postgres:18` and `redis:8` images and the
normal project development environment:

```bash
DMC268_RUN_DOCKER_TESTS=1 uv run pytest tests/test_backup_restore.py -q
```

The two tests run the maintained backup scripts against fresh source services, change
those sources after the snapshot, and restore into separate fresh targets. PostgreSQL
checks Unicode, a foreign key, sequence continuity and corrupt-gzip refusal. Redis checks
Unicode, stream IDs/values, consumer-group lag, the pending entry list and reclaim,
and rejection of truncated RDB bytes. All containers have unique
`dmc268-obs26-backup-*` names and `codex.task=dmc268-obs26-backup` labels; cleanup targets
only those fixtures, including anonymous volumes. No host ports or real credentials are
used. Without explicit opt-in or on macOS the tests skip; a skip is not restore proof.
The real Linux CI drill must pass before declaring restore verification complete.

The portable CLI tests are separate:

```bash
uv run pytest tests/test_redis_backup.py -q
```

These stub the external Docker boundary and cover validated/private publication,
failure cleanup, retention, safe error output and nonblocking locking. They prove the
command contract, not data recoverability, remote durability, or production recovery.
