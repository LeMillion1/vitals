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

The encrypted offsite snapshot also contains the protected host/operator `.env`
and the runtime-only `.env.runtime`. The first holds the database-owner and
worker DSNs and must never be mounted into either runtime process; the second is
the allowlisted file Settings updates. Web mounts that file read/write and the
worker read-only. Never treat a loose artifact without its matching manifest as
a recovery point.

The optional ZITADEL identity store is deliberately separate. A complete
identity-store recovery point lives below `backups/idp/` and has exactly:

- `zitadel_<timestamp>.sql.gz`;
- `zitadel_bundle_<timestamp>.sha256`, published last and containing the dump
  checksum.

The `idp` profile starts this backup sidecar only after the provider reports
ready and the script refuses a database with no user tables. The health-store
bundle does **not** include `vitals_idp_pgdata`, and a healthy identity backup
does not make a health bundle complete. Starting a supported provider while
Vitals still uses password login is preparation; enabling OIDC is the cutover.

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

Use versioned S3-compatible storage dedicated to this installation. Initialize
the health and identity repositories separately from a trusted administration
machine, not from either production sidecar. Separate repository passwords and
S3 credentials make their encryption and failure boundaries real rather than
mere restic tags. Keep both administrative recovery credentials outside the VPS
in two independently recoverable locations.

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

Create another four, with different credentials and repository password, for
the identity stream:

- `idp_restic_repository`;
- `idp_restic_password`;
- `idp_restic_s3_access_key`;
- `idp_restic_s3_secret_key`.

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

The identity stream uses a different process, repository, state volume, and
schedule. Once a verified local identity manifest exists, start and inspect it
without reconciling any other service:

```bash
docker compose --env-file .env --env-file .env.idp \
  --profile idp-offsite up -d --no-deps vitals_idp_offsite_backup
docker compose --env-file .env --env-file .env.idp \
  --profile idp-offsite logs --tail 50 vitals_idp_offsite_backup
```

The trusted admin machine must see `vitals-idp-bundle:<timestamp>` in the
identity repository. That snapshot contains only the dump and its manifest:
neither the Vitals `.env` nor the IDP master key is included.

## Routine verification

Use the administrative credential from a trusted machine, never the production
credential, for destructive retention or repository-wide checks.

- Daily: alert if either local backup or offsite container is
  stopped/restarting. Independently parse the UTC timestamp inside each latest
  manifest and offsite marker and alert when either stream exceeds its RPO plus
  replication delay. Container uptime and marker mtime are not freshness: an
  unchanged restic cycle rewrites the marker with the same manifest name.
- Monthly: run `restic check`.
- Quarterly: run `restic check --read-data` and the full scratch restore below.
- After every PostgreSQL, restic, Compose, storage-provider, schema, or private-
  file-layout change: repeat the full restore drill before deployment approval.

Record the snapshot ID, source manifest, start/end time, restored Alembic
revision, aggregate ownership validation result, RLS result, and browser smoke
result. Do not put row values, filenames from medical uploads, tokens, or
credentials into the drill record.

## Release deployment gate

Before a production update, verify a complete recovery point and preserve the
exact existing project plus its owner-only overlay. `deploy.sh` refuses a new or
mistyped project by requiring exactly one existing database and web container
with matching Compose labels:

```bash
export COMPOSE_PROJECT_NAME=vitals_prod
export COMPOSE_FILE=docker-compose.yml:docker-compose.production.yml
./deploy.sh
```

The script builds one image tagged with the full Git SHA, migrates and converges
roles, waits for worker readiness, switches web, then requires local `/health`
and `/login` smoke. `.vitals-deploy-state` is a mode-`0600` host-local record of
validated image SHAs, not a health-data backup or a disaster-recovery point. If
it or an image is absent, rollback fails closed instead of guessing.

`./deploy.sh rollback` is only a runtime-image switch to a recorded compatible
split image; it neither restores data nor downgrades Alembic. The first split
cutover has no ordinary pre-split rollback anchor. The explicit `0083` to `0082`
combined-runtime emergency path is documented in
[OWNERSHIP_CUTOVER_RUNBOOK.md](OWNERSHIP_CUTOVER_RUNBOOK.md), and must never be
substituted for a production restore decision.

## Scratch restore drill

Restore an offsite snapshot on a trusted administration machine when that is
the recovery point under test:

```bash
restic snapshots --tag vitals
restic check
restic restore <snapshot-id> --target <temporary-restore-root>
```

The restored tree contains `backups/`, `source/vitals.env`, and
`source/vitals.runtime.env`. Do not source or pass either restored environment
file to Compose. Select one exact absolute manifest path, then run the bounded
orchestrator from the reviewed Vitals checkout:

```bash
python3 scripts/rehearse_installation_restore.py run \
  --manifest /absolute/recovery/backups/vitals_bundle_<timestamp>.sha256 \
  --scratch-parent /var/tmp/vitals-restore-drills
```

The command accepts no production environment, database URL, Compose project,
volume, or credential. It stages the exact Git `HEAD`, verifies and safely
extracts the exact five-file bundle, renders and audits a scratch-only Compose
configuration, and restores PostgreSQL with `ON_ERROR_STOP` and one transaction.
It then migrates to the single Alembic head, records the ownership and scoped-key
checks in the disposable database, provisions distinct web and worker roles,
proves forced subject RLS through the web role, restarts PostgreSQL, and repeats
the read-only proof. Web, worker, and data services have only internal networks.
The worker must pass its DB/Redis/generation/lease/heartbeat readiness helper and
has no published port. A credential-free byte proxy on a separate app-only
network publishes the loopback browser port without giving web an outbound
route; backup, offsite, and identity services are not part of the active set.

An ordinary successful run removes its exact Compose project, volumes, copied
bundle, synthetic secrets, and run directory, and returns one aggregate JSON
record containing the measured recovery time. A failed run also cleans by
default. If cleanup itself fails, the error record includes the exact safe
project and run-directory identifiers that require operator attention; never
use a Docker-wide prune or a wildcard deletion.

To retain the verified scratch app for browser inspection, use a different
loopback port:

```bash
python3 scripts/rehearse_installation_restore.py run \
  --manifest /absolute/recovery/backups/vitals_bundle_<timestamp>.sha256 \
  --scratch-parent /var/tmp/vitals-restore-drills \
  --serve --port 18080
```

The result names a `0600` credential file under the `0700` run directory; do not
copy its contents into logs or the drill record. Open `http://127.0.0.1:18080`,
then verify login, Today, reports, settings, care hub, messages, and one page for
every enabled module at desktop and phone widths. Check protected legacy uploads
and private attachments only through authenticated routes. Do not expose this
port beyond loopback.

After the first browser pass, restart both runtime services and repeat worker,
web-health, and RLS gates:

```bash
python3 scripts/rehearse_installation_restore.py status --run-dir <exact-run-dir>
python3 scripts/rehearse_installation_restore.py restart --run-dir <exact-run-dir>
```

Destroy it immediately after the browser evidence is recorded:

```bash
python3 scripts/rehearse_installation_restore.py destroy --run-dir <exact-run-dir>
```

This drill proves recovery of the health store and the current strict
subject-data RLS contract. It does not prove that root/control tables form a
hostile-process tenant boundary, does not recover the independently gated
identity provider, and does not authorize a production switch. Those are
separate release gates.

## Production restore decision

A production restore requires a maintenance window, a fresh verified recovery
point, an explicit chosen timestamp, and a successful scratch drill of that
same bundle. Stop writes before taking the final pre-restore copy. Restore into
new database and file volumes first, validate them, and switch worker before web
only after the new stack passes worker readiness, web health, RLS, and browser
checks.

Do not overwrite the old database or volumes during the first attempt. Retain
them read-only until the restored stack has passed the agreed observation
window. Rollback is switching the application back to those untouched volumes;
once new multi-subject writes have been accepted, schema downgrade is not a
rollback path.

## Identity-provider gate

Compose contains no runnable default identity-provider image. The old
`ghcr.io/zitadel/zitadel:v2.66.0` reference was removed because it was obsolete,
not digest-pinned, and fell within published vulnerable ranges. The inactive
profile now contains a nonexistent all-zero sentinel and refuses preflight
unconditionally even when its secrets are valid. Provider approval must be a
reviewed code change, not an env override. Do not enable the production `idp`
profile until the supported release and its licence have been chosen, the tag's
manifest has been independently matched to the committed digest, and its
Compose/login/backup configuration plus OIDC/browser conformance have been
reviewed.

ZITADEL v4 requires a separate Login V2 image and login-client PAT/bootstrap
state outside PostgreSQL. A future v4 approval must add that state to the
identity recovery bundle or document and prove a non-circular recreation path;
a database-only restore is not sufficient. The same change must separate
one-shot database setup privileges from the long-running provider role and
distinguish the public HTTPS port from the loopback origin port.

Before the approved provider's first start, copy `.env.idp.example` to an
owner-only `.env.idp` and escrow `VITALS_IDP_MASTERKEY` outside the VPS. Never
put IDP control-plane secrets in `.env.runtime` or another runtime-visible file.
Neither runtime mounts the host/operator `.env`. The provider image/digest
belongs in reviewed Compose code, not a secret file. The database password is
not the encryption key; losing the master key can leave a successfully restored
identity database unusable.

For the current production checkout, preserve the exact project and overlay on
every command:

```bash
test -f docker-compose.production.yml
export COMPOSE_PROJECT_NAME=vitals_prod
export COMPOSE_FILE=docker-compose.yml:docker-compose.production.yml
docker compose --env-file .env --env-file .env.idp config >/dev/null
docker compose --env-file .env --env-file .env.idp \
  --profile idp up -d --wait vitals_idp_backup
```

`docker-compose.production.yml` is an owner-only, untracked host overlay on this
installation. Before the first IDP start, its rendered config must map both
`vitals_idp_backup:/backups` and `vitals_idp_offsite_backup:/backups/idp` to the
protected `/root/vitals/backups` tree; the base checkout-relative mounts are not
the production recovery directory.

The named backup target pulls in only its IDP dependencies. Keep the four Vitals
OIDC variables unset, so password login remains authoritative during this
preparation stage. Configure a synthetic/operator identity and application,
then wait for its first **non-empty** recovery point and select that exact
manifest. To force another point, stop the persistent writer first, run the
one-shot, and restart it; never run two writers against the directory:

```bash
docker compose --env-file .env --env-file .env.idp stop vitals_idp_backup
docker compose --env-file .env --env-file .env.idp --profile idp run --rm \
  --no-deps \
  -e VITALS_IDP_BACKUP_RUN_ONCE=true vitals_idp_backup
docker compose --env-file .env --env-file .env.idp --profile idp up -d \
  --no-deps vitals_idp_backup
cd backups/idp
sha256sum -c zitadel_bundle_<timestamp>.sha256
gzip -t zitadel_<timestamp>.sql.gz
```

Restore only into a new empty PostgreSQL 15 database with drill-only
credentials:

```bash
gzip -dc zitadel_<timestamp>.sql.gz \
  | psql -v ON_ERROR_STOP=1 <empty-zitadel-drill-database-url>
```

Start the exact approved image digest against that restored database with the
escrowed master key and an isolated external URL. The valid drill must pass
`/debug/healthz`, `/debug/ready`, OIDC discovery, and one synthetic Authorization
Code + PKCE login against a non-production Vitals stack. Restart the restored
provider and repeat discovery/login. Record only the manifest, snapshot ID,
image digest, and pass/fail timings—never identities, claims, credentials, or
provider rows.

From the trusted restic administration machine, prove that the independent
offsite stream can be selected and restored without the health snapshot:

```bash
export RESTIC_REPOSITORY_FILE=<identity-repository-file>
export RESTIC_PASSWORD_FILE=<identity-password-file>
restic snapshots --tag vitals-idp
restic restore <identity-snapshot-id> --target <temporary-restore-root>
```

Health-store success alone never proves identity-store recoverability. Likewise,
an identity restore does not authorize overwriting or merging the health store.
After owner binding and every new account admission, immediately create both
independent recovery points and record their compatible manifest/snapshot pair;
the health store owns `(issuer, sub)` while the identity store owns that `sub`.
An older identity restore can roll back MFA, sessions, revocations, and users,
so keep it isolated until those states and linked accounts have been reviewed.
