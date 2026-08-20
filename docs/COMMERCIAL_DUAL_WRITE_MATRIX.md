# Commercial Legacy Dual-write Matrix

Status: PR-03 Stage-2 / Stage-3A implementation source of truth

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
