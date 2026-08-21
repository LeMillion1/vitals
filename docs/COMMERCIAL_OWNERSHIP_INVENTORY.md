# Commercial Subject-Ownership Inventory

Status: PR-03 Stage-3A / Stage-3B / Stage-3C / Stage-3D / Stage-3E / Stage-3F / Stage-3G / Stage-3H / Stage-3I / Stage-3J / Stage-3K / Stage-3L / Stage-3M / Stage-3N / Stage-3O / Stage-3P / Stage-3Q implementation source of truth

Last reviewed: 2026-08-21

This document classifies every SQLAlchemy table currently registered in
`Base.metadata` and records the ownership, provenance, key, backfill, and
rollback work required before Vitals may allow a second writable health
subject. It complements `COMMERCIAL_MULTI_USER_ROADMAP.md`; the roadmap owns the
cross-PR sequence, while this file owns the table-by-table rationale and PR-03
migration contract. The machine-readable companion in `vitals/ownership.py`
owns the exact registry membership and target-column categories. Both forms
must change together.

The original table-by-table inventory contains the 55 tables present at the
Stage-0 expansion. The post-foundation control-plane tables below bring the
current exhaustive `Base.metadata` and machine registry to 62 tables.
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
| 48 | `weekly_digests` / `WeeklyDigest` | S, optional A/system, optional AI invocation | Add `(S, kind, date)`. `content` and `context_json` are PHI. New model-generated rows link to subject-owned `AIInvocation`; historical subject OpenRouter C stays bridge provenance. Never put prompts or content in audit metadata. |
| 49 | `weight_logs` / `WeightLog` | S, A, optional C | Current partial unique active date becomes `(S, date) WHERE superseded = false`. Add a subject-safe raw link; direct C preserves provider provenance if the raw link is later absent. |
| 50 | `integration_connections` / `IntegrationConnection` | S-bound connection root | Provider/type plus an opaque account discriminator is unique within S. New OpenRouter writes move to the separate platform gateway below; existing subject OpenRouter rows remain immutable historical provenance during the bridge. `credential_ref` is a resolver handle only; secrets, tokens, PII, cursors, and transient sync state are forbidden. |
| 51 | `file_assets` / `FileAsset` | S, optional uploader A | Opaque lookup key is separate from the private backend/storage reference. Legacy rows are placeholders registered from DB references without reading or moving bytes. |
| 52 | `platform_settings` / `PlatformSetting` | Platform control plane | Non-secret installation settings only. No current legacy key is copied here automatically. |
| 53 | `user_settings` / `UserSetting` | Account-scoped preference | Composite key `(user_id, key)`. MFA and credentials are forbidden. |
| 54 | `subject_settings` / `SubjectSetting` | S-scoped preference | Composite key `(S, key)`. Excluded from legacy generic portability until selected-subject backup v2 exists. |
| 55 | `integration_connection_settings` / `IntegrationConnectionSetting` | C-scoped option, S inherited from C | Composite key `(C, key)`. External-action settings are never restored blindly. |

### Post-foundation control-plane additions

The centrally funded OpenRouter, durable Telegram-delivery, and bounded-backfill
slices add reviewed control state above the original 55-table inventory rather
than weakening subject-bound data roots:

| Table | Ownership | Contract |
| --- | --- | --- |
| `platform_integration_connections` / `PlatformIntegrationConnection` | Platform control plane | One installation-wide OpenRouter AI-gateway root, configured only by an active `platform_superadmin`. It has no S and stores an opaque credential reference, lifecycle, and non-secret configuration version; secrets remain outside ordinary DB/settings/export paths. |
| `ai_platform_quota_periods` / `AIPlatformQuotaPeriod` | Platform control plane | Installation-wide bounded usage ledger. It contains no prompt, completion, document, or medical value and is excluded from ordinary subject portability. |
| `ai_subject_quota_periods` / `AISubjectQuotaPeriod` | Required S control state | Opaque-S quota ledger aligned to the platform period. It is accounting and authorization state, not a PHI artifact or ordinary export row. |
| `ai_invocations` / `AIInvocation` | Required S, optional A/system, required platform gateway | One paid-call reservation/outcome with purpose, source, model/config version, idempotency, lifecycle, opaque upstream ID, token counts, and cost microunits. Prompts, completions, documents, raw payloads, and medical values are forbidden. `UNIQUE(id, S)` supports composite subject-equality links from artifacts. |
| `legacy_openrouter_connection_bridges` / `LegacyOpenRouterConnectionBridge` | Platform control child | Exact mapping from one historical subject OpenRouter C to the platform gateway. It preserves provenance but never grants PHI access or copies a credential. |
| `notification_delivery_intents` / `NotificationDeliveryIntent` | Required S and recipient/delivery C, optional A/system | Payload-free at-most-once delivery lifecycle. Text, buttons, recipient address, credentials, and free-form provider errors are forbidden. |
| `ownership_backfill_checkpoints` / `OwnershipBackfillCheckpoint` | Required S control state | One versioned phase checkpoint with stable scan watermarks, cumulative counts, operational timestamps, and lowercase SHA-256 digests. It contains no row payload, title, medical/event date, file path, credential, or free-form error and is excluded from ordinary portability. Any populated checkpoint makes revision `0045` downgrade fail before DDL. |

`WeeklyDigest`, AI-parsed `RawPayload`, AI-assisted `Signal`/`Notification`, and
future AI artifacts link to `AIInvocation`, not to a fabricated per-subject
OpenRouter connection. Existing subject OpenRouter C values are preserved and
mapped explicitly to the platform root during expand/backfill; they are never
silently reassigned or deleted.

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
turns subject-provider jobs into per-connection dispatchers and namespaces locks,
cursors, breakers, budgets, dedupe, and caches. OpenRouter is deliberately
different: one superadmin-managed platform root pays for all subjects, while
subject-owned `AIInvocation` reservations enforce authorization, idempotency,
per-subject quotas, provenance, and usage accounting.

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
| OpenRouter endpoint/model allowlist/quota defaults | Platform gateway/settings | Active platform superadmin only; secrets remain in a dedicated secret resolver, never generic settings. |
| AI feature opt-in and subject quota override | Subject | Availability preference only; it never grants access to the subject or to additional prompt domains. |

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
9. A v1 full restore never imports checkpoint contents. A non-empty restore has
   stripped A/C/F and cannot prove whether a row came from a historical connector
   or the platform parser, so the same transaction records
   `stage3.raw_payloads.v1` as `RESTORE_BLOCKED`; ordinary Stage-3A apply cannot
   guess or clear it. A future backup-v2 remap or reviewed manual recovery must
   supply the missing provenance. An empty restore records an empty `COMPLETED`
   checkpoint. If retained AI-invocation or durable-delivery control rows still
   reference a raw row, v1 replacement refuses before deleting anything. A
   checkpoint is operational state, never authorization: consumers continue to
   reject S-only restored history.

Medical file bytes are not currently part of the JSON backup. FileAsset metadata
must not imply that the file itself was backed up. A later archive format needs
separate encryption, size limits, checksums, and private restore handling.

An operator disaster-recovery backup, if introduced, is a separate encrypted
and access-controlled product. It must not weaken the ordinary user contract.

## Staged migration and deployment sequence

### Stage 0 — Registry and roots

- keep the static ownership registry exhaustive across all 62 current tables;
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
bounded counts and deterministic checksums. It records before/after evidence for
data/provenance fields, raw links, and frozen reports. Zero orphans, ambiguous
connection mappings, and duplicate candidates are hard gates, not warnings.

Do not hide a large production data rewrite inside one unbounded Alembic
transaction. Alembic owns schema; a reviewed resumable operation owns the data
backfill and produces a validation report.

The implemented Stage-3A slice is limited to step 3, under the immutable phase
key `stage3.raw_payloads.v1`. Revision `0045` adds its subject-bound checkpoint
but does not rewrite data. The operator command is deliberately fixed-target:

```bash
# Read-only status and complete preflight (the default).
python scripts/backfill_subject_ownership.py

# Advance at most one independently committed batch.
python scripts/backfill_subject_ownership.py --apply

# Explicit bounded maintenance window: at most 10 batches of 500 rows.
python scripts/backfill_subject_ownership.py \
  --apply --batch-size 500 --max-batches 10
```

Batch size defaults to 250 and is limited to 1–1000; `max-batches` defaults to
1 and is limited to 1–100. Each batch and its checkpoint commit atomically, so a
later invocation resumes after the last committed stable-PK cursor. The operation
is a maintenance boundary: all raw ingest, refresh, replay, and import writers
remain paused from the first mutating batch through completion. The initial high
watermark freezes the reviewed population; new rows above it are reported and
must already satisfy the live dual-write graph rather than being silently folded
into the old scan. Before `COMPLETED`, the service locks and keyset-rehashes the
entire frozen snapshot with bounded page memory; any cross-batch payload, count,
or ownership drift fails closed. There is no reset/rebase, delete, arbitrary
table/phase, or command-line DB URL. Standard output is one versioned JSON object
containing only phase/status, counts, completion/result codes, and deterministic checksums. Subject,
checkpoint, and raw row IDs—even internal cursor/high-watermark IDs—are not
serialized. Remaining normalized, child, report/notification/alert/outbox,
file, and setting phases require separate reviewed phase keys and are still
pending.

Stage 3B begins step 4 with the fixed group
`stage3.normalized_manual.v1`. It covers exactly these 17 integer-PK tables:
`hrt_cycles`, `hrt_cycle_templates`, `annotations`, `body_measurements`,
`glp1_dose_phases`, `glp1_injections`, `glp1_side_effects`, `hrt_doses`,
`hrt_side_effects`, `lab_markers`, `meal_logs`, `milestones`, `noise_markers`,
`skincare_logs`, `skincare_observations`, `skincare_products`, and
`supplements`. These rows require S, allow historical A to remain null, and do
not require C/F/raw provenance inference. Provider/raw-sensitive, file-backed,
mixed-catalog, child, artifact, alert, outbox, report, and setting tables remain
outside this phase.

Each catalog table has its own deterministic checkpoint key because one BIGINT
cursor cannot represent 17 independent PK streams. The fixed-target operator is:

```bash
python scripts/backfill_normalized_subject_ownership.py
python scripts/backfill_normalized_subject_ownership.py --apply
python scripts/backfill_normalized_subject_ownership.py \
  --apply --batch-size 500 --max-batches 10
```

The command exposes no table/phase/reset/delete/DB-URL selector. Stage 3A must
already be `COMPLETED`. At or below each frozen watermark, only fully-null S/A
history may gain the sole S; actor attribution is never invented. Exact S with
null A remains valid history, and exact S plus the active owner is an unchanged
dual-write row. Partial/foreign roots and unknown domain/source values fail
closed. New rows above a watermark require strict live S+A, except reviewed
actorless LabMarker seeds. HRT parent/child and dose/compound references are
validated but not rewritten; `skincare_logs(date)`,
`body_measurements(date)`, and `lab_markers(name)` duplicate candidates are hard
gates. Every ownership-only update preserves `created_at`, `updated_at`, and all
business/provenance values.

All 17 writers stay paused from the first mutating batch through catalog
completion. Final transition for each table takes PK-ordered locks and rehashes
the frozen snapshot with bounded row materialization. Backup v1 replaces these
portable tables after binding them to the sole local S and dropping A, so the
same transaction resets their fixed checkpoints to a new reviewed snapshot;
empty tables complete immediately. This reset is not available from the CLI and
does not authorize provider/raw/file provenance.

Historical actorless provider/parser rows may receive C only from the exact
same-subject `legacy_singleton_v1` provider/type root; its retired lifecycle is
valid historical provenance, while rotated/current accounts are never guessed.
That compatibility bridge is not an ingress authority: every new
provider/parser write remains on the strict live S/A/C-or-artifact dual-write
boundary.

Stage 3C continues step 4 with the fixed inherited-child group
`stage3.inherited_children.hrt.v1`. It covers exactly `hrt_cycle_items` and
`hrt_cycle_template_items`, in that order. Stage 3A and all 17 Stage-3B table
checkpoints must already be `COMPLETED`. A historical child with `S=NULL` gains
only the exact S of its reviewed cycle/template parent; an exact-S child is
unchanged, and a foreign S fails closed. The operation never creates A/C/F or
changes schedules, compounds, timestamps, or other medical values. Cycle-item
compound links are validation-only and cannot be used to infer ownership. A
fully unowned custom compound is tolerated only for a child inside the frozen
historical snapshot; an appended live child requires a same-subject custom
compound. Strict conflict resolution for the historical custom-compound case
still waits for the separate mixed-catalog phase.

Each child table has its own deterministic checkpoint. The fixed-target
operator is:

```bash
python scripts/backfill_hrt_child_subject_ownership.py
python scripts/backfill_hrt_child_subject_ownership.py --apply
python scripts/backfill_hrt_child_subject_ownership.py \
  --apply --batch-size 500 --max-batches 10
```

The command has no table, phase, reset, delete, or database-URL selector. The
HRT writers remain paused for the complete multi-batch window; finalization
locks and rehashes the complete two-table snapshot. Backup v1 atomically resets
the two checkpoints to incoming ID/count bounds after the raw and Stage-3B
checkpoint transitions; empty tables complete immediately. `body_scan_metrics`,
`hevy_exercises`, `hevy_sets`, and `hrt_compound_components` remain deferred
until their raw/file/provider or mixed-catalog parents are reviewed.

Stage 3D continues step 4 with `stage3.provider_raw_linked.v1` over exactly
`garmin_daily`, `garmin_activities`, `garmin_intraday`, and `hevy_workouts`.
Every candidate must link an exact reviewed raw row: daily Garmin API and HAE
use `daily:<date>` / `hae:<date>`, activities use
`activity:<external_id>`, intraday samples share their `daily:<date>` raw, and
Hevy raw/payload/normalized external IDs agree. Historical all-null or exact-S
rows gain only the raw's exact S/C; A remains the original nullable attribution
and is never forced to equal a later refresh raw. Foreign or partial roots fail.

The sole historical cross-channel exception is an HAE daily row retaining an
older Garmin API `daily:<same date>` raw; the reverse mismatch and every live
tail mismatch fail. Future duplicate gates are `(C,date)` for daily and
`(C,external_id)` for activities/workouts; intraday deliberately has no unique
sample key. Hevy descendants are validated read-only and remain transitional
until their child phase. Intraday is a replace-whole-series table, so the initial
completion snapshot is frozen under the writer pause, while later completed
status validates the current strict rows rather than requiring deleted historical
sample IDs to survive forever.

The fixed-target operator is:

```bash
python scripts/backfill_provider_raw_subject_ownership.py
python scripts/backfill_provider_raw_subject_ownership.py --apply
python scripts/backfill_provider_raw_subject_ownership.py \
  --apply --batch-size 500 --max-batches 10
```

Stage 3A, all Stage-3B tables, and both Stage-3C tables must be completed before
ordinary apply. All Garmin/HAE/Hevy writers remain paused throughout the
multi-batch window. Backup v1 strips C from both raw and normalized rows; the
same import transaction therefore records every non-empty Stage-3D table as
`RESTORE_BLOCKED`, while empty tables complete. No Stage-3D CLI reset or remap
exists.

Stage 3E continues inherited-child migration with
`stage3.inherited_children.hevy.v1` over exactly `hevy_exercises` and
`hevy_sets`. An exercise copies missing S/C only from its exact reviewed
Stage-3D workout. A set copies only from its exact exercise after that table's
checkpoint completes, while also proving the exercise/workout S/C chain.
Historical `(NULL,NULL)` and backup-v1 `(parent S,NULL)` shapes are transitional;
connection-only, foreign, orphaned, or live-null rows fail closed. No child fact,
timestamp, actor, raw link, or synthetic uniqueness is changed.

The fixed-target operator is:

```bash
python scripts/backfill_hevy_child_subject_ownership.py
python scripts/backfill_hevy_child_subject_ownership.py --apply
python scripts/backfill_hevy_child_subject_ownership.py \
  --apply --batch-size 500 --max-batches 10
```

Every Hevy writer remains paused until both checkpoints complete. Owned refresh
rebuilds both child tables, so later completed checks validate all current strict
rows without requiring frozen IDs to survive. Backup v1 rebinds child S but
strips C; the same import transaction records non-empty Stage-3E snapshots as
`RESTORE_BLOCKED` after Stage-3D handling, while empty snapshots complete. No
Stage-3E remap/reset CLI exists.

Stage 3F continues with `stage3.mixed_catalog.hrt.v1` over exactly
`hrt_compounds` and `hrt_compound_components`. Checked-in HRT/system definitions
remain global and must match their current YAML scalars and complete component
multiset. Reviewed historical manual/MCP definitions outside the curated key
set gain only the sole S, preserve nullable A, and pass that S to their exact
components. Dose/cycle-item links and snapshot keys are validation-only; no
actor, connection, file, raw link, or medical value is invented.

The initial full snapshot is locked and rehashed under an HRT/catalog writer
pause. After completion, the durable frozen ownership checksum covers custom
parents/components only; current global definitions are validated against the
current checked-in catalog so a legitimate catalog reseed can replace component
IDs without hiding custom history loss or ownership drift. Backup v1 retains
source/key and its subject-bound marker, so import resets the exact two Stage-3F
checkpoints to bounded RUNNING/empty-COMPLETED states after the Stage-3E
transition and before replacement. Catalog synchronization fails closed on any
custom row colliding with a curated key and never co-opts its medical data.
The fixed-target `scripts/backfill_hrt_compound_subject_ownership.py` command is
read-only by default, commits at most one bounded batch per transaction, and
emits only allowlisted counts, result codes, and checksums. Stage 3F does not
claim HRT activation, sharing, custom-CRUD, or conflict-reader cutover; those
remain subject-scoping work after the data phase.

All Stage-3F whole-graph checks and rehashes use fixed PK keyset pages. The
service rejects unequal persisted data-digest chains, impossible prior/own
checkpoint pairs, reverse parent/component restore bounds, and noncanonical
custom keys. Catalog synchronization locks matching rows before its collision
decision; a PostgreSQL two-session regression proves a concurrent custom
recategorization cannot be overwritten.

Stage 3G continues with `stage3.mixed_catalog.conflict_rules.v1` over exactly
`conflict_rules`. Current checked-in YAML definitions remain global only when
all catalog-owned fields match exactly. Reviewed historical custom rows with a
null code gain only the sole S; `active`, the evidence citation stored in
`source`, JSON conditions, medical copy, and timestamps are never rewritten.
Known catalog codes cannot become subject-owned, while noncatalog custom codes
must be nonblank and already subject-owned when written after the frozen HWM.

Initial completion performs a locked all-row data rehash, then retains an
ownership checksum only for frozen custom rows. Current curated rows are always
revalidated against YAML, allowing a legitimate catalog reseed without hiding
custom deletion, reclassification, or S drift. Catalog synchronization takes
identity governance and ordered row locks before it preserves `active` and
refreshes catalog fields; a subject-owned code collision fails closed. Backup v1
retains the subject-bound marker and IDs, so its single Stage-3G checkpoint is
reset to bounded RUNNING or exact-empty COMPLETED before replacement. This phase
does not retire the legacy activation bridge or authorize unscoped conflict
composition and alert reads.

Stage 3H continues with `stage3.file_backed.progress_photos.v1` over exactly
`progress_photos`. The initial bounded run is performed under a complete
progress-photo upload/delete maintenance pause. A reviewed fully-unowned legacy
photo gains only the sole S and a new metadata-only FileAsset root; historical
A and the placeholder uploader remain null because the old authenticated route
does not prove who originally uploaded a particular file. The existing
`file_key`, photo data, timestamps, and bytes are not changed, read, moved,
hashed, or tested for existence. New/live rows retain their exact owner actor
and uploader.

Only canonical root-level `uploads/` image references are eligible. Duplicate
photo keys, duplicate or cross-table F use, unsafe paths, document-path aliases,
retired roots, partial ownership, and non-bijective live FileAsset/photo graphs
fail closed. Initial completion locks and hashes the reviewed photo/file graph.
Later completed status revalidates the entire current graph rather than freezing
deleted photo IDs: the supported delete path first retires FileAsset metadata
and then removes the fact, while an unlinked live progress-photo asset remains
an integrity error. A validated checkpoint prefix temporarily distinguishes
processed actorless history from the unprocessed fully-null compatibility tail;
it never substitutes for the required S+F authorization graph.

Backup v1 carries neither file bytes nor trustworthy A/F. Import therefore
records a nonempty Stage-3H snapshot as `RESTORE_BLOCKED`, leaves restored S-only
photos inaccessible, and never creates a placeholder for a file that was not in
the backup. An empty snapshot is exact `COMPLETED`. Replacement retires only
outgoing photo assets, preserves the physical files, validates the blocked or
empty incoming shape in the same transaction, and requires backup v2 or an
explicit reviewed recovery before nonempty restored history can be activated.

Stage 3I continues with `stage3.channel_optional.day_context.v1` over exactly
`day_context`. Under a complete plan/answer/import writer pause, a reviewed
fully-null S/A/C row gains only the sole S. Existing exact S plus nullable owner
A and nullable same-subject historical Telegram-recipient C is preserved; the
phase never manufactures either optional provenance root from source or from a
sole current channel. Answers, planned context, date, domain, source, and
timestamps remain untouched by the migration.

Initial completion locks and rehashes the complete frozen snapshot. The model
is overwrite-in-place by design, so later completed checks validate the whole
current graph and allow legitimate plan/answer updates without requiring the
frozen data digest to remain. Frozen IDs and cardinality remain durable
migration evidence; deleting a frozen row fails closed, while a new strict row
above the HWM is validated as live data.
The legacy global `UNIQUE(date)` still serializes the single-user natural key
until the later `(S, date)` constraint cutover.

Backup v1 retains the subject-bound marker and business content while stripping
A/C. Import rebinds S, resets the exact Stage-3I checkpoint after Stage 3H and
before replacement, and records a nonempty snapshot as RUNNING or an empty one
as COMPLETED. Recompletion leaves unknown A/C null. The fixed-target operator
is read-only by default, commits one bounded batch per transaction, and emits
only allowlisted aggregate counts and checksums. Consumer-bridge retirement,
the scoped uniqueness cutover, and registration remain later gates.

Stage 3J continues with `stage3.channel_optional.signals.v1` over exactly
`signals`. Under a complete ingest/reparse/MCP/misparse/delete/import writer pause,
a reviewed fully-null S/A/C row gains only the sole S. Existing A remains null
or the sole owner, and existing C remains null or an exact same-subject
historical Telegram-recipient root; neither is inferred. MCP facts remain
raw/channel-neutral. Historical Telegram facts may retain an exact linked
Signals/Telegram raw row, while new rows above the HWM require the complete
owner/recipient/raw graph. The only actorless above-HWM exception is a late
reparse from the exact S+C/A-null Telegram raw row already covered by the
validated Stage-3A HWM; the fact must retain that raw and recipient exactly.
Rows sharing a batch must agree on date, source, actor, channel, and raw
provenance, and one raw message cannot be split across normalization batches.

Initial completion locks recipient roots, raw rows, and signal facts in canonical
order and rehashes the frozen data and ownership snapshot. Signals remain
volatile after completion: supported misparse, delete, reparse, and new
ingest operations are validated against the current graph rather than requiring
the original cardinality or business digest to remain. Backup v1 preserves the
signal business fields and `raw_id`, rebinds S, and strips optional A/C from both
fact and raw rows. Import resets the exact Stage-3J checkpoint after Stage 3I and
before replacement to RUNNING for a nonempty snapshot or exact COMPLETED for an
empty snapshot. Recompletion never fabricates the stripped actor or recipient.

Stage 3K continues with `stage3.retained_artifact.shared_reports.v1` over
exactly `shared_reports`. A frozen report is adopted only from the fully-null
S/creator/revoker shape and gains S alone; no actor is inferred and no token,
password hash, snapshot, title, date, lifecycle value, counter, or timestamp is
rewritten. A RUNNING checkpoint exposes only its unprocessed frozen tail through
the fully-null compatibility bridge. Exact-S historical actor shapes remain
valid anywhere at or below the snapshot HWM, while every report created above
the HWM must have the strict live S+creator graph. Current reports are
revalidated after legitimate open, revoke, purge, delete, and new-create
volatility rather than comparing live data to the initial frozen digest.

Backup v1 excludes `shared_reports` from both export and replacement so it cannot
carry password hashes or resurrect public links. Under the same governance-locked
restore transaction, Stage 3K therefore validates and preserves an existing
checkpoint or prepares one from the retained local report set, without accepting
incoming report bounds or mutating report rows. Post-load preflight revalidates
the retained graph before commit.

Stage 3L continues with `stage3.channel_optional.weight_logs.v1` over exactly
`weight_logs`. Under a complete manual/MCP/Garmin/body-scan writer pause, a
reviewed fully-null S/A/C row gains only the sole S. Existing exact S plus a
nullable owner A and a nullable same-subject Garmin-account or OpenRouter
AI-gateway C is preserved; the phase never manufactures either optional root
from `source`, from a sole current connection, or from the raw payload the fact
links. Mass, note, supersession, date, domain, source, raw link, and timestamps
remain untouched.

Provenance is validated per source: manual and MCP facts cannot claim a provider
connection and may only carry a weight-domain raw of the same source; Garmin
facts require a Garmin-domain `garmin_api` raw; body-scan facts require a
body-composition raw from either the vision parser or structured MCP. Parser
invocations must belong to the same subject and may not coexist with a
subject-funded provider C on the same raw. Because backup v1 strips raw C and
records Stage 3A/3D as restore-blocked, a connection-stripped or still-unowned
raw stays valid provenance for a historical fact, while an adopted fact linking
fully-unowned raw history fails closed.

Initial completion locks provider roots, raw payloads, and facts in canonical
order and rehashes the frozen snapshot. Weights stay volatile afterwards, so
later completed status validates the whole current graph rather than requiring
the initial cardinality or digest. The phase additionally proves the
one-active-weight-per-date invariant that the later
`(S, date) WHERE superseded = false` cutover must satisfy; the legacy global
partial unique still serializes it today.

Backup v1 retains the weight business fields and `raw_payload_id`, rebinds S,
and strips A/C. Import resets the exact Stage-3L checkpoint after the retained
Stage-3K preparation and before replacement, records a nonempty snapshot as
RUNNING or an empty one as COMPLETED, and revalidates the restored graph before
commit. Recompletion leaves unknown A/C null. The fixed-target operator is
read-only by default, commits one bounded batch per transaction, and emits only
allowlisted aggregate counts and checksums.

Stage 3M continues with `stage3.raw_linked_facts.lab_results.v1` over exactly
`lab_results`. Under a complete manual/MCP/parser writer pause, a reviewed
fully-null S/A row gains only the sole S. Existing exact S plus a nullable owner
A is preserved, and no actor is inferred from the linked raw payload. Marker,
value, unit, the reference-range snapshot, flag, lab name, note, date, domain,
source, raw link, and timestamps remain untouched.

`lab_results` has no connection column, so parser provenance is validated on the
raw payload and never copied down. Manual and MCP results require a labs-domain
raw of the same source with no connection, file, or document-parser invocation.
A parsed result accepts exactly three reviewed raw shapes: subject-funded
history behind a same-subject OpenRouter AI-gateway connection with no platform
invocation and no file root; a platform-funded parse whose same-subject
`lab_document` asset has a `storage_ref` equal to the raw `external_id` and
exactly one succeeded `lab_document_parse` invocation; and a fileless raw —
pre-FileAsset history and the shape backup v1 leaves once C/F are stripped —
which is valid history but may not claim a parser invocation. Any invocation on
a referenced raw must belong to the reviewed subject. A rawless result stays
legal for every source; registering that missing document provenance is
Stage-3A and PR-06 work, not a Stage-3M gate.

Initial completion locks gateway roots, raw payloads, and results in canonical
order and rehashes the frozen snapshot. Results stay volatile afterwards, so
later completed status validates the whole current graph.

Backup v1 retains the business fields and `raw_payload_id`, rebinds S, and
strips A. Import resets the exact Stage-3M checkpoint after the Stage-3L reset
and before replacement, records a nonempty snapshot as RUNNING or an empty one
as COMPLETED, and revalidates the restored graph before commit. Recompletion
leaves the unknown actor null.

Stage 3N continues with `stage3.raw_linked_facts.genetic_variants.v1` over
exactly `genetic_variants`. Under a complete manual/MCP/VCF writer pause, a
reviewed fully-null S/A row gains only the sole S. Existing exact S plus a
nullable owner A is preserved, and no actor is inferred from the linked VCF
batch. Gene, rsID, genotype, marker, impact, interpretation, action notes,
domain, source, raw link, and timestamps remain untouched.

A variant has no event date, so the reviewed duplicate gate is its stable rsID:
two variants sharing one non-null rsID fail closed, which is exactly the shape
the later `(S, rsid) WHERE rsid IS NOT NULL` cutover must resolve. The legacy
global partial unique still serializes it today. Manual and MCP variants must
remain rawless; an imported variant must retain a genetics-domain `vcf_import`
raw whose provider-connection and file roots are null, because a VCF upload is
streamed and registers neither. A still fully-unowned raw is valid provenance
for a still-unowned variant, while an adopted variant linking unowned raw
history fails closed. The rsID-membership and payload-revision rules the
genetics reader enforces stay in the domain service.

Backup v1 retains the business fields and `raw_payload_id`, rebinds S, and
strips A. Import resets the exact Stage-3N checkpoint after the Stage-3M reset
and before replacement, records a nonempty snapshot as RUNNING or an empty one
as COMPLETED, and revalidates the restored graph before commit. Migrated
variants keep their unknown actor null; the genetics reader currently reaches
that shape only through its legacy compatibility bridge, so retiring the bridge
is a separate later gate rather than a Stage-3N outcome.

Stage 3O continues with `stage3.file_backed.body_scans.v1` over exactly
`body_scans`. The initial bounded run is performed under a complete body-scan
upload/parse/delete maintenance pause. A reviewed fully-unowned scan gains only
the sole S and, when it kept a sheet, a new metadata-only FileAsset root;
historical A and the placeholder uploader remain null because the old
authenticated route does not prove who uploaded a particular sheet. The existing
`file_key`, device, raw link, note, and timestamps are not changed, read, moved,
or hashed.

Eligible sheet locators carry an optional `uploads/` or `body/` prefix, a safe
POSIX basename, and one of the route's document extensions. Duplicate sheet
keys, duplicate or cross-table file use, unsafe paths, partial ownership, and
non-bijective live FileAsset/scan graphs fail closed. A manual scan may claim
neither file nor raw provenance; a structured MCP scan stays file-free; a parsed
scan's raw payload must present subject-funded gateway history, a platform parse
with one successful invocation and a matching file root, or the fileless shape a
restore leaves behind, and every parser invocation on it must belong to the
reviewed subject.

Because a migrated scan keeps a null actor and a placeholder file root, the
body-scan reader was extended to recognise exactly that reviewed shape. Without
it the historical branches would have rejected their own migrated history, and
a legacy scan that kept a sheet would have stayed unreadable; an unprocessed
tail whose sheet is not registered yet stays legible as well.

Backup v1 carries neither sheet bytes nor trustworthy A/F. Import therefore
records a nonempty Stage-3O snapshot as `RESTORE_BLOCKED`, retires only outgoing
scan assets, preserves the physical files, validates the blocked or empty
incoming shape in the same transaction, and requires backup v2 or an explicit
reviewed recovery before nonempty restored history can be activated. An empty
snapshot is exact `COMPLETED`. `body_scan_metrics` remains deferred until its
own inherited-child phase.

Stage 3P continues with `stage3.inherited_children.body_scan_metrics.v1` over
exactly `body_scan_metrics`. Stage 3O must already be `COMPLETED`. Under a
complete body-scan writer pause, a historical child with a null S gains only the
exact S of its reviewed parent scan; the metric key, printed label, value, unit,
reference range, segment, category, and timestamps are untouched, and the child
never gains an actor.

A child never leads its parent: a metric whose scan is still unowned fails
closed, foreign parent or child ownership fails closed, and a child whose S
disagrees with its scan is an integrity error rather than something to repair.
A metric appended above the frozen high-water mark requires the strict parent
graph. Parents are locked before children in every batch and whole-graph pass,
and the referenced parent digest is rechecked afterwards, so a concurrent scan
adoption cannot slip between validation and the child update.

Because Stage 3O leaves a migrated scan's unknown actor null, the body-scan
reader's manual branch now recognises that shape under the legacy compatibility
bridge; without it a migrated manual scan and every metric under it would have
become unreadable.

Backup v1 carries the child business fields and rebinds the child subject from
the reviewed local root. Import resets the exact Stage-3P checkpoint after the
Stage-3O block and before replacement, records a nonempty snapshot as RUNNING or
an empty one as COMPLETED, and revalidates the restored parent/child graph before
commit.

Stage 3Q continues with `stage3.provider_outbox.garmin_weight_exports.v1` over
exactly `garmin_weight_exports`. Under a complete Garmin export/delete writer
pause, a reviewed fully-null S/C/requester row gains the sole S plus the exact
reviewed legacy Garmin account it was queued for. The requesting actor stays
null, and the date, mass, measurement time, dispatch marker, lifecycle status,
retry counters, remote sample identity, remote ownership flag, error record, and
timestamps are untouched.

The destination is never guessed: it resolves only while the subject has exactly
one Garmin account root and that root is the reviewed `legacy_singleton_v1`
singleton in a historical lifecycle state. Because the gate runs whenever
adoption is still pending, an ambiguous account fails the read-only preflight
rather than the first mutating batch. An owned row without a destination is
half-migrated state and fails closed; a linked weight log must already belong to
the subject because Stage 3L owns every weight fact; and a live row that lost its
weight log is legitimate only in the delete/skip lifecycle states. Two outbox
rows on one date fail closed, which is the duplicate gate the later `(C, date)`
cutover must satisfy, though the legacy global unique still serializes it today.

Backup v1 rebinds S but carries neither the required destination nor the
requester. Import records a nonempty Stage-3Q snapshot as `RESTORE_BLOCKED` after
the Stage-3P reset, validates the S-only incoming shape in the same transaction,
and the fixed operator refuses to advance until a provenance-bearing restore or
an explicit reviewed remap. An empty snapshot is exact `COMPLETED`.

The Stage-3A synthetic PostgreSQL 15 rehearsal passed a real migration build through
revision `0034` and then to head, batch-size-2 process stop/resume, idempotent
completion, byte-stable data/link/frozen-output hashes, and downgrade refusal
before DDL once the checkpoint contained durable state. Stage-3B and Stage-3C
have separate PostgreSQL rehearsals. The Stage-3D PostgreSQL rehearsal covers
the same migration boundary, fixed-catalog stop/resume, restore refusal, and an
actual two-session normalized/raw-link race that fails before ownership or
checkpoint mutation. The Stage-3E rehearsal adds volatile Hevy child rebuilds,
and its PostgreSQL service gate exercises workout-FK, exercise-FK, and child
disappearance races before ownership/checkpoint mutation. The Stage-3F
PostgreSQL rehearsal covers normal and backup-v1 restore dependencies,
custom-versus-curated stop/resume, post-completion catalog churn, and populated
downgrade refusal; its service gate adds parent-classification, component-FK,
consumer-FK, snapshot-key, component-disappearance, and catalog-recategorization
races. The Stage-3H rehearsal covers metadata-only placeholder creation,
actorless historical provenance, stop/resume, backup-v1 blocking, supported
photo deletion, and real upload/delete/file-key races. The Stage-3I rehearsal
covers actorless/channel-less historical adoption, mutable plan/answer
stop/resume and post-completion updates, backup-v1 reset/recompletion, and real
date-row/provenance races. The Stage-3J rehearsal covers optional actor/channel
history, batch/raw invariants, stop/resume, volatile misparse/delete behavior,
backup-v1 reset/recompletion, and recipient/raw/FK races. The Stage-3K rehearsal
covers retained public reports, bounded stop/resume, immutable frozen snapshots,
lifecycle volatility, and backup-v1 checkpoint preservation. The Stage-3L
rehearsal covers linked Garmin and rawless manual history, stop/resume, a strict
live tail above the frozen high-water mark, backup-v1 reset/recompletion, empty
replacement, and populated downgrade refusal; its service gate adds
fact/raw/connection races that fail before ownership or checkpoint mutation. The
Stage-3M rehearsal covers parsed and manual history, stop/resume, a strict live
result above the frozen high-water mark, backup-v1 reset/recompletion, empty
replacement, and populated downgrade refusal; its service gate adds
result/raw/gateway races. The Stage-3N rehearsal covers imported and manual
variant history whose durable VCF link only exists from revision 0037,
stop/resume, a strict live variant above the frozen high-water mark, backup-v1
reset/recompletion, empty replacement, and populated downgrade refusal; its
service gate adds variant/raw races. The Stage-3O rehearsal covers
metadata-only placeholder creation for sheet-backed parser history, stop/resume,
the migrated processed bound, post-completion volatility, backup-v1 blocking and
apply refusal, empty replacement, and populated downgrade refusal; its service
gate adds sheet-key, scan-disappearance, and duplicate-key races. The
Stage-3P rehearsal covers inherited child adoption from a reviewed scan,
stop/resume, a strict live child above the frozen high-water mark, backup-v1
reset/recompletion, empty replacement, and populated downgrade refusal. The
Stage-3Q rehearsal covers destination adoption for a queued outbox, stop/resume,
a strict live delete intent above the frozen high-water mark, backup-v1 blocking
and apply refusal, empty replacement, and populated downgrade refusal. Remaining
artifacts, control phases, and the Stage 4 gates remain pending.

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
