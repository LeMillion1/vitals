# Commercial Subject-Ownership Inventory

Status: PR-03 design and migration source of truth

Last reviewed: 2026-08-19

This document classifies every SQLAlchemy table currently registered in
`Base.metadata` and records the ownership, provenance, key, backfill, and
rollback work required before Vitals may allow a second writable health
subject. It complements `COMMERCIAL_MULTI_USER_ROADMAP.md`; the roadmap owns the
cross-PR sequence, while this file owns the table-by-table rationale and PR-03
migration contract. The machine-readable companion in `vitals/ownership.py`
owns the exact registry membership and target-column categories. Both forms
must change together.

The inventory contains all 55 tables now registered in `Base.metadata`.
Revision `0036` adds the six Stage-0 roots/scoped-setting tables without moving
data, reading credentials, or touching file bytes. Revisions `0037` and `0038`
implement the Stage-1 nullable expansion for all 36 top-level and six inherited
child tables. They deliberately do not backfill rows, remove legacy uniqueness,
change readers, or enable a second writable subject.

## Legend and common rules

- **S**: `subject_id`, initially nullable during schema expansion, referencing
  `health_subjects.id` with `ON DELETE RESTRICT`. It becomes required for PHI
  rows only after backfill and validation.
- **A**: a nullable originating `actor_user_id`, referencing `users.id` with
  `ON DELETE RESTRICT`. A mutable lifecycle may use explicit names such as
  `created_by_user_id`, `revoked_by_user_id`, `resolved_by_user_id`, or
  `updated_by_user_id` instead of an ambiguous generic actor.
- **C**: `integration_connection_id`, referencing a soft-deletable
  `integration_connections.id` with `ON DELETE RESTRICT`.
- **F**: `file_asset_id`, referencing a private `file_assets.id`.
- **R**: `recipient_user_id`, used where delivery ownership differs from the
  health subject.

`Source` remains the ingestion channel. It never substitutes for S, A, C,
relationship, consent, or support-grant identity.

Expansion and backfill follow these shared rules:

1. S, A, C, and F columns are nullable in the expand migration.
2. Every existing PHI row receives the bootstrapped legacy S.
3. Historical A stays null unless an existing durable record proves the actor.
   Backfill must not manufacture attribution to the owner.
4. Provider rows receive C only through an unambiguous source/provider mapping.
   Ambiguous rows are reported and remain blocked from constraint validation.
5. A dated subject-owned table gains `(subject_id, date)` and, where it uses
   `InsightsMixin`, `(subject_id, domain, date)` indexes alongside the legacy
   indexes during expansion.
6. A directly queried child carries S even when ownership can be found through
   a join. The parent receives `UNIQUE(id, subject_id)`, and the child receives a
   composite parent FK so it cannot cross subjects.
7. A normalized row linked to `raw_payloads` must have the same S as the raw row.
   Raw provenance is retained; deletion must not silently detach subject or
   connector identity.
8. Legacy columns, paths, settings, and readers remain available during
   dual-write. Contract removal is a later operation.
9. Health history, provenance, and frozen report content are never discarded by
   the ownership migration.

## Stage-0 roots

PR-03 needs minimal forms of these roots before it can add honest foreign keys.
Revision `0036` creates their schema; runtime bootstrap/backfill remains a
separate, idempotent application operation because Alembic can run before the
legacy owner and subject exist.

### `IntegrationConnection`

The PR-03 form needs only a UUID, S, provider/type, lifecycle status, safe
external-account discriminator, and a credential reference such as
`legacy_env:garmin`. It must not read `.env`, copy plaintext secrets, move Garmin
token files, or start network activity. Credential encryption, cursors,
breakers, leases, and provider operation remain PR-09 work.

At most one legacy logical connection is created for each unambiguous current
provider/account. Garmin API and Health Auto Export rows that describe the same
legacy Garmin account map to the same logical connection. An import-only
connection may exist without credentials.

### `FileAsset`

The PR-03 form needs a UUID, S, uploader A, purpose, opaque storage key, content
type, size/hash when known, and lifecycle timestamps. Existing file keys are
registered without moving or reading the medical files. `file_key` remains in
place for dual-write and rollback. Private serving and object-storage migration
remain PR-06 work.

## Existing-table inventory

| # | Table / model | Target ownership | Key, FK, index, and backfill contract |
| ---: | --- | --- | --- |
| 1 | `annotations` / `Annotation` | S, A | Add subject-leading date and date-range indexes. Backfill legacy S; historical A remains null. |
| 2 | `app_settings` / `AppSetting` | Legacy compatibility only | Keep the current `key` PK for rollback. Add platform, user, subject, and connection-scoped setting tables rather than adding one ambiguous owner column. |
| 3 | `audit_events` / `AuditEvent` | Platform journal with optional existing S/A/grant | Existing RESTRICT FKs and actor/subject/grant indexes remain. Existing platform events are not assigned a fabricated S. The table stays append-only and outside ordinary user backup. |
| 4 | `body_measurements` / `BodyMeasurement` | S, A | Current global `UNIQUE(date)` becomes `(S, date)` at scoped-key cutover. |
| 5 | `body_scan_metrics` / `BodyScanMetric` | S inherited from scan | Add `(scan_id, S) -> body_scans(id, S) ON DELETE CASCADE`; backfill S by joining the parent. |
| 6 | `body_scans` / `BodyScan` | S, A, F | Add `UNIQUE(id, S)`, a subject-safe raw link, and F. Retain `file_key` and the legacy raw FK during dual-write. |
| 7 | `conflict_rules` / `ConflictRule` | Mixed global definitions and subject custom state | Curated catalog definitions stay `S IS NULL`; manual/ad-hoc rules receive S. Replace global key semantics with partial global and `(S, code)` indexes at cutover. Move the user's `active` choice to a subject preference. |
| 8 | `day_context` / `DayContext` | S, A, optional C for channel input | Current global `UNIQUE(date)` becomes `(S, date)`. Telegram-origin rows retain the channel connection. |
| 9 | `garmin_activities` / `GarminActivity` | S, C, optional A | Current global `UNIQUE(external_id)` becomes `(C, external_id)`. Add a subject-safe raw link and backfill the legacy Garmin C. |
| 10 | `garmin_daily` / `GarminDaily` | S, C, optional A | Current global `UNIQUE(date)` becomes `(C, date)`. API and backup sources for the same account use one logical legacy C. |
| 11 | `garmin_intraday` / `GarminIntraday` | S, C | Keep wholesale delete/reinsert semantics, but scope deletion and indexes by C/S: `(C, series_type, date)` and `(C, date, ts)`. Add no uniqueness until upstream duplicate semantics are proven. |
| 12 | `garmin_weight_exports` / `GarminWeightExport` | S, destination C, optional requesting A | Current global `UNIQUE(date)` becomes `(C, date)`. Make the `weight_log_id` reference subject-safe and index retries by `(C, status, next_attempt_at)`. |
| 13 | `genetic_variants` / `GeneticVariant` | S, A | Current partial unique `rsid` becomes `(S, rsid) WHERE rsid IS NOT NULL`. VCF import currently creates raw payloads without linking variants; add a raw/import-batch FK. |
| 14 | `glp1_dose_phases` / `DosePhase` | S, A | Add subject-leading range indexes. Overlapping phases remain valid domain data. |
| 15 | `glp1_injections` / `Injection` | S, A | Add subject/date indexes. Multiple injections on one date remain allowed. |
| 16 | `glp1_side_effects` / `SideEffect` | S, A | Add subject/date indexes. |
| 17 | `health_subjects` / `HealthSubject` | PHI root with existing owner FK | Preserve the unique/restricted owner boundary. This row must exist before every PHI backfill. |
| 18 | `hevy_exercises` / `HevyExercise` | S and C inherited from workout | Add `(workout_id, S) -> hevy_workouts(id, S) ON DELETE CASCADE`; backfill S/C from workout. |
| 19 | `hevy_sets` / `HevySet` | S and C inherited through exercise/workout | Add `(exercise_id, S) -> hevy_exercises(id, S) ON DELETE CASCADE`; backfill through the parent chain. |
| 20 | `hevy_workouts` / `HevyWorkout` | S, C, optional A | Current global `UNIQUE(external_id)` becomes `(C, external_id)`. Add a subject-safe raw link and legacy Hevy C. |
| 21 | `hrt_compound_components` / `HrtCompoundComponent` | Nullable S inherited from compound | Curated components remain global; components of custom compounds receive S. Preserve the parent FK and enforce matching compound ownership. |
| 22 | `hrt_compounds` / `HrtCompound` | Mixed global system catalog and subject custom rows | System rows remain `S IS NULL`; manual rows receive S. Replace global `UNIQUE(key)` with partial global and `(S, key)` uniqueness at cutover. Move the per-user `active` toggle to a subject preference. |
| 23 | `hrt_cycle_items` / `HrtCycleItem` | S inherited from cycle | Add a composite cycle parent FK. A referenced compound must be global or custom-owned by the same S. |
| 24 | `hrt_cycle_template_items` / `HrtCycleTemplateItem` | S inherited from template | Add `(template_id, S) -> hrt_cycle_templates(id, S) ON DELETE CASCADE`. |
| 25 | `hrt_cycle_templates` / `HrtCycleTemplate` | S, A | Add `UNIQUE(id, S)` and subject/name indexes. Duplicate display names remain allowed. |
| 26 | `hrt_cycles` / `HrtCycle` | S, A | Add `UNIQUE(id, S)` and subject-leading range indexes. |
| 27 | `hrt_doses` / `HrtDose` | S, A | Add subject/date and subject/compound indexes. A compound is global or custom-owned by the same S. |
| 28 | `hrt_side_effects` / `HrtSideEffect` | S, A | Add subject/date indexes. |
| 29 | `lab_markers` / `LabMarker` | Current rows are subject-owned | `retest_interval_days`, `defer_until`, and `note` are personal. Current global `UNIQUE(name)` becomes `(S, name)`. A future global marker definition must be a separate table. |
| 30 | `lab_results` / `LabResult` | S, A | Add `(S, marker, date)` and a subject-safe raw link. |
| 31 | `meal_logs` / `MealLog` | S, A | Add subject/date indexes. Multiple meals on one date remain allowed. |
| 32 | `milestones` / `Milestone` | S, A | Add `(S, status)` and subject/deadline indexes. |
| 33 | `noise_markers` / `NoiseMarker` | S, A | Replace the global range access path with `(S, domain, start_date, end_date)`. |
| 34 | `notifications` / `Notification` | S, R, delivery C, optional A/system | Scope dedupe by `(R, C, dedupe_key)`, reply lookup by R/C/external ID, and budget queries by `(R, S, category, sent_at)`. Existing rows map to the owner and legacy Telegram connection. |
| 35 | `progress_photos` / `ProgressPhoto` | S, A, F | Register a FileAsset for every referenced DB key without reading or moving the file. Retain `file_key` during dual-write. |
| 36 | `raw_payloads` / `RawPayload` | S, optional C, optional A, optional F | Add `UNIQUE(id, S)`. Add partial unique `(C, domain, source, external_id)` when C/external ID are present and `(S, domain, source, external_id)` when C is null. Add S/C-aware pending-sweep indexes. |
| 37 | `shared_reports` / `SharedReport` | S, creator A, revoker A | Keep the public token globally unique. Add `(S, created_at)` for owner management. Preserve the frozen snapshot byte-for-byte/checksum through backfill. |
| 38 | `signals` / `Signal` | S, A, optional Telegram C | Add `(S, batch_id)`, `(S, key, date)`, and a subject-safe raw link. |
| 39 | `skincare_logs` / `SkincareLog` | S, A | The service treats this as one row per day but the DB has no unique constraint. Audit duplicates, then add `(S, date)` at cutover. |
| 40 | `skincare_observations` / `SkincareObservation` | S, A | Add subject/date indexes. Multiple observations remain allowed. |
| 41 | `skincare_products` / `SkincareProduct` | S, A | Despite the current “reference” label, schedule and active state are personal. Add subject/name/type indexes. |
| 42 | `supplements` / `Supplement` | S, A | This is a personal regimen, not a global catalog. Add `(S, key)` lookup; make it unique only if the product contract forbids duplicate regimens. |
| 43 | `support_access_grants` / `SupportAccessGrant` | Existing S plus named grantee/approver/revoker | No PR-03 ownership change. It remains control-plane state outside ordinary user backup. |
| 44 | `support_access_scopes` / `SupportAccessScope` | Authorization child owned by one grant | A redundant S is not required: the mandatory grant parent already identifies exactly one subject. |
| 45 | `system_alerts` / `SystemAlert` | Optional S, optional C, named lifecycle actors | Add separate unresolved partial uniques for connection, subject-without-connection, and platform-without-S/C alerts. Add `(S, domain, resolved_at)`, `overridden_by_user_id`, and `resolved_by_user_id`. |
| 46 | `user_roles` / `UserRole` | Account control plane | Preserve additive `(user_id, role)` uniqueness and assignment provenance. A role never grants PHI access. |
| 47 | `users` / `User` | Account control plane | No S. Keep outside ordinary user backup/import. |
| 48 | `weekly_digests` / `WeeklyDigest` | S, optional A/system, optional AI C | Add `(S, kind, date)`. `content` and `context_json` are PHI. Retain model/provider provenance without putting prompts or content in audit metadata. |
| 49 | `weight_logs` / `WeightLog` | S, A, optional C | Current partial unique active date becomes `(S, date) WHERE superseded = false`. Add a subject-safe raw link; direct C preserves provider provenance if the raw link is later absent. |
| 50 | `integration_connections` / `IntegrationConnection` | S-bound connection root | Provider/type plus an opaque account discriminator is unique within S. `credential_ref` is a resolver handle only; secrets, tokens, PII, cursors, and transient sync state are forbidden. |
| 51 | `file_assets` / `FileAsset` | S, optional uploader A | Opaque lookup key is separate from the private backend/storage reference. Legacy rows are placeholders registered from DB references without reading or moving bytes. |
| 52 | `platform_settings` / `PlatformSetting` | Platform control plane | Non-secret installation settings only. No current legacy key is copied here automatically. |
| 53 | `user_settings` / `UserSetting` | Account-scoped preference | Composite key `(user_id, key)`. MFA and credentials are forbidden. |
| 54 | `subject_settings` / `SubjectSetting` | S-scoped preference | Composite key `(S, key)`. Excluded from legacy generic portability until selected-subject backup v2 exists. |
| 55 | `integration_connection_settings` / `IntegrationConnectionSetting` | C-scoped option, S inherited from C | Composite key `(C, key)`. External-action settings are never restored blindly. |

## Critical cross-surface dependencies

### Raw-first ingestion

`vitals/services/raw_payload_service.py` treats
`(domain, source, external_id)` as an upsert key, but `raw_payloads` currently has
only a non-unique index. Pending sweeps are global by domain. PR-03 must establish
S/C before normalization, use the scoped key, and prevent normalized/raw subject
mismatch. Garmin, Hevy, labs, body scans, genetics, Telegram inbound, MCP raw
writes, and reparse/backfill scripts all use this boundary.

### MCP and external APIs

`web/routers/mcp.py` contains direct global selects, generic bare-ID updates,
global export/overview tools, and notification/settings reads. PR-03 dual-write
must propagate the bootstrapped legacy S/A/C without claiming full
authorization; mandatory scoped reads and IDOR closure belong to PR-04/PR-10.

The generic MCP `_ROW_NOISE` set and data-portability `_LLM_SKIP_COLUMNS` must
exclude S/A/C/F plumbing before those columns are added, otherwise UUIDs leak
into every generic MCP/LLM response. `Source.MCP` remains channel provenance;
the authenticated user/token is A.

### Files and uploads

`/static/uploads/{key}` currently checks only for an authenticated session.
Progress photos and body scans store path-like keys, and lab documents reuse a
raw external ID rather than a file registry. PR-03 adds ownership metadata and
dual-write; PR-06 changes serving to opaque FileAsset policy checks. A known key
or raw ID must never be sufficient to bind or download another subject's file.

### Reports, alerts, notifications, and outbox

- report list/get/revoke/delete and snapshot construction are global;
- alert dedupe, dismissal, resolve/override, and resolve-all are global;
- notification dedupe, daily budgets, external reply lookup, and recent history
  are global;
- `garmin_weight_exports` is a real transactional outbox, while `notifications`
  is primarily a delivery journal and must not be treated as the same lifecycle.

Every one of these paths needs S and the relevant actor/recipient/connection
before a second writable subject exists.

### Integrations, scheduler, and Redis

Garmin, Hevy, Telegram, and OpenRouter configuration is currently process-wide.
Scheduled jobs run once for the installation, and locks/caches use global names
such as `scheduler:lock:{job_id}`, `settings:enabled_modules`, and
`settings:custom_charts`. PR-03 records ownership and connection identity. PR-09
turns jobs into per-connection dispatchers and namespaces locks, cursors,
breakers, budgets, dedupe, and caches.

### Analytics, conflict checks, exports, and service lookups

Most domain services, analytics, conflict resolvers, dashboards, digest/LLM
context, external summaries, and data-portability queries currently read the
whole lake. PR-03 must not imply these are safe merely because columns exist.
Registration stays closed; PR-04 makes AccessContext and scoped lookup mandatory
and adds PostgreSQL RLS as a second boundary.

## AppSetting scope map

The existing single-row-per-key table remains a compatibility store during
dual-write. Known keys map as follows:

| Key | Target scope | Notes |
| --- | --- | --- |
| `ui_language` | User | Redis cache must include user ID. |
| `twofa_secret` | Dedicated user-security/MFA storage | Do not move it to an ordinary exportable setting bag. |
| `enabled_modules` | Subject | Platform feature availability, if added, is a separate platform setting. |
| `custom_charts` | Subject | Cache key includes S. |
| `week_template` | Subject | Day interpretation uses the subject timezone. |
| `proactive` | Subject plus recipient/channel | Delivery times, quiet hours, and budgets must not be global. |
| `garmin_weight_export_enabled` | Integration connection | It controls one destination connection/outbox. |

Reads use new scoped state first and legacy fallback while only the bootstrapped
subject is writable. Writes update both stores. Unknown keys remain in the
legacy table and are listed in a migration report; the migration must not guess
their scope.

## Ordinary backup v2 and identity remapping

The ordinary user backup contract excludes durable control-plane tables:
`users`, `user_roles`, `health_subjects`, support grants/scopes, audit events,
and live shared reports. Import cannot delete, load, reset sequences for, or
otherwise mutate those tables.

Once PHI rows carry S/A/C, raw UUID FKs cannot be blindly restored into a fresh
installation whose durable IDs differ. Backup v2 therefore needs an explicit
logical subject envelope and these rules:

1. Export exactly one selected subject and no unrelated rows.
2. Do not export password hashes, roles, MFA, consent, support state, audit
   events, credentials, provider token material, or live share tokens.
3. On import, bind exported PHI to the already-authorized current subject rather
   than inserting or replacing `HealthSubject`.
4. Remap internal portable parent/child/raw/file IDs within the import graph.
5. Remap C through a safe provider/account discriminator. Never plant a
   credential reference from the file. Unresolved C remains unbound and is
   reported rather than guessed.
6. Do not assign an imported historical row to the importing user as its
   original A. Record the restore action in AuditEvent and leave unprovable
   historical actor attribution null.
7. A same-install restore preserves the durable current subject and control
   plane. A fresh-install restore uses the same logical remap path.
8. A user import replaces only the selected subject's portable data and never
   performs a global table wipe.

Medical file bytes are not currently part of the JSON backup. FileAsset metadata
must not imply that the file itself was backed up. A later archive format needs
separate encryption, size limits, checksums, and private restore handling.

An operator disaster-recovery backup, if introduced, is a separate encrypted
and access-controlled product. It must not weaken the ordinary user contract.

## Staged migration and deployment sequence

### Stage 0 — Registry and roots

- keep the static ownership registry exhaustive across all 55 current tables;
- add minimal IntegrationConnection and FileAsset roots;
- add scoped setting/preference tables;
- keep provider clients, credentials, and files untouched;
- verify ORM/Alembic/create-all parity.

### Stage 1 — Nullable expansion

- add nullable S/A/C/F to top-level tables;
- add S to directly queried children;
- add non-unique supporting indexes and parent `(id, S)` uniqueness;
- retain legacy fields, paths, settings, readers, and global unique constraints.

This stage is schema-reversible while the new columns are empty.

### Stage 2 — Legacy dual-write

- derive the current explicit S/A/C from the PR-02 AccessContext/bootstrap;
- cover web, MCP, scheduler, upload, Telegram, Garmin, Hevy, manual import, and
  raw-reparse writes;
- write raw ownership before normalized rows;
- dual-write scoped settings/FileAsset metadata and their legacy representation;
- keep registration disabled and readers on the verified legacy path.

### Stage 3 — Resumable bounded backfill

Backfill in dependency order:

1. resolve the bootstrapped legacy subject;
2. create safe legacy connection placeholders;
3. backfill raw payloads;
4. backfill normalized top-level PHI;
5. derive child S through parent joins;
6. backfill reports, notifications, alerts, and outbox rows;
7. register DB-referenced file keys without reading file contents;
8. copy known scoped settings while retaining `app_settings`.

The job is idempotent, batches by stable PK, stores a checkpoint, and emits only
counts/opaque IDs. It records before/after counts and deterministic checksums for
data/provenance fields, raw links, and frozen reports. Zero orphans, ambiguous
connection mappings, and duplicate candidates are hard gates, not warnings.

Do not hide a large production data rewrite inside one unbounded Alembic
transaction. Alembic owns schema; a reviewed resumable operation owns the data
backfill and produces a validation report.

### Stage 4 — Ownership validation

- add PostgreSQL FKs/checks as `NOT VALID`, then validate them separately;
- validate parent/child and raw/normalized subject equality;
- verify every new write is populated by dual-write;
- perform scoped shadow reads and compare counts/checksums with legacy reads;
- keep old global unique constraints at this stage.

### Stage 5 — Gated scoped-key cutover

Global uniqueness and duplicate acceptance cannot coexist. The cutover is a
separate, explicit gate after expansion/backfill validation and before any
second subject becomes writable:

1. audit and resolve duplicate candidates under the proposed scoped keys;
2. create scoped unique/partial indexes, preferably concurrently on PostgreSQL;
3. switch every upsert/select/delete path to the scoped key;
4. run two-subject collision and concurrency tests;
5. remove the corresponding old global unique constraint;
6. re-run migration, rollback-boundary, and full isolation smoke tests.

Registration and any other path that can create a second writable subject remain
disabled throughout this cutover.

### Stage 6 — Scoped read and RLS cutover

PR-04 removes bare-ID/global reads, requires AccessContext, and enables FORCE RLS
only after application scoping is complete. Nullable compatibility columns are
not removed until the later contract migration.

## Rollback boundary

Before a second subject can write:

- code may roll back to legacy readers and disable new dual-write;
- nullable columns and backfilled values remain in place;
- legacy paths/settings still work;
- if scoped-key cutover has removed a global unique, it may be recreated only
  after proving all current data still satisfies it.

After a second subject has written data, especially a value that intentionally
duplicates a legacy global date/upstream ID/key:

- downgrade to the single-subject/global-key schema is forbidden;
- ownership columns, scoped keys, and data must not be dropped or merged;
- recovery requires a verified backup and a forward fix;
- an old binary that cannot express S/C must not be started against that data.

Alembic downgrade of populated ownership columns is therefore valid only before
the irreversible writable-subject gate. A nominal downgrade function must fail
closed or be protected by a precondition once scoped duplicate data can exist.

## Test and release gates

### Metadata and migration

- every `Base.metadata` table has an explicit ownership classification;
- model, create-all, migration upgrade, and reversible pre-gate downgrade agree;
- a real revision-0034 snapshot upgrades with counts, checksums, provenance,
  raw links, frozen reports, and file references intact;
- no orphan S/C/F, cross-subject child, or raw/normalized mismatch remains.

### Scoped keys

- two subjects may use the same active weight date, body-measurement date,
  day-context date, rsID, alert key, notification dedupe key, and raw external ID;
- two connections may use the same Garmin activity ID, Garmin date, Hevy workout
  ID, and outbox date;
- concurrent scoped upserts are idempotent on PostgreSQL 15;
- the old global constraint is removed only after its replacement is valid.

### Provenance and behavior

- new web/MCP writes retain both Source and real A;
- scheduler/provider writes retain C and do not invent a human A;
- legacy historical rows with unknown actors remain null;
- settings/cache values cannot cross user/S/C;
- FileAsset, report, alert, notification, and outbox ownership is explicit;
- MCP and LLM serializers do not expose S/A/C/F plumbing.

### Portability

- ordinary export contains one subject and no identity/control-plane secrets;
- legacy import cannot delete or replace durable owner/subject/roles/audit;
- forged control-plane sections are ignored;
- backup v2 remaps S/C/internal IDs and never globally wipes user data;
- file metadata does not falsely claim that file bytes were archived.

### Database and operational gates

- SQLite fast tests cover compatibility behavior;
- PostgreSQL 15 tests cover partial unique indexes, composite FKs, `NOT VALID` /
  `VALIDATE`, concurrent upsert, and the downgrade precondition;
- provider clients remain fake and no test reads credentials, health data,
  upload contents, or sends Telegram/provider traffic;
- `git diff --check`, focused tests, then the full fast suite are recorded;
  Ruff unavailability is reported rather than treated as a pass.

## Maintenance rule

Any new SQLAlchemy table, write service, MCP tool, HTTP route, upload, report,
job, Redis key, connector, or export must update this inventory and the
machine-readable registry before merge. An unclassified table or write path is
a failing security contract, not deferred documentation work.
