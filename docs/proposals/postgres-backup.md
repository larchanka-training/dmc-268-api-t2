# Daily Postgres backup on the VPS

Decisions for this change are locked. No further grill.

## Current

Deploy (`larchanka-training/dmc-268-api-t2`, `.github/workflows/deploy-staging.yml`) SSHs in and runs Compose. The same job already copies `docker-compose.staging.yml` into the deploy directory and runs `docker` as that SSH user, without `sudo`.

| Git branch | Environment | Deploy path | Env file | Compose project |
|---|---|---|---|---|
| `main` | staging | `/opt/dmc-268-t2` | `.env.staging` | `dmc-268-t2-staging` |
| `develop` | develop | `/opt/dmc-268-t2-develop` | `.env.staging.develop` | `dmc-268-t2-develop` |

There is no `origin/develop`, so only the staging path is in use.

`db` is `postgres:18-alpine`. It publishes no host port. Data is the named volume `db-data`, mounted at `/var/lib/postgresql` inside the container. Deploy writes `POSTGRES__USER=dmc` and `POSTGRES__DB=dmc`. The password stays in the env file on the server. Redis has no volume.

Before this change, nothing in this repo created a dump, a backup directory, or a cron entry.

## Desired

No new GitHub workflow. A `schedule` workflow runs on a GitHub runner. The runner has no Postgres data, so the job would still SSH in, and GitHub may delay or skip that schedule when the repo is idle. The dump has to be created on the VPS, next to the database, including on days when nobody pushes.

The existing Deploy job installs the schedule, the same way it already copies the Caddyfile. `Deploy application` copies the script and runs it with `predeploy` before `alembic upgrade head`. `Ensure Caddy HTTPS` is next. `Install Postgres backup` then refreshes the script and the crontab line and does not run the script. A later deploy replaces that line and does not touch the rest of the crontab. The step does not write `/etc/cron.d` and does not use `sudo`.

| Environment | Script on the VPS | Dump directory | Cron |
|---|---|---|---|
| staging | `/opt/dmc-268-t2/backup-postgres.sh` | `/opt/dmc-268-t2/backups` | `15 3 * * *` |
| develop | `/opt/dmc-268-t2-develop/backup-postgres.sh` | `/opt/dmc-268-t2-develop/backups` | `45 3 * * *` |

The directory mode is `700`. Each dump file is mode `600`. Dumps are not inside `db-data`, and they are not committed. Daily files are `backups/dmc-YYYY-MM-DD.sql.gz`: seven names, one per UTC date, in `backups/` and not under `backups/predeploy/`. An older daily file is deleted only after a new dump has been written and checked, and deletion follows the date in the filename. A second run of the two-argument script on the same UTC date replaces that day's file. `predeploy` writes a new `backups/predeploy/dmc-<YYYYMMDDTHHMMSSZ>.sql.gz` and does not replace an existing daily file. Predeploy files whose UTC date is today or one of the previous six days stay, with no per-day count. They are not part of the seven daily names.

The copy survives a bad migration and a container recreate. It does not survive the VPS disk dying, because the files are on that same disk. Cron uses the VPS local clock. The dump file name uses the UTC date.

Redis is not backed up. This change does not modify the worker command.

## Touchpoints

Three files:

- `scripts/backup-postgres.sh` (new). Source of truth. Arguments are the Compose project and the env-file name. It `cd`s to its own directory, which is the deploy path, and runs:

```bash
docker compose \
  -p "${COMPOSE_PROJECT}" \
  --env-file "${ENV_FILE}" \
  -f docker-compose.staging.yml \
  exec -T db \
  pg_dump -U dmc -d dmc --no-owner --no-acl
```

The host runs `gzip`. `postgres:18-alpine` is not asked to provide `gzip`. The two-argument stream is written to `backups/dmc-YYYY-MM-DD.sql.gz.partial`, then `gzip -t` runs, then the file is renamed to `backups/dmc-YYYY-MM-DD.sql.gz`. A third argument, `predeploy`, writes `backups/predeploy/dmc-<YYYYMMDDTHHMMSSZ>.sql.gz` the same way, with a new name on every run. After a good predeploy dump, if today's daily file is absent, the script copies that gzip to the daily name and leaves the predeploy file in place. `pipefail` is on, so a failed `pg_dump` does not become a kept dump. The `.partial` file is deleted when `pg_dump` or `gzip -t` fails. `pg_dump` uses the container's local socket. The password is not an argument and is not printed. The script sets `PATH` so cron can find `docker` and `gzip`. `umask 077` is set before the lock, the directory, or the `.partial` file. `predeploy` takes `backups/backup.lock` with `flock -w 600` and exits 1 on timeout. The two-argument run uses `flock -n`; when the lock is held it appends `skip-lock` and exits 0. Every run appends `ok`, `fail`, or `skip-lock` to `backups/backup.log`, and that log is trimmed to the last 30 lines.

- `.github/workflows/deploy-staging.yml`. `Deploy application` copies the script and runs it with `predeploy` after Postgres is healthy and before `alembic upgrade head`. `Ensure Caddy HTTPS` is next. `Install Postgres backup` then refreshes the script and the crontab line and does not run the script. It reuses `secrets.VPS_DMC268_U`, `secrets.VPS_DMC268_P`, and `SSH_HOST`.

On the VPS, for the environment this run is deploying:

1. `Deploy application` does `install -d -m 700` for `backups/` and `backups/predeploy/`, then `scp`s the script to `${DEPLOY_PATH}/backup-postgres.sh`, marks it executable, and runs it with `predeploy`. That run, including the copy when today's daily file is absent, is the dump that exists before the next 03:15. Cron may still replace the daily file at 03:15.
2. `Install Postgres backup` installs the SSH user's crontab line. The marker is `# dmc-postgres-backup` plus the Compose project name. Lines for the other environment stay. If `crontab` is missing, the step fails and names that fact. It does not install packages. `crontab -l` is empty only when the failure says `no crontab`. Any other non-zero status exits 1 and does not write a new table. The cron command appends to `backups/cron.log`.

`docker-compose.staging.yml` is unchanged. The script calls the `db` service that is already defined there.

- `docs/staging-deployment.md`. Under `### PostgreSQL`, name both trees: `/opt/dmc-268-t2/backups/dmc-YYYY-MM-DD.sql.gz` (seven files) and `/opt/dmc-268-t2/backups/predeploy/` (every deploy from the last seven UTC dates). Directory mode `700`, on the VPS disk. Name this file as the restore procedure. Do not paste the restore commands into that section.

Unchanged:

- `docker-compose.staging.yml`
- Redis, and any Redis backup
- `infra/terraform/**`
- Step order: `Deploy application` (predeploy, then `alembic upgrade head`), then `Ensure Caddy HTTPS`, then `Install Postgres backup`
- `SENTRY__DSN` and the worker command

The workflow definition that Deploy runs is the one on API `main`. The cron line appears on the first Deploy after this change is on `main`.

Check after that deploy, on the VPS:

```bash
stat -c '%a %n' /opt/dmc-268-t2/backups
gzip -t /opt/dmc-268-t2/backups/dmc-*.sql.gz
crontab -l
```

The directory mode is `700`. `gzip -t` exits 0. `crontab -l` contains `/opt/dmc-268-t2/backup-postgres.sh` and `dmc-268-t2-staging`. A second run of the two-argument script on the same UTC date leaves a single file for that date. Two `predeploy` runs leave two files under `backups/predeploy/`. Daily names in `backups/` stay at seven. Predeploy files from the last seven UTC dates stay.

## GitHub

No new secret and no new variable. Deploy already writes the user and database as the literals `dmc` and `dmc`. Repository secret `STAGING_POSTGRES_PASSWORD` is only `POSTGRES__PASSWORD` in the env file. The backup script never reads a GitHub secret of its own. Do not add a `schedule` workflow.

This change is its own pull request into `main`. It is not part of the Sentry change.

After the merge, Deploy runs from the workflow file on `main`, and only after a successful push-triggered API CI on `main`. That run copies the script, installs the crontab line, and writes the first dump. Nothing else has to be clicked in GitHub if that Deploy succeeds.

If that run finished before this workflow file was on `main`, start it once more: Actions → Deploy → Run workflow. "Use workflow from" must be `main`, and the environment must be `staging`. A run started from any other branch skips the job and installs nothing. Leave `ui_sha` empty.

There is no GitHub button that loads a dump. Restore is the SSH procedure below.

## Restore

Use this when the staging database must be put back to a dump. Rows written after that dump are gone. Do this through the same SSH login Deploy uses. `db` has no host port, so `psql` on the host cannot reach it.

The dump is plain SQL (`pg_dump` without a custom format) with `--no-owner --no-acl`. Loading it into the database that is already there fails, because the dump creates tables that already exist. Replace the database.

`dmc` is the superuser created by `POSTGRES_USER`. The `postgres` database exists beside `dmc`. Drop and create `dmc` while connected to `postgres`.

### Bad migration

The dump to load is `backups/predeploy/dmc-<YYYYMMDDTHHMMSSZ>.sql.gz` from the deploy that ran that migration: the snapshot taken before `alembic`. Do not load a later predeploy file from a retry. Do not set `DUMP` to today's daily name. Cron may have replaced a daily file seeded by the pre-migration copy, and that daily file is then the migrated database. Do not run `./backup-postgres.sh` before that load. A two-argument run replaces today's daily file and is not how the pre-migration snapshot is chosen.

`gzip -t` the chosen predeploy file, then use the drop, create, and `psql` load below with that `DUMP`. Skip `alembic upgrade head` after the load when the damage is a migration you do not want reapplied. Deploy the older image first, then restore, and do not run upgrade until the image and the dump are the pair you want.

```bash
cd /opt/dmc-268-t2
DUMP=backups/predeploy/dmc-YYYYMMDDTHHMMSSZ.sql.gz
gzip -t "$DUMP"
```

### Other damage

When the damage is not that migration and the chosen file is today's daily name, copy it aside first (`dmc-YYYY-MM-DD.sql.gz.keep`). The two-argument script replaces the file for today's UTC date, so a run after that copy does not destroy the dump you chose.

If the current database is still readable, run the script once after that copy. The new file is a snapshot of the database you are about to replace. Load the `.keep` copy, or an older day's file. Do not load the file the script just wrote unless that snapshot is the state you want.

```bash
cd /opt/dmc-268-t2
DUMP=backups/dmc-YYYY-MM-DD.sql.gz.keep
cp -n backups/dmc-YYYY-MM-DD.sql.gz "$DUMP"
./backup-postgres.sh dmc-268-t2-staging .env.staging
gzip -t "$DUMP"
```

Skip the `cp` and the script when the chosen file is an older day, and set `DUMP` to that `.sql.gz` instead. The two-argument script does not replace older names. `gzip -t` has to exit 0 before the load below. Stop the processes that open Postgres. Leave `db` running:

```bash
docker compose \
  -p dmc-268-t2-staging \
  --env-file .env.staging \
  -f docker-compose.staging.yml \
  stop api worker webhook-worker
```

Replace `dmc` and load the dump in that same shell, so `DUMP` is still the path `gzip -t` accepted. `--single-transaction` rolls the load back on the first error, so a failed load leaves `dmc` empty instead of half-applied. Do not start the API until a load exits 0. Retry with the same file, or with the snapshot just taken.

```bash
compose() {
  docker compose \
    -p dmc-268-t2-staging \
    --env-file .env.staging \
    -f docker-compose.staging.yml \
    "$@"
}

compose exec -T db psql -U dmc -d postgres -v ON_ERROR_STOP=1 \
  -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = 'dmc' AND pid <> pg_backend_pid();"
compose exec -T db psql -U dmc -d postgres -v ON_ERROR_STOP=1 \
  -c "DROP DATABASE dmc;"
compose exec -T db psql -U dmc -d postgres -v ON_ERROR_STOP=1 \
  -c "CREATE DATABASE dmc OWNER dmc;"
set -o pipefail
gzip -dc "$DUMP" \
  | compose exec -T db psql -U dmc -d dmc -v ON_ERROR_STOP=1 --single-transaction
```

The dump's schema is the schema at backup time. The API image may expect later migrations. Before starting the API, run the same upgrade Deploy runs:

```bash
compose run --rm --no-deps --interactive=false -T api alembic upgrade head </dev/null
```

Skip that upgrade when the damage is a migration you do not want reapplied. In that case deploy the older image first, then restore, and do not run upgrade until the image and the dump are the pair you want.

Start the stopped services:

```bash
compose up -d api worker webhook-worker
```

`web` was not stopped. The API healthcheck is `http://127.0.0.1:8000/healthcheck` from inside the `api` container, which is the check Deploy already waits for.

Develop, if it is ever deployed, uses the same commands with `/opt/dmc-268-t2-develop`, project `dmc-268-t2-develop`, and env file `.env.staging.develop`. Do not load a staging dump into the develop project, or the reverse.

## Revert

Remove the workflow step and land that removal on API `main` before deleting the cron line. A branch-only deletion leaves the next Deploy installing the line again.

Then, on the VPS, through the same SSH login, remove the staging marker line and the command under it from the SSH user's crontab, and delete `/opt/dmc-268-t2/backup-postgres.sh`. Leave the files in `/opt/dmc-268-t2/backups` until someone deletes them on purpose. The script in git stays until that file is deleted too.

## Open questions

None.
