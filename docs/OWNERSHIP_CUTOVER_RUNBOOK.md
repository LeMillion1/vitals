# Ownership Cutover Runbook

Last reviewed: 2026-08-26

## Not this, if the database is empty

This whole document is about a lake that already holds somebody's history. A
**new** installation needs none of it: `alembic upgrade head` reaches head on its
own. Compose runs that in the one-shot `vitals_migrate` service, provisions a
separate restricted role in `vitals_db_roles`, and only then starts FastAPI.

That was not true until 2026-08-24 — revision `0005` seeded five rows nobody
could ever own, and revision `0049` refused over them, so a fresh deployment
could not start at all. `tests/test_fresh_installation_migrations.py` now walks
that path, because every rehearsal here starts from a synthetic revision-0034
lake and none of them was ever it.

## Upgrading an existing lake

The upgrade that gives every row an owner is not a single `alembic upgrade head`.
It is three parts in a fixed order, and the middle one is an application job, not
a migration. Running them out of order does not corrupt anything — the contract
revision refuses rather than proceeding — but it does stop the upgrade halfway,
which is worth avoiding on a database people are using.

Past revision `0049`, once a second subject has written
data, downgrade to the single-subject schema is forbidden and recovery is a
verified backup plus a forward fix. Take the backup first.

`tests/test_ownership_deploy_rehearsal.py` performs this whole sequence against
a synthetic revision-0034 lake on every integration run, so the order below is
executed and not merely written down.

## 1. Migrate as far as an unstamped lake can go

```bash
docker compose run --rm --no-deps vitals_migrate alembic upgrade 0048
```

Every ownership column exists and is still nullable. This is a safe place to
stop, but do not start the current application image on this intermediate
schema: current runtime code may query tables added after 0048. Do not run
`docker compose up vitals_app` yet: its migration dependency advances to head.

## 2. Bootstrap the roots, then run the phases in order

Materialize the legacy owner, the resource roots, and the checked-in HRT and
conflict catalogs with the bounded bootstrap command. The phases classify rows
against those, so they must exist first.

From the host checkout:

```bash
docker compose up -d vitals_db vitals_redis
docker compose run --rm --no-deps \
  --volume "$PWD/.env:/app/.env:ro" vitals_migrate \
  python scripts/bootstrap_ownership_roots.py
```

The command performs database work only, commits once, prints
`{"status":"completed","revision":"0048"}`, and exits. It does not run the
scheduler or call Garmin, Hevy, OpenRouter, Telegram, or another external
service. Do not replace it with the normal container command: that command
upgrades to head before Uvicorn starts.

Each command is resumable and reports one line of JSON. Run every listed Python
command through the same privileged one-shot wrapper:

```bash
docker compose run --rm --no-deps \
  --volume "$PWD/.env:/app/.env:ro" vitals_migrate \
  python scripts/<phase-script>.py --apply --batch-size 1000 --max-batches 100
```

Replace the script and arguments with each line below. A phase is done when its
`status` is `completed`; re-running a completed phase is a no-op. Run them in
this order — a child cannot inherit an owner its parent does not have yet, and a
normalized fact takes its provenance from a raw payload that must already be
stamped:

  1. `python scripts/backfill_subject_ownership.py --apply --batch-size 1000 --max-batches 100`
    checkpoint phase `stage3.raw_payloads.v1`
  2. `python scripts/backfill_normalized_subject_ownership.py --apply --batch-size 1000 --max-batches 100`
    checkpoint phase `stage3.normalized_manual.v1`
  3. `python scripts/backfill_hrt_child_subject_ownership.py --apply --batch-size 1000 --max-batches 100`
    checkpoint phase `stage3.inherited_children.hrt.v1`
  4. `python scripts/backfill_provider_raw_subject_ownership.py --apply --batch-size 1000 --max-batches 100`
    checkpoint phase `stage3.provider_raw_linked.v1`
  5. `python scripts/backfill_hevy_child_subject_ownership.py --apply --batch-size 1000 --max-batches 100`
    checkpoint phase `stage3.inherited_children.hevy.v1`
  6. `python scripts/backfill_hrt_compound_subject_ownership.py --apply --batch-size 1000 --max-batches 100`
    checkpoint phase `stage3.mixed_catalog.hrt.v1`
  7. `python scripts/backfill_conflict_rule_subject_ownership.py --apply --batch-size 1000 --max-batches 100`
    checkpoint phase `stage3.mixed_catalog.conflict_rules.v1`
  8. `python scripts/backfill_progress_photo_subject_ownership.py --apply --batch-size 1000 --max-batches 100`
    checkpoint phase `stage3.file_backed.progress_photos.v1`
 9. `python scripts/backfill_shared_report_subject_ownership.py --apply --batch-size 1000 --max-batches 100`
    checkpoint phase `stage3.retained_artifact.shared_reports.v1`
10. `python scripts/backfill_weight_log_subject_ownership.py --apply --batch-size 1000 --max-batches 100`
    checkpoint phase `stage3.channel_optional.weight_logs.v1`
11. `python scripts/backfill_lab_result_subject_ownership.py --apply --batch-size 1000 --max-batches 100`
    checkpoint phase `stage3.raw_linked_facts.lab_results.v1`
12. `python scripts/backfill_genetic_variant_subject_ownership.py --apply --batch-size 1000 --max-batches 100`
    checkpoint phase `stage3.raw_linked_facts.genetic_variants.v1`
13. `python scripts/backfill_body_scan_subject_ownership.py --apply --batch-size 1000 --max-batches 100`
    checkpoint phase `stage3.file_backed.body_scans.v1`
14. `python scripts/backfill_body_scan_metric_subject_ownership.py --apply --batch-size 1000 --max-batches 100`
    checkpoint phase `stage3.inherited_children.body_scan_metrics.v1`
15. `python scripts/backfill_garmin_weight_export_subject_ownership.py --apply --batch-size 1000 --max-batches 100`
    checkpoint phase `stage3.provider_outbox.garmin_weight_exports.v1`
16. `python scripts/backfill_weekly_digest_subject_ownership.py --apply --batch-size 1000 --max-batches 100`
    checkpoint phase `stage3.retained_artifact.weekly_digests.v1`
17. `python scripts/backfill_retired_signal_ownership.py --apply --batch-size 1000 --max-batches 100`
    compatibility phase `stage3.retired_signals.v1`; these legacy Telegram
    tables are removed by revision `0058`, but revision `0049` still requires
    their existing rows to cross the ownership contract first
18. `python scripts/backfill_notification_subject_ownership.py --apply --batch-size 1000 --max-batches 100`
    checkpoint phase `stage3.delivery_artifact.notifications.v1`
19. `python scripts/backfill_system_alert_subject_ownership.py --apply --batch-size 1000 --max-batches 100`
    checkpoint phase `stage3.subject_optional.system_alerts.v1`

Between any two, the upgrade can pause indefinitely.

## 3. Migrate the rest of the way

Create distinct web/worker DSNs first. Preserve the exact existing production
project and its host-only overlay; an accidental Compose project name creates
different volumes and is not a deployment:

```bash
# First split adoption only: do not invoke the old deploy.sh. It parses its old
# hard-reset/combined body before updating itself. Bootstrap the reviewed new
# script through an explicit fast-forward, then invoke it separately.
branch="$(git rev-parse --abbrev-ref HEAD)"
test "$branch" != HEAD
git diff --quiet && git diff --cached --quiet
git fetch --prune origin
git merge-base --is-ancestor HEAD "origin/$branch"
git merge --ff-only "origin/$branch"
```

The new checkout contains the directory-aware one-shot helper. Before invoking
the new deploy script, migrate the current Settings-owned legacy runtime file
into the new private directory and verify both modes. This copies rather than
moves the old file because the first-cutover emergency path needs its absolute
bind unchanged:

```bash
python3 scripts/create_runtime_env.py --migrate-from .env.runtime
test "$(stat -c '%a' .vitals-runtime)" = 700
test "$(stat -c '%a' .vitals-runtime/vitals.env)" = 600
```

For a fresh installation with no legacy runtime file, use
`python3 scripts/create_runtime_env.py` instead. Both modes are one-shot and
refuse to replace `.vitals-runtime/vitals.env`.

The new base Compose file owns the runtime-directory mount. Remove only a legacy
`/app/.env` bind from the host-only production overlay if that overlay added one;
do not remove its proxy, port, upload, or other installation-specific settings:

```bash
if grep -q -- '/app/.env' docker-compose.production.yml; then
  "${EDITOR:?set EDITOR}" docker-compose.production.yml
fi
! grep -q -- '/app/.env' docker-compose.production.yml
```

The new `deploy.sh` renders the combined configuration and refuses before any
migration unless web and worker resolve to the same owner-only runtime
directory, web is read/write, worker is read-only, both use
`/run/vitals-runtime/vitals.env`, and neither receives an `/app/.env` bind. It
also requires host modes `0700` and `0600`; do not loosen them to make Docker
startup pass.

Before that first invocation, prepare a separate detached worktree, a reviewed
copy of the old production overlay, and a deliberately named local pre-split
image tag from the currently running app image ID:

```bash
test "$(pwd -P)" = /root/vitals-commercial-production
git worktree add --detach /root/vitals-pre-split dba1053
install -m 600 docker-compose.production.yml \
  /root/vitals-pre-split/docker-compose.production.yml
pre_split_container="$(docker ps -aq \
  --filter label=com.docker.compose.project=vitals_prod \
  --filter label=com.docker.compose.service=vitals_app)"
test -n "$pre_split_container"
test "$(printf '%s\n' "$pre_split_container" | awk 'NF { count++ } END { print count + 0 }')" -eq 1
pre_split_image="$(docker inspect --format '{{.Image}}' "$pre_split_container")"
docker image tag "$pre_split_image" vitals_prod_pre_split:dba1053
docker image inspect vitals_prod_pre_split:dba1053 >/dev/null
```

Keep that emergency bundle outside the active checkout and treat it as
read-only. The new deploy's fast-forward neither preserves nor manufactures it,
and its normal rollback state must not claim it. On later split releases, skip
the manual bootstrap block and invoke the already new `deploy.sh` directly. Do
not copy `.env`, `.env.runtime`, or `.vitals-runtime/` into the worktree: the
only authoritative copies remain under `/root/vitals-commercial-production`.

Only after that emergency anchor exists, invoke the newly fast-forwarded script
as a separate shell command:

```bash
cd /root/vitals-commercial-production
export COMPOSE_PROJECT_NAME=vitals_prod
export COMPOSE_FILE=docker-compose.yml:docker-compose.production.yml
./deploy.sh
```

The deploy preflight proves that exactly one existing `vitals_db` and
`vitals_app` belong to that project. It fast-forwards a clean checkout, builds
one immutable full-SHA image, upgrades to head as the migration role, converges
the two restricted role contracts, and starts the scheduler-only worker before
switching web. Both service healthchecks and a loopback `/health` plus `/login`
smoke must pass before the successful SHA is recorded. Verify that both runtime
roles are neither superusers nor `BYPASSRLS` and own zero relations.

Revision `0049` counts the remaining nulls in every
target column before it alters anything, and refuses with the table, the column
and the count if a phase was skipped or is unfinished. Revision `0050` then
enables the row policies.

After this the application must supply `vitals.subject_id` on every transaction
that reads patient data; `vitals/persistence/rls.py` does it, and an unbound
session sees nothing rather than everything. Migration and backfill roles that
must see every row need `BYPASSRLS` or superuser. The long-running runtime roles
have neither. Revision `0083` instead grants only the worker membership in a
NOLOGIN capability role, and the worker still has to set the transaction-local
platform GUC. Web receives no such membership. A backfill that could not see an
unstamped row could not stamp it.

## Split-runtime rollback boundary

An ordinary rollback changes runtime images only. It accepts a full Git SHA only
after importing `vitals.worker_health` from that exact local image, switches the
worker before web, repeats both health gates, and deliberately does not run
Alembic or role provisioning:

```bash
export COMPOSE_PROJECT_NAME=vitals_prod
export COMPOSE_FILE=docker-compose.yml:docker-compose.production.yml
./deploy.sh rollback
# Or: ./deploy.sh rollback <full-split-compatible-sha>
```

The first successful split deployment is preserved as the future compatibility
anchor, but there is no older ordinary rollback target during that first
cutover. In particular, the currently deployed pre-split revision `dba1053`
does not contain `vitals.worker_health`; the script must and does reject it.
Never point the new Compose topology at that image after schema revision `0083`.

If the first cutover cannot be fixed forward and returning to the old combined
runtime is unavoidable, declare a maintenance window and use a separately
reviewed emergency procedure. Stop writes and both split runtime services, keep
the database/Redis volumes untouched, and use the failed split SHA's migration
image to perform exactly one downgrade:

```bash
cd /root/vitals-commercial-production
export COMPOSE_PROJECT_NAME=vitals_prod
export COMPOSE_FILE=docker-compose.yml:docker-compose.production.yml
export VITALS_IMAGE_TAG=<failed-split-full-sha>
docker compose stop vitals_app vitals_worker
docker compose run --rm --no-deps vitals_migrate alembic downgrade 0082
```

Then use the separately prepared detached `dba1053` worktree and reviewed old
production overlay. Create the named owner-only emergency override before
entering its YAML, and verify its mode:

```bash
cd /root/vitals-pre-split
umask 077
"${EDITOR:?set EDITOR}" docker-compose.emergency-pre-split.yml
chmod 600 docker-compose.emergency-pre-split.yml
test "$(stat -c '%a' docker-compose.emergency-pre-split.yml)" = 600
```

The file must contain only this reviewed image and runtime bind replacement:

```yaml
services:
  vitals_app:
    image: vitals_prod_pre_split:dba1053
    volumes:
      - type: bind
        source: /root/vitals-commercial-production/.env.runtime
        target: /app/.env
        bind:
          create_host_path: false
```

Explicitly keep `COMPOSE_PROJECT_NAME=vitals_prod`, render the combined
configuration without printing it, and prove that the image and `/app/.env`
source resolve exactly before starting only `vitals_app`:

```bash
cd /root/vitals-pre-split
export COMPOSE_PROJECT_NAME=vitals_prod
export COMPOSE_FILE=docker-compose.yml:docker-compose.production.yml:docker-compose.emergency-pre-split.yml
docker compose --env-file /root/vitals-commercial-production/.env config --quiet
docker compose --env-file /root/vitals-commercial-production/.env \
  config --format json | python3 -c '
import json
import sys

app = json.load(sys.stdin)["services"]["vitals_app"]
mounts = [item for item in app["volumes"] if item["target"] == "/app/.env"]
expected_source = "/root/vitals-commercial-production/.env.runtime"
assert app["image"] == "vitals_prod_pre_split:dba1053"
assert len(mounts) == 1 and mounts[0]["source"] == expected_source
'
docker compose --env-file /root/vitals-commercial-production/.env \
  up -d --no-deps --no-build vitals_app
```

Keep the split `vitals_worker` stopped: the old FastAPI process owns its embedded
scheduler. Prove combined `/health` plus login locally. This weakens the
capability boundary and is an emergency recovery, not `deploy.sh rollback`; the
script must never automate the schema downgrade or guess an old image, checkout,
project, or overlay.

## Rotate an owner credential previously exposed to the web container

Deployments created before the application-only runtime boundary mounted the
host `.env` into `vitals_app`. After creating `.vitals-runtime/vitals.env`,
rendering the production overlay to prove that `/run/vitals-runtime` resolves to
that host directory, and recreating the app, rotate the old migration-owner
password. Run the bounded helper through the already authenticated one-shot
migration image; it updates PostgreSQL and the two exact operator-file fields
without printing the replacement:

```bash
docker compose run --rm --no-deps \
  --volume "$PWD:/operator" vitals_migrate \
  python scripts/rotate_migration_db_password.py --env-file /operator/.env
```

The directory bind is deliberate: the helper stages and fsyncs a sibling file,
then renames it over `.env`. A bind of the file alone is a mount point and Linux
correctly refuses that atomic replacement. The short-lived migration container
already has owner authority; it must exit immediately after this bounded
operation and must never be reused as the web image.

Then recreate the backup sidecar so its container environment receives the new
password, and prove the migration/role chain using the new credential:

```bash
docker compose up -d --no-deps --force-recreate vitals_backup
docker compose run --rm vitals_db_roles
```

Do not rotate before the web mount is isolated: otherwise the freshly generated
owner credential is immediately exposed to the same process again.

## If step 3 refuses

The message names each table that is behind and by how many rows. Find the phase
that owns that table in the list above, run it to `completed`, and retry. The
refusal happens before any schema change, so nothing is half-applied.
