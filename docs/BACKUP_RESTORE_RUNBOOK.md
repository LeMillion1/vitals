# Backup and Restore Runbook

Last reviewed: 2026-08-26

## Scope and release boundary

This runbook covers operator disaster recovery for the health-store
installation. It is not the subject-scoped portability export exposed in the
web UI.

A complete health-store recovery point has one timestamp and exactly these
files:

- `vitals_<timestamp>.sql.gz`;
- `garmin_session_<timestamp>.tar.gz`;
- `private_files_<timestamp>.tar.gz`;
- `legacy_uploads_<timestamp>.tar.gz`;
- `vitals_bundle_<timestamp>.sha256`, published last and containing the four
  artifact checksums.

The encrypted offsite snapshot also contains the protected installation `.env`.
Never treat a loose artifact without its matching manifest as a recovery point.

The optional ZITADEL identity store is deliberately separate. The current
health-store bundle does **not** include `vitals_idp_pgdata`. Do not enable the
Compose `idp` profile in production until a separate ZITADEL PostgreSQL backup,
master-key escrow, scratch restore, and OIDC discovery/login drill have passed.

## Recovery objectives

Before calling the installation production-ready, record concrete values for:

- RPO: at most 24 hours with the default local backup interval;
- offsite replication delay: at most 15 minutes after a complete local bundle;
- RTO: measured by a full scratch restore, not estimated;
- retention: local seven days by default, with offsite retention managed only
  by a separate administrative credential.

The VPS-local copy protects against an application or database failure. It does
not protect against loss of the VPS or its account. Offsite replication is the
disaster-recovery boundary.

## Prepare the S3 restic repository

Use a versioned S3-compatible bucket dedicated to this installation. Initialize
the repository from a trusted administration machine, not from the production
sidecar. Keep the repository password and an administrative recovery credential
outside the VPS in two independently recoverable locations.

The production S3 credential needs list, read, and write access. Restic also
needs to delete its own lock objects. Permit deletion only below the repository
`locks/` prefix; deny deletion of `data/`, `index/`, `snapshots/`, `keys/`, and
`config`. Keep prune/retention rights on a different administrative credential.
Bucket versioning or provider-side immutability is an additional boundary, not
a replacement for tested restic recovery.

Create four owner-only files below `.secrets/` on the production host:

- `restic_repository` — for example `s3:https://endpoint/bucket/vitals-prod`;
- `restic_password` — a unique high-entropy restic repository password;
- `restic_s3_access_key` — exactly one non-empty line;
- `restic_s3_secret_key` — exactly one non-empty line.

The file paths may be overridden with the four `VITALS_RESTIC_*_FILE` variables
shown in `.env.example`. Those variables contain paths only, never secret
values. The `.secrets/` directory is excluded from Git and the Docker build
context.

Initialize and verify the empty repository with restic `0.19.1` from the trusted
machine. Production uses the official image pinned by an OCI digest in
`docker-compose.yml`; do not silently replace it with `latest`.

## Enable replication without reconciling the app stack

Confirm that the ordinary local backup sidecar is healthy and that the newest
manifest passes `sha256sum -c`. Then start only the offsite sidecar:

```bash
docker compose --profile offsite up -d --no-deps vitals_offsite_backup
```

Do not use a bare `docker compose --profile offsite up -d`: Compose would also
reconcile every unprofiled application service. To stop replication without
touching the main stack:

```bash
docker compose stop vitals_offsite_backup
```

The sidecar validates the exact manifest and every local checksum, opens the
already initialized repository, and runs `restic backup --skip-if-unchanged`.
It never initializes, forgets, prunes, or restores a repository. It exits on a
failed cycle so Compose exposes the failure through state/restart count instead
of leaving a silently broken process marked as running.

After the first run, verify all of the following without printing secret files:

```bash
docker compose --profile offsite ps vitals_offsite_backup
docker compose --profile offsite logs --tail 50 vitals_offsite_backup
```

- the container is running with a stable restart count;
- the log reports verified encrypted replication for the newest timestamp;
- a trusted admin machine sees the `vitals-bundle:<timestamp>` tag;
- a second unchanged run creates no duplicate snapshot.

## Routine verification

Use the administrative credential from a trusted machine, never the production
credential, for destructive retention or repository-wide checks.

- Daily: alert if either backup container is stopped/restarting or the newest
  offsite timestamp exceeds the RPO plus replication delay.
- Monthly: run `restic check`.
- Quarterly: run `restic check --read-data` and the full scratch restore below.
- After every PostgreSQL, restic, Compose, storage-provider, schema, or private-
  file-layout change: repeat the full restore drill before deployment approval.

Record the snapshot ID, source manifest, start/end time, restored Alembic
revision, aggregate ownership validation result, RLS result, and browser smoke
result. Do not put row values, filenames from medical uploads, tokens, or
credentials into the drill record.

## Scratch restore drill

### 1. Isolate the restore

Use a disposable host or VM with PostgreSQL 15 and a clean Vitals checkout.
Create a new temporary directory with `mktemp -d`. Never restore over production
and never mount a production database or file volume into the drill.

From the trusted administration machine:

```bash
restic snapshots --tag vitals
restic check
restic restore <snapshot-id> --target <temporary-restore-root>
```

The restored tree contains `backups/` and `source/vitals.env`. Do not `source`
the restored environment file. Review it offline, provision fresh drill-only
credentials, and rotate production credentials after any suspected repository
or backup-host compromise.

### 2. Validate the bundle before extraction

Select one restored `vitals_bundle_*.sha256`. It must contain exactly four lines
and the expected names for its timestamp. Then run from the restored `backups/`
directory:

```bash
sha256sum -c vitals_bundle_<timestamp>.sha256
gzip -t vitals_<timestamp>.sql.gz
```

List each tar before extraction. Reject an absolute path, `..` path component,
device node, or other unexpected entry. Only then extract into three new empty
destinations. The legacy upload archive must be restored to
`web/static/uploads`; the private and Garmin archives belong in their respective
volumes.

### 3. Restore PostgreSQL 15

Create an empty disposable PostgreSQL 15 database owned by a drill-only schema
owner. Restore with fail-fast SQL handling:

```bash
gzip -dc vitals_<timestamp>.sql.gz \
  | psql -v ON_ERROR_STOP=1 <drill-migration-database-url>
```

The target must be empty. Never merge a full installation dump into a populated
database. Confirm that `alembic_version` contains the expected application
revision and that Alembic can upgrade the restored database to the checked-out
head without a downgrade.

Point a drill checkout at the restored database and run the aggregate-only
validators. Run ownership status first, record evidence for the restored graph,
then do the same for scoped keys:

```bash
.venv/bin/python scripts/validate_subject_ownership.py
.venv/bin/python scripts/validate_subject_ownership.py --apply
.venv/bin/python scripts/audit_scoped_keys.py
.venv/bin/python scripts/audit_scoped_keys.py --apply
```

An otherwise healthy status can be `not_started` after ordinary new writes,
because the persisted cutover checksum describes an older graph. The two
`--apply` commands above update aggregate checkpoint evidence only in the
disposable drill database; either refusal fails the drill. Do not run them on
production merely to make a drill green. If the recovered revision legitimately
predates the cutover, follow the
[ownership cutover runbook](OWNERSHIP_CUTOVER_RUNBOOK.md); do not improvise an
ownership update. Re-provision
the distinct runtime role with `scripts/provision_runtime_db_role.py` and verify
that it is not superuser, has no `BYPASSRLS`, owns no relation, sees no patient
rows without a bound subject context, and sees only the bound subject inside a
request transaction.

### 4. Start and inspect the recovered app

Start the restored stack on a different loopback port and a distinct Compose
project name. Keep all real outbound integrations disabled. Verify:

- `/health` reports database, Redis, and scheduler healthy;
- login succeeds with drill credentials;
- Today, reports, settings, care hub, messages, and one representative page for
  every enabled health module render in desktop and phone widths;
- legacy uploads and private-volume attachments can be read only through their
  authenticated routes;
- the database runtime role still satisfies the least-privilege/RLS checks;
- no scheduler job sends a message or calls a real Garmin, Hevy, OpenRouter, or
  other production endpoint.

Destroy the disposable database, extracted files, and drill secrets after the
result has been recorded. Keep only the aggregate drill record.

## Production restore decision

A production restore requires a maintenance window, a fresh verified recovery
point, an explicit chosen timestamp, and a successful scratch drill of that
same bundle. Stop writes before taking the final pre-restore copy. Restore into
new database and file volumes first, validate them, and switch the application
only after the new stack passes health, RLS, and browser checks.

Do not overwrite the old database or volumes during the first attempt. Retain
them read-only until the restored stack has passed the agreed observation
window. Rollback is switching the application back to those untouched volumes;
once new multi-subject writes have been accepted, schema downgrade is not a
rollback path.

## Identity-provider gate

Before enabling ZITADEL, add and test a separate identity-store backup with its
own PostgreSQL dump/manifest/offsite tag. Escrow `VITALS_IDP_MASTERKEY` outside
the VPS. A valid drill must restore ZITADEL into an empty PostgreSQL 15 instance,
pass its health and OIDC discovery endpoints, and complete a synthetic login
against a non-production Vitals stack. Health-store success alone never proves
identity-store recoverability.
