# Commercial Legacy Dual-write Matrix

Status: PR-03 Stage-2 / Stage-3A / Stage-3B / Stage-3C / Stage-3D / Stage-3E / Stage-3F / Stage-3G / Stage-3H / Stage-3I / Stage-3J / Stage-3K / Stage-3L / Stage-3M / Stage-3N / Stage-3O / Stage-3P / Stage-3Q / Stage-3R / Stage-3S implementation source of truth

Last reviewed: 2026-08-21

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
| `weight_logs` | manual/MCP saves, Garmin bridge, body-scan bridge | S always; human writes get A, Garmin facts get Garmin C, and new Body Scan derivations use C null plus their raw-bound platform invocation. Historical subject-C rows remain readable provenance. |
| `body_measurements` | manual/MCP and lean-mass recompute | Human creates get S+A; derived recompute preserves ownership. |
| `progress_photos` | protected upload/weight service | S+A+F; the file asset is registered before the fact row. |
| `noise_markers` | web/MCP | New rows get S+A; delete is S-scoped. |
| `body_scans`, `body_scan_metrics` | upload confirm, structured MCP, raw reparse | Scan ownership comes from the trusted upload/raw boundary; metrics copy S from the scan. |
| `conflict_rules` | curated catalog sync and subject toggle | Curated rows stay global. Subject activation moves to `SubjectSetting` with temporary legacy dual-write. |
| `signals`, `day_context` | Telegram, MCP, evening plan, raw reparse | Telegram facts copy S+A+Telegram C from exact raws; platform-AI recovery copies preserved raw roots, including the exact-one S-only adopted bridge. MCP gets S+A; planned/system rows have A/C null. |
| `garmin_daily`, `garmin_activities`, `garmin_intraday` | scheduler, on-demand sync, HAE import, raw reparse | S+Garmin C required. Human-triggered runs get A; scheduler runs do not. Intraday replacement deletes only inside S+C. |
| `garmin_weight_exports` | Core upsert and outbox lifecycle | S matches the validated Weight subject; C is the distinct Garmin destination. Q records the human requester and stays NULL for scheduler work. Linked Weight provenance keeps its own origin C/raw roots, and lifecycle updates never erase Q. |
| `genetic_variants` | VCF/manual/MCP | Raw-first versioned VCF evidence retains the bounded sample plus curated tail, then S+A facts link to that exact revision. Manual/MCP roots stay immutable; upsert keys are S-scoped. |
| `raw_payloads` | all imports/connectors/uploads/Telegram | Raw ownership is written before normalized rows. Lookup is S/C scoped; refresh preserves historical A and rejects cross-S/C/F conflicts. Telegram keeps the complete current inbound message but reduces nested replied-to/callback bot output to operational IDs so a memory-only AI answer cannot be copied back into durable raw history. |
| `glp1_*` | web/MCP | Human rows get S+A; automatic phase close is S-scoped and preserves A. MCP retains `Source.MCP`. |
| `hevy_workouts`, `hevy_exercises`, `hevy_sets` | sync and raw reparse | Workout gets S+Hevy C; children copy S+C from the parent. Child rebuild/delete is parent-scoped. |
| `hrt_compounds`, `hrt_compound_components` | catalog sync and activation | Curated definitions/components remain global; subject activation is a scoped setting. Future custom rows and components share S. |
| `hrt_doses`, `hrt_side_effects`, `hrt_cycles`, `hrt_cycle_items`, `hrt_cycle_templates`, `hrt_cycle_template_items` | web/MCP/template import/materialization | Human roots get S+A; child rows copy S; referenced compounds must be global or same-S. Automatic closes preserve A. |
| `lab_markers`, `lab_results` | upload/parser, manual/MCP, reparse, hormone seed | S always; human/import parser rows get A, system seed does not. Raw and result S must match. |
| `meal_logs` | web/MCP | S+A and existing MCP provenance. |
| `milestones` | reports web/MCP | Create gets S+A; updates retain A; direct reads/mutations and progress inputs use the selected S. |
| `weekly_digests` | web/MCP/schedulers/brief | S always; human generation gets A, scheduler does not. New weekly and Daily Brief narratives link to a subject-owned `AIInvocation`, set subject C null, and obtain provider/config/quota provenance from the installation-wide platform OpenRouter gateway. Failed/ambiguous/cancelled Daily Brief attempts link a model-null deterministic header to that exact invocation; platform-unavailable headers have both provider roots null. Historical subject-OpenRouter rows remain readable bridge provenance. |
| `notifications` | proactive delivery | S + recipient user + Telegram C; explicit human test actions may get A, scheduled/reply delivery does not infer one. AI parser echoes and question replies additionally link the exact terminal subject invocation. Question answers are never copied into the journal: its payload contains only a bounded redaction marker and raw id. |
| `shared_reports` | create/open/revoke/purge | Create gets S+creator; human revoke gets revoker. Anonymous open and scheduled purge do not mutate actor fields. |
| `skincare_*` | web/MCP/seed scripts | Human creates get S+A; product/log updates preserve A and are S-scoped. |
| `supplements` | web/MCP | Human creates get S+A; updates retain A and MCP retains `Source.MCP`. |
| `system_alerts` | domain services, jobs, web/MCP lifecycle | Health alerts get S; subject-provider alerts get subject C; platform-provider alerts use a separate platform-gateway reference. Human override/resolve uses the named actor field; automatic resolution remains actorless. The scheduled `brief_empty_day` row is S-only. New `signal_parser_failed` rows are S-only/C-null and may link the exact failed or ambiguous platform invocation. Historical subject-C and fully-null rows are resolved through explicit bridges without fabricating missing roots. |

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
| Lab document | S+A+lab F; C null; exact LAB_DOCUMENT_PARSE AIInvocation | Results/markers use raw S/A; F remains on raw. AIInvocation links the subject to the platform OpenRouter gateway and paid-call metadata. Historical subject-C uploads remain dual-read provenance only. |
| Body-scan document | S+A+body F; C null; exact BODY_SCAN_PARSE AIInvocation | Scan uses raw S/A/F; metrics copy S. Derived Weight follows the raw/invocation chain with C null rather than pretending the platform gateway is a subject provider. Historical subject-C uploads remain dual-read provenance only. |
| Structured MCP lab/body input | S+A; C/F null | Normalized rows use the supplied write identity. |
| VCF | S+A; C/F null | Versioned bounded sample + canonical curated tail; facts link to that exact content-addressed raw revision. |

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
the mixed `proactive` object are not copied by a generic bridge. Its specialized
cutover is now explicit: brief/evening times and nudge categories live in
`SubjectSetting('proactive_subject_policy')`; quiet hours and the initiative
budget live on the Telegram recipient C as `proactive_delivery_policy`; Garmin
sync, pulse, and weight-export policy live on the Garmin account C as
`garmin_proactive_policy`. Runtime reads are strict new-only. Startup splits the
legacy/default aggregate while governance proves exactly one S, rejects partial
or drifted state, and only that exact-one bridge may mirror a normalized save to
`AppSetting('proactive')`. Multi-subject reads require the exact active owner
actor and writes never touch the global row. Redis keys must include the
corresponding user/S/C UUID before a second subject exists.

The startup scheduler still consumes the exact-one aggregate. Replacing its
global cron registry with per-subject, timezone-aware due dispatchers remains a
PR-09 gate; the storage split does not claim that dispatcher cutover.

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
or raw rows. New Labs and Body Scan raw rows carry S+A+F, C null, and a raw-bound
platform AIInvocation; historical subject-OpenRouter C+F rows remain validated
dual-read provenance only.
Confirmation locks and
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
malformed or cross-subject raw graphs. Versioned payloads keep the first 50,000
parsed rows plus canonical evidence for every curated tail hit; both collections
participate in the revision hash and replay union. Legacy truncated payloads keep
their explicit compatibility semantics, while v2 membership, overlap, flags,
and partial-root adoption fail closed before mutation. Lossless whole-file
chunking, `(S, rsid)` uniqueness, scoped raw uniqueness, composite raw ownership
FKs, backfill, and whole-lake composition remain required before registration
can open.

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
parser replay validates either the historical S/A+subject-OpenRouter-C+F chain
or the new S/A+F+C-null raw with one exact successful platform Labs invocation.
Local PDF conversion finishes before start/charge. Reservation and charge commit
before the single usage-aware vision call; accounting and the verbatim validated
extraction replace the raw placeholder in one later transaction, and a transient
rollback retries that same paid in-memory completion rather than dispatching
again. Failed/in-flight placeholders are retained for audit but are skipped by
replay and never normalized; malformed head rows cannot starve later eligible
panels. Labs out-of-range/retest
reconciliation and the startup marker seed are
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
selectors. New Body Scan uploads commit exact S+A+F, a C-null raw placeholder,
and one raw-bound platform `BODY_SCAN_PARSE` invocation. Image/PDF preprocessing
finishes before charge; exactly one usage-aware call runs without a database
transaction; and terminal accounting plus the strict verbatim extraction commit
atomically. Confirmation remains a separate editable transaction in canonical
governance -> Garmin advisory -> S/A -> raw/F/invocation -> scan/metrics ->
derived Weight/outbox order. Direct reads, notes/deletes, conflict evaluation,
passive alerts, and replay validate that exact graph or historical subject-C
provenance and reject mixed roots. Retiring the source file denies document
access without invalidating the independent retained Weight fact. The legacy
bridge admits only a fully NULL historical raw root and keeps that provenance
explicit when normalizing subject-owned facts. MCP keeps Source.MCP on both raw
and scan, cannot claim a platform parser invocation, and its linked derived
Weight correctly retains Source.BODY_SCAN. The global active-weight/body-
measurement/outbox date keys, BodyScan composite-FK/backfill and raw-key
cutovers, and unscoped chart, share, digest, overview, and export consumers
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
processed, and a recovery pass can replay a parked action after rollback. During
the current bridge, new Daily Brief generation uses platform `AIInvocation`
provenance and never writes a subject OpenRouter C; historical C-backed rows
remain readable. The Notification separately carries the delivery C. Dedupe and
daily budget remain stable across channel rotation, while the same logical key
may be used by different subjects without collision. New owned messages now use
a durable `NotificationDeliveryIntent`: T1 commits a payload-free PENDING claim;
T2 locks and revalidates S, recipient Q, the current Telegram C, module policy,
quiet hours, and initiative budget before committing DISPATCHING; the network
call has no database transaction; and T3 atomically marks SENT and inserts the
exact linked Notification. Transport uncertainty is terminal AMBIGUOUS and is
never retried. Stale reconciliation performs no provider I/O, and PENDING,
DISPATCHING, SENT, and AMBIGUOUS all count conservatively for budget/cooldown.
Scoped owned and fully-null legacy journal indexes replace the global dedupe
index, while raw/category uniqueness prevents alternate keys from sending the
same reply or echo twice.

Generic reconciliation cancels only stale non-raw claims. A stale raw-backed
reply/echo PENDING claim—or a stale cancellation whose lifecycle proves no
dispatch began—may be re-armed on the same row after exact graph validation.
Commands reconstruct their fixed response, Signals reconstruct the current
raw-linked echo, and a question whose in-memory answer was lost uses the fixed
redacted fallback without another OpenRouter call. DISPATCHING, SENT,
AMBIGUOUS, and policy-cancelled claims are never re-opened.

The intent carries no text, buttons, recipient address, credential, or free-form
error. Ordinary Notification journals still contain the sent brief/nudge/echo
content, so the wider PHI-free-notification exit criterion is not yet claimed.
The current environment Telegram token/private recipient can be bound only while
the exact-one legacy bridge proves S/Q/C; a durable per-recipient credential
mapping and callback/edit/withdrawal mutation intents remain PR-09 gates.

The live signal parser and scheduled recovery now reserve a raw-bound
`AIInvocation` against the installation-wide platform gateway. T1 locks
governance -> S/owner -> Telegram C -> raw, freezes the exact health day and
bounded prompt, then reserves quota; T2 freshly revalidates and charges. Both
transactions commit before exactly one usage-aware provider call. T3 re-locks
the roots and atomically finalizes accounting plus Signal normalization and the
raw terminal marker. Failed or ambiguous calls retain the raw for at most three
attempts; keyset recovery scans past exhausted, in-flight, malformed, and
oversized head rows so later messages cannot starve. A fully-null historical raw
may gain only S under the exact-one bridge, preserving unknown A/C/F, and partial
or reverse ownership fails closed. New `signal_parser_failed` alerts are
S-only/C-null, use an opaque per-S entity key, and may link the exact failed
invocation. Historical subject-C and fully-null alerts are only resolved, never
rewritten with invented roots.
Parser echoes carry the invocation link and revalidate the logical Telegram
message before both delivery transactions; a concurrently superseded echo is
suppressed, while a post-send edit is best-effort neutralized. Their raw-bound
delivery intent prevents another new-message send. A scoped replacement for the
global active-alert unique index and durable Telegram edit intent remain PR-09
registration blockers.

Telegram questions use the same installation gateway with a distinct
`QUESTION_REPLY` invocation bound to the exact raw, human owner A, and TELEGRAM
source. T1 atomically marks the raw as classified and reserves the sole lifetime
attempt; T2 commits the conservative charge before one usage-aware completion;
T3 finalizes accounting before delivery. Prompts and completions never enter the
ledger, Notification, durable raw history, logs, or generic audit metadata.
Before a later Telegram reply is stored raw, any nested replied-to/callback bot
message is reduced to operational message/chat identifiers so Telegram cannot
copy the prior answer back into persistence. The in-memory answer is
repr-redacted and non-pickleable, and the journal stores only
`content_redacted=true`, the raw id, and the optional exact invocation FK.
Configuration/quota failures send the same bounded fallback without an
invocation, while failed, ambiguous, and pre-dispatch-cancelled attempts retain
their exact terminal linkage. Recovery queries every unjournaled invocation
independently of an opaque raw-scan cursor, so a busy Telegram history cannot
strand a paid result. Delivery rechecks the module, current owner/recipient C,
and immutable edit ordering before dispatch. The exact raw/category intent makes
the new-message provider call at-most-once and journals the terminal invocation
without storing the answer text. If the source is edited after dispatch,
withdrawal remains a best-effort Telegram mutation; durable edit/withdrawal
reconciliation is still a PR-09 blocker.

The scheduled morning brief freezes its exact-S compatibility context and
reserves platform quota, commits authorization/charge before exactly one
OpenRouter call, then finalizes sanitized accounting and its invocation-linked
artifact in a fresh transaction. Missing configuration/quota yields an
invocation-null deterministic header; provider failure yields a header linked to
the exact conservatively charged terminal invocation. Delivery uses the shared
durable intent protocol, so policy and dispatch state commit before Telegram and
the journal is linked atomically afterward; concurrent workers cannot produce a
second provider call. Isolated actorless S-only
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

Weekly Digest is the first platform-funded AI consumer. Web, MCP, and scheduler
boundaries freeze the exact-one compatibility snapshot and reserve quota in a
short transaction, commit a second authorization/charge transaction before the
single provider await, then finalize sanitized accounting plus the artifact in
one fresh transaction. Failed, ambiguous, or policy-cancelled calls advance only
through three versioned attempt slots; concurrent starts still obtain one lease.
Product status is resolved before comparing mutable gateway roots, quota periods,
or reservation ceilings: a succeeded artifact and an in-flight paid call remain
idempotent across rotation, while an incompatible PREPARED reservation is
released before the next attempt is reserved.
Daily Brief uses a separate immutable product-key namespace derived only from
surface, report date, and the bounded opaque form token (scheduler uses no
token). Mutable model and prompt-policy versions remain provenance/fingerprint
inputs, never product identity: an existing terminal row keeps its resolved
model, while a PREPARED model mismatch is cancelled to a header and cannot
create a replacement invocation.
Configuration failure before dispatch releases the reservation when authority
still permits it, and the platform reconciliation job releases PREPARED rows
older than 15 minutes while marking hour-old paid DISPATCHING rows ambiguous.
Backup v1 excludes `weekly_digests` because it cannot transport either legacy C
or platform-invocation provenance safely; it preserves live rows in place, while
the separate curated LLM export may still include narrative text.

The platform AI settings boundary now creates and version-rotates the global
gateway, preserves a disabled root across ordinary configuration saves, and
configures one platform period plus one exactly aligned opaque-S period in a
single caller-owned transaction. Its read model contains only gateway
status/version, opaque S identifiers, dates, limits, and counters; it never joins
subject profiles or generated artifacts. Every production OpenRouter call now
enters the platform invocation gateway. Historical subject OpenRouter roots stay
readable only as validated provenance; compatibility-only non-network adapters
do not weaken the platform kill switch or quota boundary.

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

## Stage-3A raw-ownership operation

Stage 2 makes new `RawPayload` writes explicit; Stage 3A handles only historical
rows below a frozen stable-PK high watermark. The phase is permanently named
`stage3.raw_payloads.v1`. Its service resolves the exact bootstrapped subject and
reviewed legacy connection mapping under identity governance, rejects partial or
foreign ownership, ambiguous provider mapping, duplicate natural-key candidates,
and checkpoint drift, and never invents a historical actor or file root.
An actorless historical provider/parser row may receive C only from the exact
same-subject `legacy_singleton_v1` provider/type root (including a retired root
that remains historical provenance); rotated/current accounts are never guessed.
The bridge is never accepted as authority for live ingress, whose strict
S/A/C-or-artifact dual-write contract remains unchanged.

`scripts/backfill_subject_ownership.py` is a thin operator boundary. With no
arguments it runs read-only status and the complete fail-closed preflight.
Mutation requires `--apply`; batch size is 1–1000 (default 250), `--max-batches`
is 1–100 (default 1), and each batch plus checkpoint commits independently. The
command accepts no table, phase, reset/delete, or DB-URL argument. It performs no
payload-file or provider I/O and never resolves or uses credentials.

The service result retains internal scan watermarks for resume validation, but
the CLI applies a narrower JSON allowlist: phase/status, cumulative and final-
batch counts, remaining/above-high-watermark counts, completion/result codes,
and three lowercase SHA-256 checksums. It emits no subject/checkpoint/raw ID,
payload, medical value, title, medical/event date, path, credential reference,
DB URL, or exception text. Revision `0045` is schema-only; once any durable
checkpoint row exists, its downgrade refuses before DDL. All normalized, child,
artifact, delivery, alert/outbox, file, and setting phases remain separate
pending slices.

Every mutating invocation belongs to one continuous raw-writer maintenance
window: ingest, refresh, replay, and import stay paused until the phase completes.
The final transition locks and keyset-rehashes the complete frozen snapshot with
the requested batch size as its page bound. Cardinality, payload, or ownership
drift between committed batches fails closed; Stage-3A intentionally exposes no
operator reset/rebase that could erase that evidence.

The throwaway PostgreSQL 15 rehearsal passed a real migration build through
revision `0034` and then to head, batch-size-2 process stop/resume, idempotent
completion, unchanged data/link/frozen-output hashes, and populated-checkpoint
downgrade refusal before DDL.

A v1 full restore does not import checkpoint state. A non-empty restore has
discarded A/C/F provenance and atomically records `stage3.raw_payloads.v1` as
`RESTORE_BLOCKED`; ordinary apply cannot remap or unblock it. Future backup-v2 or
reviewed manual recovery must provide the missing graph. An empty restore records
an empty `COMPLETED` checkpoint. Replacement refuses before mutation if retained
AI-invocation or durable-delivery rows still reference raw history. No checkpoint
state authorizes S-only restored rows.

## Stage-3B normalized-manual ownership operation

The next fixed operator phase is `stage3.normalized_manual.v1`. It covers only
the 17 reviewed top-level tables whose old rows need S, may retain historical
`A=NULL`, and do not require C/F/raw reconstruction. HRT parent rows, annotations,
body measurements, GLP-1 facts, HRT dose/effect facts, lab markers, meals,
milestones, noise markers, skincare facts/products, and supplements form a
closed compile-time catalog; provider/raw-sensitive, file-backed, mixed-catalog,
child, report, notification, alert, outbox, and setting rows stay in later
phases.

`scripts/backfill_normalized_subject_ownership.py` defaults to read-only status.
`--apply`, `--batch-size`, and `--max-batches` have the same bounds and
commit-per-batch semantics as Stage 3A, but there is no caller-selectable table
or phase. Every table has a separate deterministic checkpoint because its PK
stream has an independent watermark. The CLI aggregates only fixed table names,
counts, result codes, and checksum chains; S/A IDs, row IDs, cursors, dates,
titles, notes, values, and exceptions never cross the operator boundary.

Stage 3A completion is a hard dependency. Historical `(S=NULL,A=NULL)` becomes
`(S=legacy,A=NULL)`; exact historical S-only and current S+owner rows remain
unchanged. Partial or foreign roots, unreviewed domain/source values, future-key
duplicates, foreign HRT child/compound roots, and non-dual-written rows above a
watermark fail closed. LabMarker is the only live actorless exception because
the reviewed system seed writes an exact subject row without a source column.
No operation fabricates A, changes source/domain, invokes a domain service,
reconciles alerts, or updates medical values. The backfill explicitly preserves
`updated_at` and finalizes a table only after bounded PK-ordered rehashing proves
the complete frozen data and ownership chains.

Backup v1 replaces these portable rows after trusted S rebinding while omitting
unprovable historical A. The import transaction therefore resets exactly the
fixed Stage-3B table checkpoints to the incoming ID/count snapshots; empty
tables complete immediately and nonempty tables require the operator to rebuild
their evidence. This private reset is not exposed by the CLI and does not apply
to provider/raw/file phases. All covered writers stay paused across the entire
multi-batch maintenance window.

## Stage-3C inherited HRT-child ownership operation

The fixed `stage3.inherited_children.hrt.v1` phase covers only
`hrt_cycle_items` and `hrt_cycle_template_items`. Ordinary preflight/apply
requires Stage 3A and every Stage-3B table checkpoint to be `COMPLETED`. A
historical child with null S copies the exact S of its reviewed cycle/template
parent; exact-S history is unchanged, while missing, foreign, or malformed
parent/child graphs fail closed. No actor, connection, file, raw link, compound
ownership, or medical value is inferred or rewritten.

An unowned custom compound is a transitional legacy reference only for a child
inside the frozen snapshot. A child above the watermark must point to an exact
same-subject custom compound (or an intact checked-in global system compound).
Stage 3C therefore closes the child-S bridge; strict conflict resolution for a
historical unowned custom compound remains gated on the later mixed-catalog
phase.

`scripts/backfill_hrt_child_subject_ownership.py` defaults to read-only status
and exposes only `--apply`, bounded `--batch-size`, and bounded
`--max-batches`. The two tables have independent fixed checkpoint keys and are
processed in parent-group order. Finalization uses PK-ordered locks and verifies
both data and ownership checksums for the complete group; completed status and
repeat apply continue to reject ownership drift while allowing later legitimate
business edits.

Backup v1 replaces both portable child tables and rebinds their S. In the same
transaction, after raw and Stage-3B checkpoint handling, it resets exactly these
two Stage-3C checkpoints to incoming ID/count snapshots; empty tables complete
immediately. The reset is private and cannot authorize other children. Body
scan metrics, Hevy exercise/set children, and HRT compound components remain
deferred to phases that can validate their raw/file/provider or mixed-catalog
parents.

## Stage-3D provider/raw-linked ownership operation

The fixed `stage3.provider_raw_linked.v1` phase covers `garmin_daily`,
`garmin_activities`, `garmin_intraday`, and `hevy_workouts`. It requires
completed raw, Stage-3B, and Stage-3C checkpoints and assigns only the exact S/C
already proven by each normalized row's reviewed raw link. Nullable historical A
is validated independently and never rewritten. Exact domain/source, raw
external IDs and payload keys, provider/account connection roots, and future
scoped natural keys fail closed on mismatch; intraday intentionally has no
invented sample uniqueness.

One frozen-history exception preserves actual old behavior: an HAE daily row may
retain a same-date Garmin API raw link. The reverse mismatch and all live-tail
cross-source links are invalid. Hevy exercise/set roots are validation-only in
this phase; `(NULL,NULL)`, `(parent S,NULL)` from backup v1, and exact parent S/C
are transitional reviewed shapes, while partial or foreign roots fail.

`scripts/backfill_provider_raw_subject_ownership.py` has the same read-only
default, bounded status/apply controls, independent table checkpoints, no-PHI
JSON projection, and commit-per-batch contract as prior phases. The complete
provider writer set remains paused through final locked rehash. Because normal
Garmin intraday sync replaces whole series, later completed-status checks
validate all current rows but do not require old frozen sample IDs to remain.

Backup v1 preserves raw links but strips both raw and normalized C. Import
therefore atomically records a non-empty provider table as `RESTORE_BLOCKED`
after the prior phase resets and before replacement; ordinary apply cannot guess
or clear it. Empty tables record `COMPLETED`. Future backup v2 or an explicit
reviewed remap must provide the missing connection provenance.

## Stage-3E Hevy inherited-child ownership operation

The fixed `stage3.inherited_children.hevy.v1` phase covers exactly
`hevy_exercises` followed by `hevy_sets`. Exercises inherit S/C only from an
exact reviewed Stage-3D workout/raw/Hevy-account graph. Sets inherit only from
an exact exercise after the exercise checkpoint completes, and independently
prove the same workout S/C chain. Frozen history permits only `(NULL,NULL)`, the
backup-v1 `(parent S,NULL)` bridge, or exact parent S/C; live tail rows must
already be exact. No actor, file, raw link, or child business key is invented.

Both child tables are replaced wholesale by owned Hevy refreshes. Operators
therefore pause every Hevy sync/reparse/import writer through completion of both
checkpoints. Initial finalization locks and rehashes the frozen snapshots;
subsequent completed status validates every current child graph without
requiring deleted historical child IDs to survive.

`scripts/backfill_hevy_child_subject_ownership.py` is read-only by default and
has only bounded apply controls. Its JSON projection contains fixed phase,
status, table, count, checksum, and result fields—never child IDs, parent/raw
keys, UUIDs, workout content, measurements, errors, or database configuration.
Backup v1 strips C while rebinding child S, so import atomically records each
non-empty Stage-3E table as `RESTORE_BLOCKED` after the Stage-3D transition;
empty tables complete. There is no Stage-3E remap/reset operator.

## Stage-3F HRT mixed-catalog ownership operation

The fixed `stage3.mixed_catalog.hrt.v1` phase covers `hrt_compounds` followed
by `hrt_compound_components`. A global catalog parent is valid only with exact
`domain=hrt`, `source=system`, null S/A, a checked-in key, exact catalog-owned
scalars, and the complete YAML component multiset with null child S. A custom
parent is valid only with `source=manual|mcp`, `domain=hrt`, and a non-curated
key. Frozen fully-unowned custom parents gain only the sole S; existing nullable
or owner A is preserved. Live-tail custom rows require exact S+A. A custom
component inherits only the locked parent's S, while a catalog component stays
global. The phase never creates A/C/F/raw provenance or rewrites medical data.

Linked HRT doses and cycle items are locked validation inputs: their
`compound_key` snapshot must match the linked parent, and a custom parent must
share their exact subject. Key-only template items remain free-text history and
are not ownership evidence. Catalog synchronization is paused for the complete
multi-batch window and now refuses any custom/partial row occupying a checked-in
key before changing catalog data.

Initial finalization locks and rehashes both complete snapshots. Durable
post-completion ownership evidence intentionally covers only frozen custom
parents/components; current system rows are instead revalidated against the
current YAML. This permits checked-in catalog/component reseeds while still
detecting custom deletion, reparenting, reclassification, or S/A drift. Normal
apply requires exact completed Stage 3A–3E evidence. Backup-v1 restore may use
the explicit reset-created Stage-3F group with the prior phases' exact restore
states because source/key and the subject-bound marker preserve this phase's
classification; nonempty snapshots resume as RUNNING and empty snapshots
complete. This checkpoint is migration evidence, not authorization for global
HRT reads, catalog activation, sharing, or custom CRUD.

`scripts/backfill_hrt_compound_subject_ownership.py` is read-only by default
and exposes only bounded `--apply`, `--batch-size`, and `--max-batches`
controls. Its versioned JSON contains fixed phase/table counts, result codes,
and checksums only—never compound/component IDs, keys, names, doses, routes,
UUIDs, timestamps, exception text, or database configuration.

Every full-graph validation, live-tail scan, consumer check, and final rehash is
PK-keyset paged; the batch limit therefore bounds materialized rows as well as
mutations. Persisted checkpoint validators require equal before/after data
digests and exact dependency/group state pairs. Custom keys must match the
canonical lowercase slug contract, and catalog synchronization takes ordered
row locks before collision validation or mutation.

## Stage-3G conflict-rule mixed-catalog ownership operation

The fixed `stage3.mixed_catalog.conflict_rules.v1` phase covers only
`conflict_rules`. Curated rows remain global and must exactly match every
catalog-owned YAML field; `active` remains the legacy subject toggle and is not
provenance. A frozen custom row is reviewed only when both S and code are null,
then receives the sole S without changing any rule data or timestamp. Existing
custom codes must be nonblank and outside the current catalog. Above the HWM,
curated rows must already be exact global definitions and custom rows must
already carry exact S.

The initial snapshot is locked and fully rehashed. Post-completion evidence
retains the frozen custom ownership subset while the current global catalog is
revalidated directly, allowing catalog reseeds without masking custom history
loss. The YAML synchronizer shares identity governance with the backfill, locks
matching rules in ID order, preserves `active`, and rejects any subject-owned
catalog-code collision before mutation. Backup v1 preserves the subject-bound
marker and row IDs, so import resets the one checkpoint to RUNNING for a
nonempty snapshot or exact COMPLETED for an empty one. The fixed-target operator
is read-only by default and exposes only bounded apply controls and allowlisted
non-PHI counts/checksums. Stage 3G does not remove the fully-unowned activation
bridge or scope every conflict consumer and alert path.

## Stage-3H progress-photo file ownership operation

The fixed `stage3.file_backed.progress_photos.v1` phase covers only
`progress_photos`; FileAsset rows are generated ownership roots, not a second
scanned checkpoint table. Operators pause every upload, delete, import, and
other progress-photo writer through initial completion. A frozen row is eligible
only in the fully-null S/A/F shape with exact Weight/manual provenance and a
safe root-level `uploads/` image key. It gains S plus a fresh `legacy_local`,
`legacy_placeholder`, progress-photo FileAsset whose uploader and content
metadata remain null. Historical A remains null. No byte is read, moved, hashed,
deleted, or assumed to exist.

Processed actorless history is accepted only inside the validated RUNNING or
COMPLETED checkpoint prefix and only with an exact same-subject S+F graph.
Above the HWM, the existing strict owner A/FileAsset-uploader shape is mandatory.
Duplicate keys, duplicate or raw/body-shared F, partial or foreign roots,
unsafe keys, `uploads/labs/` and `uploads/body/` aliases, conflicting metadata,
and any unlinked live progress-photo asset fail closed. Initial finalization
locks and hashes the full graph. Later completed checks deliberately validate
the current bijection: supported deletion retires the metadata root before
removing the fact, so deleted historical IDs need not remain, but deleting a
fact while leaving a live orphan asset is rejected.

Backup v1 strips A/F and does not contain medical file bytes. Its nonempty
Stage-3H checkpoint is therefore `RESTORE_BLOCKED`; import rebinds required S,
creates no placeholder, and validates that the inaccessible restored rows have
the exact blocked shape. Empty snapshots complete. The same transaction retires
only outgoing assets referenced by replaced photos and never unlinks bytes.
There is no ordinary apply path out of `RESTORE_BLOCKED`; backup v2 or a reviewed
recovery must provide file provenance.

## Stage-3I day-context channel-optional ownership operation

The fixed `stage3.channel_optional.day_context.v1` phase covers only
`day_context`. A frozen row is eligible for adoption only when S/A/C are all
null; it gains the sole S without changing answers, planned context, source,
domain, dates, or timestamps. Existing owned rows must already have exact S,
nullable-or-owner A, and either null C or an exact same-subject historical
Telegram recipient connection. The operation never infers A or C from source,
content, or the presence of a sole active recipient.

Operators pause plan, Telegram-answer, MCP-answer, import, and direct
day-context writers through initial completion. The current global date key is
validated as the one-row-per-date legacy invariant. Initial finalization locks
and hashes the complete frozen snapshot. Day context is intentionally mutable:
later plans and answers overwrite the row and may legitimately update data,
timestamps, source, A, and optional C. Completed status therefore revalidates
every current row without comparing the frozen data or ownership digests, while
still retaining the frozen IDs and cardinality as migration evidence.

Backup v1 strips A/C but preserves the row content and subject-bound marker.
Import therefore rebinds S and resets the one checkpoint to RUNNING for a
nonempty snapshot or exact COMPLETED for an empty snapshot, after Stage 3H is
blocked/reset and before portable rows are replaced. Recompletion preserves the
unknown historical A/C as null. The fixed-target operator is read-only by
default, exposes only bounded apply controls, and emits allowlisted aggregate
counts and checksums without dates, answers, IDs, UUIDs, timestamps, exception
text, or database configuration. This phase does not replace the global
`UNIQUE(date)` key or finish the reader/constraint cutover.

## Stage-3J signal channel-optional ownership operation

The fixed `stage3.channel_optional.signals.v1` phase covers only `signals`.
A frozen row is eligible for adoption only when S/A/C are all null; it gains the
sole S without changing the signal fact, batch, raw link, provenance, or
timestamps. Existing owned rows retain nullable-or-owner A and nullable-or-exact
historical Telegram-recipient C. MCP rows are raw/channel-neutral. Historical
Telegram rows may retain an exact same-subject Signals/Telegram raw link;
above-HWM live Telegram rows require the complete owner, recipient, and raw
graph. The only actorless above-HWM exception is a late reparse from an exact
S+C/A-null Telegram raw row already covered by the validated Stage-3A HWM; the
fact must retain that exact raw and recipient. The service also rejects split
batch provenance and a raw message divided across multiple normalization batches.

Operators pause Telegram/MCP ingest, pending-raw reparse, misparse, delete,
import, and direct signal writers through initial completion. Recipient roots
are locked before raw payloads and facts; projected roots and FKs are rechecked
after locking. Initial finalization hashes the frozen data and ownership graph.
Completed status is intentionally current-graph based because supported
misparse, deletion, reparse, and new ingest operations make the table volatile.

Backup v1 preserves signal content and `raw_id` but strips A/C. Import rebinds S
and resets the exact checkpoint after Stage 3I and before replacement: nonempty
snapshots become RUNNING and empty snapshots exact COMPLETED. Recompletion
preserves unknown historical A/C as null. The fixed operator is read-only by
default, exposes only bounded apply controls, and emits aggregate allowlisted
JSON without signal text, keys, batch/raw IDs, dates, UUIDs, exception text, or
database configuration. Consumer-bridge and scoped reader retirement remain a
later gate.

## Stage-3K retained shared-report ownership operation

The fixed `stage3.retained_artifact.shared_reports.v1` phase covers only
`shared_reports`. A frozen fully-null S/creator/revoker row gains the sole S and
nothing else. Historical creator/revoker gaps remain null; non-null actors must
be the subject owner and a revocation actor requires a revocation timestamp.
Every token, password hash, frozen snapshot, report field, lifecycle value,
counter, and timestamp is preserved through the ownership mutation.

The checkpoint-aware owner and public-token boundary accepts fully-null rows only
in the unprocessed frozen tail while RUNNING. Exact-S historical actor shapes are
valid anywhere at or below the snapshot HWM, and rows above that HWM require the
strict live S+creator graph. After completion, anonymous opens, owner revocation,
snapshot purge, deletion, and strict new reports are validated as current-graph
volatility; the initial digest remains migration evidence rather than a permanent
business-data snapshot.

Backup v1 excludes `shared_reports` from both export and replacement. Import
therefore prepares or preserves the retained Stage-3K checkpoint after Stage 3J
reset, never trusts incoming report bounds, and revalidates the retained graph
after portable rows are replaced. The fixed operator remains read-only by
default and emits only allowlisted aggregate counts and checksums without report
IDs, tokens, hashes, titles, snapshots, dates, actors, or exception text.

## Stage-3L optional-channel weight ownership operation

The fixed `stage3.channel_optional.weight_logs.v1` phase covers only
`weight_logs`. A reviewed fully-null S/A/C row gains the sole S and nothing else.
Existing exact S with a nullable owner actor and a nullable same-subject Garmin
account or OpenRouter AI-gateway connection is preserved; neither optional root
is inferred from `source`, from a sole current connection, or from the raw
payload the fact already links. Mass, note, supersession, date, domain, source,
raw link, and both timestamps are untouched by the migration.

Manual and MCP facts may not claim a provider connection, and their optional
historical raw must be a weight-domain row of the same source. Garmin facts must
reference a Garmin-domain `garmin_api` raw, and body-scan facts a
body-composition raw written either by the vision parser or by structured MCP.
A raw C is Stage-3A/3D evidence rather than a Stage-3L requirement: backup v1
strips it and records those phases as restore-blocked, so a still-unowned or
connection-stripped raw remains valid provenance for a historical fact while an
adopted fact may never point at fully-unowned raw history. Parser invocations
are validated for subject equality and for subject-versus-platform exclusivity,
never re-created.

Initial completion locks provider roots, raw payloads, and facts in canonical
order and rehashes the frozen data and ownership snapshot. Weights stay volatile
afterwards, so completed status validates the current graph — new manual, MCP,
Garmin, and body-scan writes, supersession, note corrections, and deletions
remain legal. The phase also proves that exactly one active weight exists per
date, which is the duplicate gate the later `(S, date) WHERE superseded = false`
cutover has to satisfy.

Backup v1 preserves the weight business fields and `raw_payload_id`, rebinds S,
and strips optional A/C. Import resets the exact Stage-3L checkpoint after the
retained Stage-3K preparation and before replacement to RUNNING for a nonempty
snapshot or exact COMPLETED for an empty snapshot, then revalidates the restored
graph before commit. Recompletion never fabricates the stripped actor or
connection. The fixed operator is read-only by default, commits one bounded
batch per transaction, and emits only allowlisted aggregate counts and checksums
without weight values, notes, dates, row/raw IDs, UUIDs, exception text, or
database configuration.

## Stage-3M raw-linked lab-result ownership operation

The fixed `stage3.raw_linked_facts.lab_results.v1` phase covers only
`lab_results`. A reviewed fully-null S/A row gains the sole S and nothing else.
Existing exact S with a nullable owner actor is preserved, and no actor is
inferred from the raw payload the result links. Marker, value, unit, the
reference-range snapshot, flag, lab name, note, date, domain, source, raw link,
and both timestamps are untouched.

The table has no connection column of its own, so parser provenance is validated
where it lives — on the raw payload — and never copied down. Manual and MCP
results require a labs-domain raw of the same source with no connection, file, or
document-parser invocation. A parsed result accepts three reviewed raw shapes:
subject-funded history, where a same-subject OpenRouter AI-gateway connection
paid for the parse and no platform invocation or file root exists; a
platform-funded parse, where a same-subject `lab_document` asset whose
`storage_ref` equals the raw `external_id` is accompanied by exactly one
succeeded `lab_document_parse` invocation; and a fileless raw — pre-FileAsset
history, and the shape backup v1 leaves once C/F are stripped — which is accepted
as history but may not claim a parser invocation. Every invocation found on a
referenced raw must belong to the reviewed subject. A rawless result stays legal
for every source, because the writer accepts a panel typed in by hand and older
parses predate the raw-first boundary.

Initial completion locks gateway roots, raw payloads, and results in canonical
order and rehashes the frozen data and ownership snapshot. Results stay volatile
afterwards, so completed status validates the current graph rather than the
original cardinality or digest.

Backup v1 preserves the result business fields and `raw_payload_id`, rebinds S,
and strips the actor. Import resets the exact Stage-3M checkpoint after the
Stage-3L reset and before replacement to RUNNING for a nonempty snapshot or
exact COMPLETED for an empty snapshot, then revalidates the restored graph
before commit. The fixed operator is read-only by default, commits one bounded
batch per transaction, and emits only allowlisted aggregate counts and checksums
without markers, values, lab names, notes, dates, row/raw IDs, UUIDs, exception
text, or database configuration.

## Stage-3N raw-linked genetic-variant ownership operation

The fixed `stage3.raw_linked_facts.genetic_variants.v1` phase covers only
`genetic_variants`. A reviewed fully-null S/A row gains the sole S and nothing
else. Existing exact S with a nullable owner actor is preserved, and no actor is
inferred from the VCF batch a variant links. Gene, rsID, genotype, marker,
impact, impact domain, interpretation, action notes, domain, source, raw link,
and both timestamps are untouched.

A variant is a lifelong fact with no event date, so the reviewed duplicate gate
is its stable rsID rather than a date: two variants sharing one non-null rsID
fail closed, which is the shape the later `(S, rsid) WHERE rsid IS NOT NULL`
cutover must resolve. Manual and MCP variants must remain rawless. An imported
variant must retain a genetics-domain `vcf_import` raw, and that raw must have
null provider-connection and file roots because a VCF upload is streamed and
registers neither. A still fully-unowned raw remains valid provenance for a
still-unowned variant — the shape backup v1 leaves before Stage 3A runs again —
while an adopted variant may never point at unowned raw history. The rsID
membership and payload-revision rules the genetics reader enforces stay in the
domain service; this phase owns ownership and subject equality.

Initial completion locks referenced roots, raw payloads, and variants in
canonical order and rehashes the frozen data and ownership snapshot. Variants
stay volatile afterwards, so completed status validates the current graph.

Backup v1 preserves the variant business fields and `raw_payload_id`, rebinds S,
and strips the actor. Import resets the exact Stage-3N checkpoint after the
Stage-3M reset and before replacement to RUNNING for a nonempty snapshot or
exact COMPLETED for an empty snapshot, then revalidates the restored graph
before commit. Migrated variants keep an unknown actor null, which the genetics
reader still reaches through its legacy compatibility bridge; retiring that
bridge remains a later gate. The fixed operator is read-only by default, commits
one bounded batch per transaction, and emits only allowlisted aggregate counts
and checksums without genes, rsIDs, genotypes, interpretations, row/raw IDs,
UUIDs, exception text, or database configuration.

## Stage-3O file-backed body-scan ownership operation

The fixed `stage3.file_backed.body_scans.v1` phase covers only `body_scans`. A
reviewed fully-unowned scan gains the sole S and, when it kept a sheet, one new
metadata-only FileAsset root; historical A and the placeholder uploader stay null
because the old authenticated route does not prove who uploaded a particular
sheet. The existing `file_key`, device, raw link, note, and both timestamps are
not changed, and no byte is read, moved, hashed, or tested for existence.

Only reviewed sheet locators are eligible: an optional `uploads/` or `body/`
directory prefix, a safe POSIX basename, and the route's own document extension
allowlist. Duplicate sheet keys, duplicate or cross-table file use, unsafe paths,
partial ownership, and non-bijective live FileAsset/scan graphs fail closed. A
manual scan may claim neither file nor raw provenance; a structured MCP scan
stays file-free; a parsed scan's vision provenance is validated read-only on its
raw payload, where subject-funded gateway history, a platform parse with one
successful invocation and a matching file root, and a fileless or restored raw
are the three reviewed shapes. Parser invocations must belong to the subject.

Because a migrated scan keeps a null actor and a placeholder file root, the
body-scan reader now recognises exactly that shape — same subject,
`body_scan_document` purpose, legacy-local backend, `storage_ref` equal to
`file_key`, null uploader, `legacy_placeholder` status, not retired. Without it
the historical branches would reject their own migrated history. An unprocessed
tail whose sheet is not registered yet remains legible too.

Backup v1 carries neither sheet bytes nor trustworthy A/F. Import therefore
records a nonempty Stage-3O snapshot as `RESTORE_BLOCKED`, retires only outgoing
scan assets while preserving the physical files, validates the blocked or empty
incoming shape in the same transaction, and requires backup v2 or an explicit
reviewed recovery before nonempty restored history can be activated. An empty
snapshot is exact `COMPLETED`.

## Stage-3P inherited body-scan metric ownership operation

The fixed `stage3.inherited_children.body_scan_metrics.v1` phase covers only
`body_scan_metrics`. A metric is a directly queried child, so it carries its own
subject even though that subject is reachable through the scan. A historical
child with a null S gains only the exact S of its reviewed Stage-3O parent; the
metric key, printed label, value, unit, reference range, segment, category, and
both timestamps are untouched, and the child never gains an actor.

A child never leads its parent: a metric whose scan is still unowned fails
closed, a foreign parent or child subject fails closed, and a child whose S
disagrees with its scan is an integrity error rather than something to repair.
A metric written above the frozen high-water mark requires the strict parent
graph. Parents are locked before children in every batch and in every whole-graph
pass, so a concurrent scan adoption cannot slip between validation and the child
update, and the referenced parent digest is rechecked afterwards.

Because Stage 3O leaves a migrated scan's unknown actor null, the body-scan
reader now also accepts that shape on the manual branch under the legacy
compatibility bridge; without it a migrated manual scan and all of its metrics
would have become unreadable.

Backup v1 carries the child business fields and rebinds the child subject from
the reviewed local root. Import resets the exact Stage-3P checkpoint after the
Stage-3O block and before replacement to RUNNING for a nonempty snapshot or
exact COMPLETED for an empty snapshot, then revalidates the restored parent/child
graph before commit.

## Stage-3Q Garmin weight-outbox ownership operation

The fixed `stage3.provider_outbox.garmin_weight_exports.v1` phase covers only
`garmin_weight_exports`. The outbox is destination state rather than health
history, so every row needs both the sole reviewed subject and the Garmin
account it was queued for. A reviewed fully-null S/C/requester row therefore
gains S plus the exact reviewed legacy Garmin account root, and nothing else:
the requesting actor stays null, and the local date, mass, measurement time,
dispatch marker, lifecycle status, retry counters, remote sample identity,
remote ownership flag, error record, and both timestamps are untouched.

The destination is never guessed. It resolves only while the subject has exactly
one Garmin account root and that root is the reviewed `legacy_singleton_v1`
singleton in a historical lifecycle state; a missing, rotated, or additional
account fails closed. Because that gate runs whenever adoption is still pending,
an ambiguous account surfaces in the read-only preflight rather than at the first
mutating batch. An owned row without a destination is half-migrated state and
fails closed too, and a linked weight log must already belong to the subject
because Stage 3L owns every weight fact. A live row that lost its weight log is
accepted only in the delete/skip lifecycle states where that is legitimate. The
phase also refuses two outbox rows on one date, which is the duplicate gate the
later `(C, date)` cutover must satisfy; the legacy global unique still serializes
it today.

Backup v1 rebinds S but cannot carry the required destination or the requester.
Import therefore records a nonempty Stage-3Q snapshot as `RESTORE_BLOCKED` after
the Stage-3P reset, validates the S-only incoming shape in the same transaction,
and the operator command refuses to advance until a provenance-bearing restore
or an explicit reviewed remap. An empty snapshot is exact `COMPLETED`.

## Stage-3R retained weekly-digest ownership operation

The fixed `stage3.retained_artifact.weekly_digests.v1` phase covers only
`weekly_digests`. A digest is a generated artifact, so its narrative, the context
it was built from, the model that wrote it, and its funding provenance are
evidence the migration must not touch. A reviewed fully-null S/A/C row gains the
sole S and nothing else; the authoring actor, the historical subject OpenRouter
connection, and the platform invocation link are never inferred.

Subject-funded and platform-funded provenance are mutually exclusive, which the
schema check constraint also states. A retained subject gateway connection must
be the subject's own OpenRouter AI gateway in a historical lifecycle state and
may not coexist with an invocation. A linked platform invocation must belong to
the subject, carry the purpose its digest kind implies, and have succeeded. Above
the frozen watermark every artifact must carry one of those two reviewed funding
roots, because nothing generates a digest without paying for it.

Backup v1 neither exports nor replaces digests, so this phase is retained rather
than rebased: import prepares the checkpoint from the local artifact set on the
first restore and afterwards revalidates and preserves it, never accepting
incoming bounds. The fixed operator is read-only by default, commits one bounded
batch per transaction, and emits only allowlisted aggregate counts and checksums
without narratives, context, dates, row IDs, UUIDs, or exception text.

## Stage-3S retained notification ownership operation

The fixed `stage3.delivery_artifact.notifications.v1` phase covers only
`notifications`. A delivered message is addressed state: it only means something
together with the person it went to and the channel that carried it, which is
also what the schema's dedupe-shape constraint says. A reviewed fully-null
S/A/R/C row therefore gains the sole subject, the reviewed owner as recipient,
and the exact reviewed legacy Telegram recipient root together — and nothing
else. The originating actor stays null, and the sent time, category, dedupe key,
channel, external message id, and payload are untouched.

The recipient root is never guessed: it resolves only while the subject has
exactly one Telegram recipient connection and that connection is the reviewed
`legacy_singleton_v1` singleton in a historical lifecycle state. Because that
gate runs whenever adoption is still pending, an ambiguous root surfaces in the
read-only preflight. An owned row missing either the recipient or the channel is
half-migrated state and fails closed. A linked delivery intent must agree with
the message on subject, recipient, and channel; a linked platform invocation may
only belong to a reply or echo, must belong to the subject, and must have
succeeded.

`notifications` is retained rather than portable. Backup v1 transports neither
the recipient nor the delivery connection, so a restored address-less row would
violate the reviewed dedupe shape and resurrect dedupe keys that no longer scope
to anything. Import therefore prepares the checkpoint from the local delivery log
on a first restore and afterwards revalidates and preserves it, never accepting
incoming bounds. `delivery_intent_id` also joins the suppressed plumbing columns
so no generic MCP, LLM, or backup surface can expose or replay it.

## Completion gates

- Every production constructor, Core insert/upsert, and bulk update has a reviewed
  ownership call site; a static inventory test fails when a new path appears.
- One create test covers each ownership-bearing table and actor/channel policy.
- Raw and normalized S match; required provider C matches; direct children copy
  parent S/C; cross-subject repair is rejected.
- Backup v1 rebinds S where its portable marker proves subject scope and leaves
  actors null. It derives child roots only when the retained parent graph still
  proves them; it never guesses a stripped provider C or file F. Non-empty
  provider/Hevy-child snapshots whose provenance was stripped are recorded as
  `RESTORE_BLOCKED` pending a provenance-bearing format or reviewed remap, while
  the known scoped settings are mirrored atomically.
- Scripts that perform global delete/import either require the sole legacy
  context and scope their work or fail closed.
- Fast SQLite tests and a real PostgreSQL 15 migration/concurrency suite pass.
- Registration and all paths to a second writable subject remain disabled.
