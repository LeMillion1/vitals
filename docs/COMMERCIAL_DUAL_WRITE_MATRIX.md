# Commercial Legacy Dual-write Matrix

Status: PR-03 Stage-2 implementation source of truth

Last reviewed: 2026-08-20

This document records every compatibility write boundary that must populate the
nullable ownership columns introduced by revisions `0037` and `0038`. It is the
runtime companion to `COMMERCIAL_OWNERSHIP_INVENTORY.md`; that inventory owns the
schema target, while this file owns how new writes reach it before registration
or multi-subject reads are enabled.

## Boundary contract

- Resolve the sole legacy health subject and active owner once at a transaction
  boundary. A human web action must match the authenticated normalized username.
- Pass an immutable `WriteIdentity(subject_id, actor_user_id)` into domain write
  services. A background operation uses `actor_user_id = NULL`; a missing actor
  is never interpreted inside a service as "probably the owner".
- Resolve only the integration roots required by the operation. A live
  network/ingest operation requires a `legacy` or `active` connection;
  `pending`, `disabled`, `retired`, missing, or ambiguous roots fail closed.
  Historical reparse/read provenance may retain `disabled` or `retired` roots,
  but never turns their lifecycle state back into permission for new activity.
- `Source` remains ingestion provenance and never substitutes for subject, actor,
  connection, file, relationship, or consent identity.
- MCP v1 remains a legacy installation-wide capability. Its direct Timeline,
  Supplements, Signals, DayContext, and provider-sync tools resolve the
  configured owner at the database boundary so writes are attributed and a
  second subject fails closed. Whole-lake MCP composition/export stays blocked
  for the later AccessContext cutover. This mapping is not subject authorization
  and must not be presented as the MCP `2026-07-28` OAuth/principal model.
- A legacy row with `subject_id IS NULL` may be attached to the sole legacy
  subject during a reviewed reconcile. A row already attached to another subject
  is never reassigned. Historical actor fields remain unchanged.
- Domain services receive explicit values. ORM autofill hooks are forbidden:
  they cannot safely classify global catalogs, Core SQL, provider identity,
  inherited children, files, or lifecycle actors.

## Write-path matrix

| Tables | Runtime writers | Stage-2 ownership rule |
| --- | --- | --- |
| `annotations` | timeline web/service and MCP event/note tools | New human rows get S+A; updates retain A and require the same S. MCP creates retain `Source.MCP`. |
| `weight_logs` | manual/MCP saves, Garmin bridge, body-scan bridge | S always; human writes get A, provider writes get provider C, derived writes retain source ownership. |
| `body_measurements` | manual/MCP and lean-mass recompute | Human creates get S+A; derived recompute preserves ownership. |
| `progress_photos` | protected upload/weight service | S+A+F; the file asset is registered before the fact row. |
| `noise_markers` | web/MCP | New rows get S+A; delete is S-scoped. |
| `body_scans`, `body_scan_metrics` | upload confirm, structured MCP, raw reparse | Scan ownership comes from the trusted upload/raw boundary; metrics copy S from the scan. |
| `conflict_rules` | curated catalog sync and subject toggle | Curated rows stay global. Subject activation moves to `SubjectSetting` with temporary legacy dual-write. |
| `signals`, `day_context` | Telegram, MCP, evening plan, raw reparse | Telegram facts get S+A+Telegram C; MCP gets S+A; planned/system rows have A/C null; reparse copies raw ownership. |
| `garmin_daily`, `garmin_activities`, `garmin_intraday` | scheduler, on-demand sync, HAE import, raw reparse | S+Garmin C required. Human-triggered runs get A; scheduler runs do not. Intraday replacement deletes only inside S+C. |
| `garmin_weight_exports` | Core upsert and outbox lifecycle | S matches the validated Weight subject; C is the distinct Garmin destination. Q records the human requester and stays NULL for scheduler work. Linked Weight provenance keeps its own origin C/raw roots, and lifecycle updates never erase Q. |
| `genetic_variants` | VCF/manual/MCP | Raw-first, then S+A and raw link on interpreted variants. Upsert keys are S-scoped. |
| `raw_payloads` | all imports/connectors/uploads/Telegram | Raw ownership is written before normalized rows. Lookup is S/C scoped; refresh preserves historical A and rejects cross-S/C/F conflicts. |
| `glp1_*` | web/MCP | Human rows get S+A; automatic phase close is S-scoped and preserves A. MCP retains `Source.MCP`. |
| `hevy_workouts`, `hevy_exercises`, `hevy_sets` | sync and raw reparse | Workout gets S+Hevy C; children copy S+C from the parent. Child rebuild/delete is parent-scoped. |
| `hrt_compounds`, `hrt_compound_components` | catalog sync and activation | Curated definitions/components remain global; subject activation is a scoped setting. Future custom rows and components share S. |
| `hrt_doses`, `hrt_side_effects`, `hrt_cycles`, `hrt_cycle_items`, `hrt_cycle_templates`, `hrt_cycle_template_items` | web/MCP/template import/materialization | Human roots get S+A; child rows copy S; referenced compounds must be global or same-S. Automatic closes preserve A. |
| `lab_markers`, `lab_results` | upload/parser, manual/MCP, reparse, hormone seed | S always; human/import parser rows get A, system seed does not. Raw and result S must match. |
| `meal_logs` | web/MCP | S+A and existing MCP provenance. |
| `milestones` | reports web/MCP | Create gets S+A; updates retain A; direct reads/mutations and progress inputs use the selected S. |
| `weekly_digests` | web/MCP/schedulers/brief | S always; human generation gets A, scheduler does not. OpenRouter C is set only when that provider actually produced content. |
| `notifications` | proactive delivery | S + recipient user + Telegram C; explicit human test actions may get A, scheduled/reply delivery does not infer one. |
| `shared_reports` | create/open/revoke/purge | Create gets S+creator; human revoke gets revoker. Anonymous open and scheduled purge do not mutate actor fields. |
| `skincare_*` | web/MCP/seed scripts | Human creates get S+A; product/log updates preserve A and are S-scoped. |
| `supplements` | web/MCP | Human creates get S+A; updates retain A and MCP retains `Source.MCP`. |
| `system_alerts` | domain services, jobs, web/MCP lifecycle | Health alerts get S; provider alerts also get C. Human override/resolve uses the named actor field; automatic resolution remains actorless. The scheduled `brief_empty_day` row is S-only. `signal_parser_failed` uses S plus the exact OpenRouter AI-gateway C, never Telegram C. Both adopt only their exact-key fully-null legacy row through the exact-one bridge. |

Direct MCP note updates to weight, meals, GLP-1, skincare, body measurements,
body scans, and labs must go through owned services or perform an explicit
same-subject assertion. A bare primary key is never a write authority.

The HRT web and direct MCP boundaries now apply the table contract above to
doses, side effects, cycles, child items, templates, import/materialization,
reads, edits, and deletes. Dose safety evaluation replaces the exact edited fact;
automatic reminder alerts use S with a null actor. Curated compound definitions
remain global and catalog-authenticated. Per-subject compound activation remains
pending and the scoped service refuses to mutate the global `active` flag.

## Raw-first provider matrix

| Origin | Raw S/A/C/F | Normalized inheritance |
| --- | --- | --- |
| Garmin API / Health Auto Export | S; optional triggering A; Garmin C; no F | Daily/activity/intraday copy S+A+C. |
| Hevy | S; optional triggering A; Hevy C; no F | Workout copies S+A+C; exercises/sets copy S+C. |
| Telegram | S+A+Telegram C; no F | Signals/day context copy the raw ownership. |
| Lab document | S+A+OpenRouter C+lab F when AI parsing ran | Results/markers use raw S/A; F remains on raw. |
| Body-scan document | S+A+OpenRouter C+body F when AI parsing ran | Scan uses raw S/A/F; metrics copy S. |
| Structured MCP lab/body input | S+A; C/F null | Normalized rows use the supplied write identity. |
| VCF | S+A; C/F null | Curated variants link to that raw row. |

Upload confirmation must load the raw/file rows by S and must not trust a client
pair of IDs or a client-provided storage key. VCF, backup JSON, Garmin HAE import,
and HRT template JSON are parse-only inputs and are not `FileAsset` objects.

## Scoped setting migration

The first reviewed mappings are deliberately small:

- `ui_language` -> `UserSetting`;
- `enabled_modules`, `custom_charts`, `week_template` -> `SubjectSetting`;
- `garmin_weight_export_enabled` -> Garmin `IntegrationConnectionSetting`
  with exact-connection reads and temporary exact-one legacy dual-write.

Reads are new-first with legacy fallback. Writes update both rows in one caller-
owned transaction. `twofa_secret`, credentials, token material, unknown keys, and
the mixed `proactive` object are not copied by a generic bridge. The proactive
object must first be split into subject, Telegram-connection, and Garmin-
connection fields. Redis keys must include the corresponding user/S/C UUID before
a second subject exists.

The language, module-toggle, custom-chart, week-template, and Garmin Weight export
product paths now
use that bridge. Week-template partial MCP updates use a locked atomic transform.
Authenticated web chrome resolves the sole owner once per request; writes use an
atomic locked transform for JSON collections and prime only UUID-namespaced
Redis entries after commit. Anonymous compatibility pages may still read the
legacy installation value while registration is closed. A strict Garmin scope
never falls back to the installation setting, and a retired historical connection
can be disabled without mutating the current installation-wide compatibility row.

## File transition

`FileAsset` registration is metadata-only and must not read or move bytes. The
legacy relative paths are limited to:

- `uploads/` for progress photos;
- `labs/` for lab documents;
- `body/` for body-scan documents.

Registration records a safe relative path, purpose, optional uploader, already-
known media type/size/SHA-256, and `legacy_placeholder` lifecycle. Repeated
registration is idempotent only for the same subject, purpose, and compatible
metadata. Delete/purge transitions update lifecycle timestamps; they do not hard-
delete the ownership root.

The Stage-2 upload slice now registers progress photos, lab documents, and body-
scan documents in the same caller-owned database transaction as their normalized
or raw rows. Lab/body raw rows carry S+A+OpenRouter C+F; confirmation locks and
validates the S -> raw -> F chain and derives the storage reference from the
server-side asset. Subject-scoped delete paths retire the asset before removing
legacy-local bytes. A failure before COMMIT rolls metadata back and removes the
new bytes; an exception or cancellation while COMMIT is in flight preserves the
bytes because the database outcome is ambiguous and requires reconciliation.

The protected legacy download route authorizes through the resolved subject and
honors deleted/purged asset state. Progress-photo paths additionally require a
reachable validated ProgressPhoto fact: exact rows prove S+A+F, purpose, uploader,
backend, lifecycle, and storage key, while pre-backfill history is accepted only
when the photo's S/A/F roots are all NULL. The broader asset-missing compatibility
behavior for lab/body documents remains separate cutover work. Opaque asset URLs,
complete file backfill, and binary-aware portability remain required before the
file contract can become non-null.

Genetics VCF, manual, and MCP boundaries now resolve the verified legacy owner,
write S+A, and scope direct reads/mutations by S. VCF imports persist a
content-addressed bounded raw revision first with C/F null, then link new
VCF-origin normalized rows to that raw; re-import replaces parser fields and
clears stale conflict markers while later human/MCP corrections retain the
original actor/source/raw roots. Pending replay handles partially normalized
batches, never rolls a newer fact back to older pending evidence, and rejects
malformed or cross-subject raw graphs. The current payload keeps the first 50,000
parsed rows plus a truncation flag, so lossless chunking, `(S, rsid)` uniqueness,
scoped raw uniqueness, composite raw ownership FKs, backfill, and whole-lake
composition remain required before registration can open.

`system_alerts` now has typed health, provider, and platform contexts, an
exhaustive key/domain registry, and a fail-closed exact-one legacy bridge.
Generic web/MCP lifecycle actions aggregate the selected subject's health alerts
with current and retired provider roots while excluding platform maintenance
alerts. Garmin/Hevy operational alerts and scheduler failures dual-write their
reviewed S/C scope. Upload-adjacent and several other health-domain writers still
use the legacy alert API, and the global unresolved-alert unique key is not yet
replaced. The global lab-marker name key and active-weight-per-date key also still
prevent a second writable subject. Registration must remain disabled until those
writer and scoped-key gates land.

Timeline annotation and Supplements catalog CRUD now resolve the authenticated
legacy owner at web/MCP transaction boundaries, stamp S+A on creates, retain the
original A on updates, and scope direct reads/mutations by S. The Timeline feed
also scopes every derived event selector when a subject is supplied. Pre-backfill
NULL rows are included only through an explicit compatibility flag after the
sole-subject resolver succeeds. Cross-domain composition readers in Today,
digest/report/share assembly, weight-chart overlays, custom-chart metric catalog/
series resolution, and whole-lake MCP exports still await the PR-04/PR-10
AccessContext cutover. Conflict-rule reads now select one subject and evaluation
date across all seven resolver domains; curated definitions remain global,
subject/custom rows are exact-S, and ambiguous or partial legacy roots fail
closed. Curated activation now lives in the subject setting with a temporary
exact-one legacy mirror, so UI, MCP, and evaluation share one effective state.
The scoped writer core binds an opaque capability to the validated identity,
session, transaction/savepoint, and evaluation date. Supplements create, update,
and activation use that capability; updates lock and refresh the target and
exclude exactly that row from the resolver snapshot before applying the proposed
state. Nutrition web/MCP create and update paths use the same proof, evaluate the
post-write subject-day aggregate, and preserve meal-level name predicates. Meal
deletion joins the subject-lock order, direct MCP Nutrition reads are scoped, and
the day-end job reconciles actorless subject alerts. Skincare checklist replacement
uses a subject-day marker so old actives do not false-block a corrected routine;
its observations and personal product catalog stamp S+A, and direct web/MCP reads,
notes, and deletes are subject-scoped. GLP-1 injections, dose phases, and side
effects use the same prepared boundary; a phase replaces the active subject-day
resolver item before evaluation and can only close an eligible phase in that
subject. Plateau reconciliation consumes subject-scoped phase/Weight/noise reads
and writes an actorless health alert. Labs manual, MCP, and upload-confirmation
writes now use the prepared boundary before marker/result/raw/file locks. Direct
Labs CRUD and notes are exact-S, the fully-NULL bridge rejects partial roots,
single and batch MCP writes persist Source.MCP raw rows before normalization, and
parser replay validates S/A plus the historical OpenRouter C and LAB_DOCUMENT F
chain. Labs out-of-range/retest reconciliation and the startup marker seed are
actorless subject actions. Labs chart/share/digest/overview/export consumers still
belong to the subject-aware composition cutover and cannot serve a second writable
subject. Direct WeightLog web/MCP create, edit, note, and delete paths now use the
selected subject and prepared conflict proof. Garmin daily ingestion, owned
replay, and body-scan-derived weights reuse a Weight capability acquired in the
canonical governance -> active-weight advisory -> subject/domain-row -> alert
order; provider facts require an exact matching raw/connection chain. A delete or
date move evaluates the historical replacement before promotion and leaves an
unsafe candidate superseded. The Garmin Weight outbox now validates the exact
S+destination-C scope, preserves human Q while scheduler projections keep Q NULL,
uses the scoped opt-in, and rejects partial/foreign outbox or linked-Weight roots
before a remote mutation. Durable leases are followed by fresh lifecycle/root
validation before network activity; unavailable Garmin export never blocks the
local health write. Direct BodyMeasurement and NoiseMarker web/MCP reads and
mutations now validate the selected subject before target reads, preserve the
original actor/source on correction, and allow only a fully NULL S/A legacy row
through the exact-one bridge. Measurement writes use the scoped conflict proof;
noise reconciliation writes an actorless health alert. The authenticated Weight
page propagates S through Weight, measurement, noise, GLP-1, Timeline, and BIA
selectors. BodyScan upload confirmation, MCP raw-first ingest, direct reads,
notes/deletes, conflict evaluation, derived Weight projection, passive alerts,
and nightly replay now validate one exact S/A/C/F/raw/metric graph. The legacy
bridge admits only a fully NULL historical raw root and keeps that provenance
explicit when normalizing subject-owned facts. MCP keeps Source.MCP on both raw
and scan, while the linked derived Weight correctly retains Source.BODY_SCAN. The global
active-weight/body-measurement/outbox date keys, BodyScan composite-FK/backfill and
raw-key cutovers, and unscoped chart, share, digest, overview, and export consumers
remain explicit release blockers.
Other domain writers and transitional legacy fallbacks stay on the reviewed
inventory, so registration and every path to a second writable subject remain
disabled until those cutovers land.

`scripts/seed_demo.py` also remains an installation-wide destructive developer
utility: it deletes and recreates domain rows without S/A. It must fail closed on
a commercial identity database or be rewritten around an explicit disposable
demo subject before registration can open; it is not a supported Stage-2 write
boundary.

The older `scripts/seed_skincare.py` utility now fails closed as soon as a
commercial `HealthSubject` exists; it cannot globally erase and rebuild real
Skincare history after identity bootstrap.

Signals and proactive delivery now use a channel-neutral ownership context.
Telegram capture writes S+A+Telegram C, MCP writes S+A with C null, and planned
day context stays actorless. Existing Signal, DayContext, Notification, and
brief rows with fully NULL roots are visible only after the exact-one-subject
resolver enables the compatibility flag; partial-root rows are rejected. A
callback is durably parked before its action, successful callbacks are marked
processed, and a recovery pass can replay a parked action after rollback. Brief
narrative provenance uses the OpenRouter C only when an LLM tail was produced;
the Notification separately carries the delivery C. Dedupe and daily budget
remain stable across channel rotation, while a key already owned by another
subject fails before network delivery. The global notification unique index is
still a concurrency/cutover blocker until its scoped replacement lands. Inbound
normalization and callback mutations recover from the durable raw update, but an
immediate reply/echo remains best-effort: PR-09 must persist an outbound intent
with pending/sent/ambiguous states before commercial registration can open.

The live signal parser freezes the exact OpenRouter AI-gateway C separately from
the Telegram recipient C, validates it fresh, and closes the raw/ownership read
transaction before the adapter await. Recovery performs nonlocking Telegram
validation and completes edit/classification work before each parser await, then
revalidates the canonical S -> historical Telegram C -> raw chain before
normalization. Durable raw/Signal terminal state commits before a fresh
governance -> S -> OpenRouter C -> alert-key/row transaction reconciles the
actorless `signal_parser_failed` warning. Any attempted failure or junk result in
a recovery batch raises; otherwise a success or explicit-empty result resolves,
including a same-subject validated historical OpenRouter C after rotation.
Skipped non-parser raws do not change alert state. Signals composition and the
durable outbound-intent cutover remain deferred. Historical-C selection still
relies on the single global active `(alert_key, entity_ref)` slot; replacing that
index with scoped partial uniques remains a registration blocker.

The scheduled morning brief freezes its exact-one legacy compatibility
context, closes that read transaction before OpenRouter, persists the rendered
digest, and ends delivery-policy reads before Telegram. A successful send is
journalled in a fresh caller-owned transaction. Isolated actorless S-only
`brief_empty_day` reconciliation then follows canonical governance -> S -> key/row
order. Empty outcomes use the same alert phase without calling either provider.

SharedReport owner actions use a transaction-bound exact-one proof before any
whole-lake compatibility read or report-row query. Create writes S+creator;
list/get/download/delete select exact S plus fully-null historical roots, while a
human revoke may attach only that fully-null row to S, preserves an unknown NULL
creator, and records the revoker. A non-null creator or revoker must be the
subject owner. Public token resolution takes no caller S, validates the stored
S/actor graph, and maps corrupt, revoked, expired, purged, and missing rows to the
same response. A successful anonymous open re-locks the live token before its
counter update; scheduled purge changes only expired snapshots. Neither infers
an actor. Bcrypt verification holds no database transaction, while unlocked HTML
is rendered under governance so concurrent revoke cannot authorize one stale
page. The access cookie binds report id plus a token fingerprint. Snapshot bytes
and rendered output contain no ownership identifiers. Underlying report assembly
is still an exact-one whole-lake compatibility read pending PR-10; this slice does
not claim subject-aware composition.

Garmin and Hevy runtime ingestion now resolves S plus the provider C before
network persistence, copies raw provenance into normalized parents and children,
and rejects cross-S/C refresh, ambiguous legacy adoption, and invalid lifecycle
state. Subject->connection lock order is shared across sync and reparse paths.
Garmin auth/token/weight-export alerts, Hevy sync alerts, and scheduler provider failures now
use exact provider roots; scheduler subject and platform jobs are classified by
an exhaustive registry. Global provider credentials, Redis namespaces, Garmin
Weight outbox/date uniqueness, upstream natural-key uniqueness, and the read transaction
spanning vendor I/O remain PR-09/cutover work; registration therefore remains
disabled.

## Completion gates

- Every production constructor, Core insert/upsert, and bulk update has a reviewed
  ownership call site; a static inventory test fails when a new path appears.
- One create test covers each ownership-bearing table and actor/channel policy.
- Raw and normalized S match; required provider C matches; direct children copy
  parent S/C; cross-subject repair is rejected.
- Backup v1 rebinds S, derives child S/C, maps required legacy connections,
  creates safe file placeholders, leaves actors null, and mirrors known scoped
  settings atomically.
- Scripts that perform global delete/import either require the sole legacy
  context and scope their work or fail closed.
- Fast SQLite tests and a real PostgreSQL 15 migration/concurrency suite pass.
- Registration and all paths to a second writable subject remain disabled.
