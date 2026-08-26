# Ownership Cutover Runbook

Last reviewed: 2026-08-24

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

```bash
docker compose up -d --build vitals_app
```

The dependency chain upgrades to head as the migration role, reapplies DML and
default grants to the distinct restricted runtime role, and starts FastAPI only
after both one-shot services exit successfully. Verify the running web role is
neither a superuser nor `BYPASSRLS`, and owns zero relations.

Revision `0049` counts the remaining nulls in every
target column before it alters anything, and refuses with the table, the column
and the count if a phase was skipped or is unfinished. Revision `0050` then
enables the row policies.

After this the application must supply `vitals.subject_id` on every transaction
that reads patient data; `vitals/persistence/rls.py` does it, and an unbound
session sees nothing rather than everything. Roles that must see across subjects
— the migration runner, these backfill jobs, the platform control plane — need
`BYPASSRLS` or superuser. A backfill that could not see an unstamped row could
not stamp it.

## Rotate an owner credential previously exposed to the web container

Deployments created before the application-only `.env.runtime` boundary mounted
the host `.env` into `vitals_app`. After creating `.env.runtime`, rendering the
production overlay to prove that `/app/.env` uses it, and recreating the app,
rotate the old migration-owner password. Run the bounded helper through the
already authenticated one-shot migration image; it updates PostgreSQL and the
two exact operator-file fields without printing the replacement:

```bash
docker compose run --rm --no-deps \
  --volume "$PWD/.env:/operator/.env" vitals_migrate \
  python scripts/rotate_migration_db_password.py --env-file /operator/.env
```

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
