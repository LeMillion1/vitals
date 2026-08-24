# Commercial Multi-user Roadmap

Status: active design and implementation plan

Last reviewed: 2026-08-24

Current implementation branch: `commercial/main`

Commercial base: current `origin/master`; publish it as a separate branch in
`LeMillion1/vitals` instead of rewriting the fork's historical `master`

This document is the durable hand-off for turning Vitals from a single-user,
self-hosted application into a commercial multi-user service. It records the PR
sequence, security invariants, migration strategy, test gates, and decisions
that must survive across implementation sessions.

The existing fork's six divergent commits are patch-equivalent to changes that
already exist upstream. New commercial work therefore starts from the current
upstream head. The fork's `master` branch is not force-updated or otherwise
rewritten.

## Where this stands today (2026-08-24)

The numbers and states below are measured, not remembered. Re-derive them rather
than trusting them if this date has gone stale.

| | |
| --- | --- |
| Branch / remote | `commercial/main` on `fork` (`LeMillion1/vitals`) |
| Alembic head | `0064` — 64 revisions |
| Schema | 76 tables; 62 carry `subject_id` and are covered by an RLS policy; 52 have it `NOT NULL` |
| Backfill | 18 phases in `OWNERSHIP_BACKFILL_SEQUENCE`, all with a script in the runbook |
| Suites | 4561 fast passed / 168 skipped; 35 browser scenarios; 2011 on PostgreSQL |
| Domains / scheduled jobs | 14 and 14, of which 11 fan out per record |

**Merged:** PR-01 identity, PR-02 bootstrap and `AccessContext`, PR-03 ownership
expansion and backfill, PR-04 scoped services + policy engine + FORCE RLS,
PR-05 OIDC, provisioning and the registration decision, PR-06
files/portability/settings, PR-07 professionals/relationships/consent, PR-08
professional UX (minus the inbox), PR-09 minus its notification transport,
PR-11 care-team messaging (minus private attachments), PR-12's read-only support
access, PR-10.

**The next gate is PR-10**, and it has started at the end that does not wait
on an external SDK. Its external API now authenticates against per-subject
credentials rather than one installation-wide string that resolved to the `.env`
owner — the last such read on a data path. Its authorization gate is in too, which is the
half the roadmap deliberately separates: the connector token has always carried
the authorizing account and the tools always ignored it, so a token any
signed-in account could obtain read and wrote the `.env` owner's record. One
seam — a request-scoped actor the six `_mcp_v1_*` helpers ask — now decides
whose record a call reaches. LLM context isolation is in as well, as proof
rather than as new machinery: `assemble_context` already
took a mandatory subject and gated every optional domain, and seven contract
tests now compose for one person beside another and search the serialized prompt
for anything of theirs — including their name, which is asserted absent because
that is how it would be lost. The wire-protocol migration is in too: `mcp==2.0.0`
replaced FastMCP, the transport is stateless, authentication moved into the
SDK's token verifier, and six tests drive the endpoint with the SDK's own
client. The conformance detail is done and mostly
turned out to be the SDK's: `server/discover`, per-request version negotiation,
`resultType`, server identity in `_meta` and private `cacheScope` all hold, with
tests. The OAuth profile is in as well: client
metadata documents replace Dynamic Client Registration, their fetching is
treated as hostile input, and the authorization response carries `iss`. Token binding is in as well: the
connector token carries `sub`, `aud`, `iss` and `jti`, and revision `0064` gives
it a revocation store, so one connector can be disconnected without rotating the
signing secret and signing the whole installation out. **PR-10 is complete.** Revision `0062` adds the ask as its own table because the grant's
constraints, correctly, cannot express a pending one. `repair`, `export`,
operational dashboards, retention controls and the break-glass path are named
and not built; each needs its own review and the roadmap already sequences them
after the read path.

**Three gaps are decisions rather than unbuilt scope**, and each is named
where it lives: private attachments on a care-team message (the download route
still resolves through the sole-owner adapter, so a professional gets a 404 that
is not about permission); the professional inbox from PR-08 (finding an
invitation by email is a different security model, not a missing screen); and
`import_full`, whose wipe is still unqualified — though not reachable while it
matters: both it and `export_full` refuse a database holding more than one
subject, so the unqualified delete only ever runs where there is exactly one
record for it to replace. What is missing is the multi-subject backup format,
not a guard.

**One defect is named and not fixed.** `labs_service.normalize_marker`
promises to standardize casing, and does so only for the 62 names in
`MARKER_ALIASES`; everything else falls through to "upper-case the first
character, keep the rest". So `TSH`, `tsh` and `tSh` are three markers — three
rows in Latest values, three charts, three histories — and the lab form takes
free text, so a person reaches this by typing. It is not fixed here because the
fix re-keys stored clinical data: changing the fallback splits every existing
installation's history at the moment of the change unless a migration re-keys
the rows first, and re-keying makes two spellings collide under
`uq_lab_markers_subject_name`, which needs a merge policy rather than a rename.
Normalizer, migration and collision policy are one piece of work, not a
one-line fix.

**Two behaviours will look like bugs if you don't know they were chosen:**

1. **There is no notification transport.** Telegram was removed outright (see the
   decision log); web push has not landed. The proactive layer composes a brief
   on schedule and stores it in `/reports`, and every send resolves to "no
   endpoint", which every caller already handles as an ordinary answer.
   `channels.resolve_legacy_bound_notifier` returns `None` and is the seam a
   per-subject push subscription plugs into.
2. **A new subject has no body on file, and that is the correct state.** The
   profile moved out of `.env` into `health_profile_service`, so the report has
   its five fields back for whoever filled them in — and a subject who has not
   gets nulls rather than somebody's default. The Navy body-fat estimate is
   skipped for them rather than computed from half a profile. That is not a
   placeholder and needs no further work; it is what an empty field in a medical
   document is supposed to mean.

**The `.env` plan is done.** `.env` should hold only what belongs to the
installation (database, Redis, session secret, identity provider, AI gateway,
endpoints, and now the credential-vault key); all of a person's settings belong
in the database. Delivered: the timezone (was already a column, now read *and*
written — `/settings/profile` had been writing `VITALS_TIMEZONE`, which nothing
reads, so changing it did nothing), the proactive schedule, (3) the profile,
goals and nutrition targets, and (4) per-subject Garmin and Hevy credentials in
`integration_credentials`, encrypted under `VITALS_CREDENTIAL_KEY`.

What is left in `.env` about a person is the authentication that is still one
username and one password hash, and that is PR-05's.

## Outcome

The service must support additive identities and roles:

- a member who owns and manages a health subject;
- a doctor who can also own a personal subject and can observe assigned subjects;
- a trainer who can also own a personal subject and can observe assigned subjects;
- a `platform_superadmin` who can operate the service, but has no standing access
  to health data merely because of that role.

Doctors and trainers receive access only through an active care relationship and
an unexpired, unrevoked consent grant for a concrete subject, domain, and action.
The safe first communication feature is a patient-visible care-team thread. A
hidden doctor-to-trainer channel is out of scope until a separate product,
privacy, and legal decision permits it.

## Non-negotiable invariants

1. **Actor and subject are different concepts.** Every protected operation knows
   who acts, whose data is affected, the authorization basis, and the channel.
2. **Roles are additive and do not grant patient access.** A doctor or trainer
   role is an identity attribute. A relationship plus consent grants access.
3. **No standing superadmin PHI access.** Operational administration is separate
   from a short-lived support session for a particular subject.
4. **Deny by default across every surface.** Web, service, MCP, scheduler,
   connector, upload, report, export, and background delivery paths use the same
   policy contract.
5. **Object lookup is scoped.** A known numeric or UUID identifier must never be
   enough to read, update, delete, attach, export, or share another subject's row.
6. **Consent revocation is immediate.** It invalidates cached policy decisions,
   subject-specific MCP/PAT grants, queued work, open professional views, and
   future file access.
7. **Provenance is preserved.** `Source` remains the ingestion channel. Actor,
   subject, relationship, consent, connector, and support-session identifiers are
   stored separately.
8. **Raw-first ingestion remains intact.** Raw payload ownership and connector
   identity are established before normalization.
9. **PostgreSQL is the isolation truth.** Explicit application filters are
   mandatory; PostgreSQL RLS, composite keys, and constraints provide a second
   boundary rather than replacing application authorization.
10. **Secrets and PHI stay out of logs and generic audit JSON.** Audit events keep
    identifiers, actions, policy basis, and bounded operational context only.
11. **Registration remains closed until isolation is complete.** A registration
    page is not a safe milestone by itself.
12. **Migrations use expand/backfill/cutover/contract.** No release depends on a
    one-shot destructive conversion, and no existing health history is dropped.
13. **The paid AI gateway is platform control-plane state, not patient
    ownership.** Only an active `platform_superadmin` may configure OpenRouter,
    while every invocation still requires ordinary subject authorization and
    records its subject, actor/system origin, purpose, model, usage, and exact
    platform gateway. Paying for AI never grants the administrator PHI access.
14. **Authentication primitives come from a standards-based IdP.** Vitals does
    not become a password, recovery, email-verification, TOTP, or passkey
    implementation. An external OIDC provider proves the principal; Vitals maps
    the immutable `(issuer, subject)` pair to a local user and remains the sole
    authority for subject selection, roles, relationships, consent, support
    grants, sessions, and every PHI decision.

## Target domain model

| Concept | Purpose |
| --- | --- |
| `User` | Stable local principal, display/login projection, status, and session version. The legacy bcrypt hash remains only during the bounded compatibility cutover. |
| `ExternalIdentity` | Immutable OIDC `(issuer, subject)` binding plus bounded verification/authentication metadata. It stores no password, MFA seed, recovery code, or provider token. |
| `UserRole` | Additive `member`, `doctor`, `trainer`, and `platform_superadmin` assignments with grant provenance. |
| `HealthSubject` | Owner of PHI, profile timezone, lifecycle state, and subject-scoped preferences. Usually self-owned, but not conflated with the acting user. |
| `ProfessionalProfile` | Doctor/trainer type, verification status, jurisdiction, and credential metadata. Self-selected intent never means verified access. |
| `CareRelationship` | Subject-to-professional invitation and lifecycle: proposed, accepted, active, paused, revoked, or expired. |
| `ConsentGrant` / scopes | Versioned subject consent for domain, action, purpose, time range, expiry, and revocation. |
| `AccessContext` | Boundary value containing principal, selected subject, role assignments, relationship, consent version/scopes, session/token, and optional support grant. |
| `IntegrationConnection` | Per-subject provider credentials/token reference, cursor, breaker, sync state, and lease namespace. |
| `PlatformIntegrationConnection` | Installation-wide provider gateway managed only by active platform superadmins. OpenRouter uses this root; it has no subject FK and stores only an opaque credential reference, never the secret. |
| `AIInvocation` | Subject-owned paid-operation ledger linking S, optional A/system origin, exact platform gateway, purpose/model/config version, idempotency, lifecycle, bounded usage, and cost. It stores no prompt, completion, document bytes, or medical values. |
| `FileAsset` | Private stored object with subject, owner/uploader, purpose, content metadata, and access policy. |
| `SupportAccessGrant` | Time-limited, reasoned, scoped support authorization for one superadmin and one subject. |
| `AuditEvent` | Append-only record of sensitive reads, writes, exports, consent changes, overrides, and support activity. |
| `CareThread` / `CareMessage` | Subject-scoped, explicitly joined, patient-visible communication with private attachments. |

Patient facts receive `subject_id`; cross-organization deployment may later add
`tenant_id`, but a tenant must never substitute for the subject boundary. Global
catalog definitions may remain global. User choices currently mixed into global
catalog rows must move to subject preferences.

## Superadmin and support access

`platform_superadmin` is for migrations, service configuration, account recovery,
job health, quotas, and incident response. It does not make an unscoped query of
health tables legal.

OpenRouter is one centrally funded platform gateway. An active superadmin may
configure its secret reference, endpoint, model allowlist, budgets, and kill
switch, but cannot compose a prompt, invoke a model against a subject, or inspect
an AI artifact without the same subject authorization that any other actor
needs. Subject owners may use authorized AI features without becoming admins;
professional and support use remains relationship/consent/grant scoped.

The first control surface is `/settings/platform/ai`. It authorizes an active
platform superadmin independently from subject access, exposes only gateway
status/version, opaque subject UUIDs, half-open UTC quota periods, and aggregate
ledger counters, and never loads a subject profile or AI artifact. Root rotation
uses the allowlisted environment resolver reference; the raw key remains in the
legacy secret file/process environment. If commit fails after that environment
write, the outcome is treated as ambiguous: the credential is cleared and an
operator must reconcile the intended database root before re-entering it. The
kill switch and quotas govern migrated platform consumers only—currently Weekly
Digest, Daily Brief, Signals parsing/recovery, Telegram question replies, Labs
document recognition, and Body Scan recognition. Historical subject-owned
OpenRouter roots remain readable only as validated legacy provenance; no current
document-parser network call depends on them.

Every paid call follows `prepare/reserve -> commit -> provider I/O -> fresh
finalize/persist`. No database lock spans OpenRouter. `AIInvocation` reserves a
subject/purpose budget before dispatch, prevents retry duplication, and records
only operational metadata: opaque request ID, exact model/config version,
token/cost counters, timestamps, and a sanitized outcome. Prompts, completions,
raw payloads, document bytes, and health values are forbidden from this ledger
and from platform audit/usage screens.

A support investigation must create a `SupportAccessGrant` with:

- admin, subject, explicit reason, and optional ticket reference;
- mode (`read`, `repair`, or exceptional `export`);
- explicit resource/domain and action scopes; no wildcard scope;
- short expiry and immediate revocation;
- recent MFA/step-up authentication;
- patient approval by default; an emergency path requires a separately designed
  break-glass policy and at least two-person approval;
- a persistent support banner and immutable audit events for sensitive reads and
  every write;
- for repair, before/after identifiers and a bounded diff, never copied PHI in
  ordinary application logs.

The last active superadmin cannot remove or suspend itself. Superadmin accounts
are never shared and cannot mint a global MCP token. Debug MCP access, if ever
enabled, is short-lived and bound to one subject and one support grant.

Operational telemetry, product analytics, and support access are separate:

- operational telemetry is designed to contain no PHI;
- product analytics is aggregated/pseudonymized and governed by an explicit
  retention and opt-in policy;
- support access is an identified investigation against a specific support grant.

## Pull request sequence

Each PR must be independently reviewable, include a real downgrade while the
schema remains reversible, and preserve the single-owner production path until
the corresponding cutover gate passes.

### PR 01 — Identity and controlled-support foundation — **merged**

Merged into `commercial/main` on 2026-08-19. SQLite model, constraint,
upgrade/downgrade, and full fast-suite checks passed. The PostgreSQL 15 gate was
subsequently completed with the PR-02 validation: the full migration chain,
identity downgrade/re-upgrade, production constraints, and FK behavior passed.

Scope:

- add `User`, additive role assignments, `HealthSubject`, `SupportAccessGrant`,
  normalized support scopes, and append-only `AuditEvent` models;
- create Alembic revision `0035` above the current `0034` head;
- register models and add model/constraint tests;
- do not change current authentication and do not expose registration.

Exit criteria:

- model and migration shapes agree on defaults, checks, indexes, foreign keys,
  and downgrades;
- duplicate identity/role/subject/scope constraints fail;
- support access cannot be represented without a subject, reason, expiry, mode,
  admin, and scopes;
- SQLite fast tests and PostgreSQL migration tests pass.

Rollback: downgrade drops only the new empty foundation tables. No legacy row is
changed in this PR.

### PR 02 — Legacy owner bootstrap and access context — **merged**

Merged into `commercial/main` as `1ecbacf` on 2026-08-19. The final fast suite
passed with `1739 passed, 32 skipped`; the skips are production-only integration
cases. A throwaway PostgreSQL 15 run passed all 65 focused foundation/bootstrap
tests, including concurrent bootstrap and last-admin revocation, after a real
Alembic `upgrade head → downgrade 0034 → upgrade head` round trip. Ruff was not
available in the project virtualenv and was not claimed as passed.

Scope:

- idempotently create the current owner from `VITALS_AUTH_USERNAME` and copy the
  existing bcrypt hash without rehashing;
- assign `member + platform_superadmin` and create the self-owned subject with
  the legacy timezone;
- introduce the framework-independent `AccessContext` and policy vocabulary;
- keep the existing cookie/login flow active behind a compatibility adapter;
- add session/token versioning so a later cutover can revoke old credentials.
- keep identity, support, and audit control-plane rows out of ordinary user
  backup/restore while subject-scoped portability is not yet available.

Tests:

- repeated bootstrap is idempotent and never duplicates roles/subjects;
- a missing email is supported; invalid/ambiguous legacy identity fails closed;
- role alone cannot authorize a health-data action;
- the last-superadmin invariant is enforced.
- legacy or forged imports cannot read, replace, or delete identity control-plane
  state.

Rollback: retain new rows but return the compatibility adapter to legacy-only
mode. Never delete the copied password hash until the auth cutover is verified.

### PR 03 — Subject ownership expansion and backfill — **merged**

The canonical table-by-table contract is
`docs/COMMERCIAL_OWNERSHIP_INVENTORY.md`. It classifies the original 55-table
expansion plus seven post-foundation control-plane additions—the current 62
tables—along with missing ownership roots, natural keys, cross-surface
dependencies, backfill order, and rollback boundary. The runtime write-path
contract is maintained in `docs/COMMERCIAL_DUAL_WRITE_MATRIX.md`.

Implementation progress on `commercial/main`:

- Stage 0 is complete in revisions `0036`: the exhaustive registry, subject-bound
  integration/file roots, scoped setting stores, and idempotent legacy resource
  bootstrap are in place without reading credentials or file bytes.
- Stage 1 is complete in revisions `0037` and `0038`: all 36 top-level and six
  inherited child tables have their nullable ownership references, supporting
  indexes, and future parent subject keys. Existing global uniqueness and parent
  foreign keys remain intact.
- Generic MCP/LLM output and backup v1 suppress tenant/resource identifiers.
  Backup v1 safely rebinds to the sole local subject and fails closed if more
  than one subject exists; a subject-selected multi-user format remains a later
  portability cutover.
- Stage 2 is in progress. Subject/actor/resource dual-write is implemented for
  persisted medical uploads, Garmin and Hevy ingestion, direct Timeline and
  Supplements CRUD, Signals and Day Context, proactive Telegram delivery, and
  the subject-scoped week template. Typed alert ownership now separates health,
  provider, and platform namespaces; generic web/MCP lifecycle actions aggregate
  current and retired provider roots, and provider/scheduler writers use an exact
  reviewed scope. The scheduled empty-day brief writer now uses an actorless
  subject alert context and exact-one fully-null bridge. Daily Brief web and
  scheduler generation now reserve the centrally funded gateway, close both
  reservation and dispatch transactions before exactly one provider call, and
  atomically finalize sanitized accounting with the artifact. Header-only
  fallback remains useful without platform capacity. Telegram, notification
  journalling, and alert bookkeeping remain separate phases so network waits hold
  no database transaction. Signals live parsing and scheduled recovery now use
  raw-bound subject `AIInvocation` rows against the same platform gateway, with
  three bounded attempts, exact usage accounting, keyset backlog selection that
  scans past deterministic invalid head rows, and no subject OpenRouter
  dependency. Live and recovery paths commit reserve and charge phases before
  the single parser await, atomically finalize successful raw/Signal state, and
  reconcile an actorless S-only/C-null warning afterward.
  Fully-null historical raws may gain only S through the exact-one bridge;
  partial graphs fail closed. Echoes link the terminal invocation and revalidate
  concurrent edits before dispatch. Owned Telegram sends now use a durable
  subject/recipient/raw-bound intent: PENDING and DISPATCHING are committed
  before one network call, SENT is atomically linked to its journal afterward,
  and ambiguous delivery is terminal and conservatively counted. A stale
  raw-backed claim with proof that dispatch never began can be re-armed from
  deterministic domain state; uncertain dispatch is never retried. Telegram question
  replies now use a distinct raw-bound platform invocation with one lifetime
  paid attempt. Raw classification and reservation are atomic, no database
  transaction spans the usage-aware provider call, and terminal accounting
  precedes delivery. The generated answer stays in redacted, non-pickleable
  memory and the Notification payload records only an opaque raw marker plus the
  optional terminal invocation. DB-backed invocation gaps and an opaque cursor
  recover past long non-question histories without buying a second attempt;
  concurrent edits, owner/recipient changes, and module disable are rechecked
  before transport. The raw/category intent provides an at-most-once new-message
  claim; atomic emergency edit/withdrawal remains a PR-09 blocker. All seven conflict
  resolver domains read within one subject and date. Curated conflict-rule
  activation is stored per subject. The
  Supplements, Nutrition, Skincare, GLP-1, and Labs web/MCP write paths use an
  opaque prepared writer capability and locked target rows. Supplements replace
  one catalog row;
  Nutrition replaces the subject-day aggregate and evaluates projected totals so
  concurrent meals cannot bypass a cumulative rule. Skincare replaces one
  subject-day checklist without comparing its new actives to the stale checklist.
  Nutrition deletes share the same subject lock, and its actorless day-end job
  uses scoped reconciliation. Skincare observations and personal products also
  dual-write S+A, while direct reads, notes, and deletes enforce the same subject.
  GLP-1 injections, dose phases, and side effects dual-write S+A with exact
  manual/MCP source provenance; plateau evaluation uses subject-scoped
  phase/Weight/noise reads and actorless alert reconciliation.
  Labs manual, MCP, and upload-confirmation writes dual-write S+A; MCP inputs are
  raw-first with C/F null. New document uploads commit exact S+A+F with C null
  and reserve a raw-bound platform `LAB_DOCUMENT_PARSE` invocation before any
  provider call. Local PDF conversion completes before charge; start/charge and
  final accounting/extraction are separate short transactions around exactly one
  usage-aware vision await, and transient finalization retries reuse the same
  paid in-memory completion. Parser facts accept
  that exact successful invocation graph or the historical subject/uploader,
  subject-OpenRouter-C, and lab-document-F chain. Failed/in-flight placeholders
  remain auditable but cannot enter replay. Direct Labs reads and
  mutations, derived alerts, startup marker seeding, and nightly replay use the
  same exact-one subject boundary.
  HRT doses, side effects, cycles, child plans, and templates now use the same
  prepared owner boundary across direct web/MCP reads and mutations. Root creates
  retain manual/MCP provenance, child graphs validate and inherit S, edits preserve
  historical actor/source, and template JSON stays parse-only. Protocol reminders
  combine only that subject's cycle, dose, and Labs state and reconcile actorless
  alerts. Curated compounds remain global; their per-subject activation setting is
  still a required cutover before registration.
  Direct WeightLog writes and Garmin/body-scan bridges now use the canonical
  governance -> outbox advisory -> subject capability, while the Garmin Weight
  outbox validates one S+destination-C scope and rechecks lifecycle before network
  activity. BodyMeasurement and NoiseMarker direct web/MCP paths now dual-write
  S+A with manual/MCP source provenance, reject partial roots and foreign IDs,
  and reconcile the noisy-period health alert actorlessly. The authenticated
  Weight chart propagates the selected subject through its direct series.
  BodyScan upload confirmation and MCP structured ingest are now raw-first and
  validate the complete subject/uploader/file/parser/raw/metric graph before
  normalization. Direct scan reads, notes, deletes, metric history/catalog/BIA,
  conflict evaluation, derived alerts, and nightly replay use that same exact-one
  subject boundary; replay isolates failures per raw row and MCP provenance keeps
  Source.MCP on raw/scan while the derived Weight remains Source.BODY_SCAN.
  New Body Scan document parsing commits exact S+A+F, a C-null raw placeholder,
  and one raw-bound platform `BODY_SCAN_PARSE` invocation before charge. Local
  conversion precedes T2, one usage-aware vision call runs outside every DB
  transaction, and T3 atomically finalizes accounting plus the strict verbatim
  extraction. Confirm keeps the existing Weight/Garmin lock order and remains
  editable; replay and derived Weight accept that exact successful invocation or
  validated historical subject-C provenance, never a mixed graph. File retirement
  denies document access while preserving the independent historical Weight fact.
  ProgressPhoto upload, gallery reads, Timeline markers, protected downloads,
  and deletes now validate one exact subject/owner/FileAsset graph. File assets
  are exclusive to one photo; deletion durably retires metadata before unlinking
  bytes, and only the fully-null S/A/F legacy shape can use the exact-one bridge.
  Genetics web/MCP/CLI writers now persist a content-addressed owned VCF raw
  revision before curated normalization, scope direct CRUD by the selected
  subject, preserve historical actor/source/raw provenance on correction, and
  replay pending partial batches.
  Re-import uses full parser replacement for an existing rsID so a reference
  genotype cannot retain an obsolete conflict marker, while replay cannot replace
  a newer fact with older pending evidence. Versioned truncated raws retain the
  first 50,000 parsed rows plus canonical evidence for every curated tail hit,
  hash both collections, and rebuild the same catalog facts on replay. Partial
  raw roots and malformed v2 evidence fail before adoption or normalization.
  A lossless whole-file chunk/import-batch design still belongs to the scoped-key
  cutover.
  Milestone web/MCP creates now dual-write S+A, direct reads and mutations use
  the same prepared exact-one subject boundary, and updates preserve the
  historical actor. Timeline, Today, and the external glance API consume
  subject-scoped goal rows; Weight, BodyMeasurement, BodyScan, Nutrition, and
  module-setting inputs used for live goal progress receive that same scope.
  Legacy whole-lake MCP snapshot/export/overview tools and manual, MCP, or
  scheduled weekly-digest generation now acquire the same exact-one governance
  proof before any global compatibility query or LLM request. Weekly generation
  reserves platform and subject quota, commits before one OpenRouter call, and
  atomically persists sanitized accounting plus an invocation-linked artifact;
  no subject provider C is created for the platform gateway. Three bounded
  attempt slots distinguish terminal failures from an in-flight dispatch, and a
  completed or dispatching product is reused even when the gateway root, quota
  period, or context-sized reservation has since changed. An incompatible
  PREPARED reservation is released before advancing, and a 15-minute platform
  recovery job releases abandoned reservations. The bridge
  accepts only fully-null S/A history and rejects partial roots.
  These paths resolve the verified legacy owner at their web, scheduler, or MCP
  boundary and fail closed if a second subject makes the compatibility bridge
  ambiguous.
  Backup v1 deliberately leaves WeeklyDigest and AI accounting rows in place
  because stripping either legacy C or `AIInvocation` would manufacture invalid
  provenance; the curated LLM export still carries narrative content.
  SharedReport creation and owner lifecycle now use a transaction-bound
  exact-one owner proof. New frozen artifacts carry S plus their human creator;
  list/get/download/revoke/delete are exact-S, a human revoke records its actor,
  and only fully-null historical S/creator/revoker roots enter the bridge.
  Public tokens remain caller-subject-free capabilities, but validate their
  stored subject/actor graph and re-lock the live token before counting an open.
  Anonymous open and scheduled purge never infer ownership actors. Password
  verification releases governance first, unlocked rendering retains governance
  through HTML construction, and the cookie binds both report id and token.
  The frozen snapshot is unchanged, and its underlying domain assembly remains
  an exact-one whole-lake compatibility read pending PR-10 AccessContext work.
- The completed Stage 2 slices include SQLite isolation tests and PostgreSQL 15
  migration/foreign-key tests. Direct interactive selectors are covered for all
  current Timeline event types, and provider ingestion preserves raw-first
  provenance with subject/connection ownership.
- Stage 3A adds schema-only revision `0045` plus the
  fixed `stage3.raw_payloads.v1` operator phase. Status/preflight is the default
  and cannot write; `--apply` advances independently committed stable-PK batches
  behind a subject-bound checkpoint. The initial high watermark, cumulative
  counts, and deterministic data/ownership checksums make stop/resume and
  no-content-change validation explicit. CLI JSON excludes S and raw/checkpoint
  IDs as well as payloads, titles, dates, paths, credentials, and free-form
  errors. Its throwaway PostgreSQL 15 gate passed a real migration build through
  revision `0034` and then to head, batch-size-2 process stop/resume, idempotent
  completion, unchanged raw/link/frozen-output hashes, and populated-checkpoint
  downgrade refusal.
  A non-empty v1 full restore has stripped A/C/F provenance and atomically marks
  this phase `RESTORE_BLOCKED`; ordinary apply cannot guess or clear it. Future
  backup-v2 or reviewed manual remap is required. An empty restore records an
  empty `COMPLETED` checkpoint, and any retained AI/delivery raw reference blocks
  replacement before mutation. The checkpoint never authorizes an S-only raw
  row. Operators pause all raw writers for the complete multi-batch run; the
  final transition performs a bounded-page full-snapshot rehash and fails closed
  on cross-batch payload, count, or ownership drift.
- Stage 3B uses the same payload-free checkpoint schema for the fixed
  `stage3.normalized_manual.v1` catalog. It covers the 17 actor-optional
  top-level tables whose historical ownership can be proved without inventing
  a connector, raw, file, or control-plane root: HRT cycle/template parents and
  dose/effect facts, annotations, body measurements, GLP-1 facts, lab markers,
  meals, milestones, noise markers, skincare facts/products, and supplements.
  Each table owns an independent immutable phase key, high watermark, cursor,
  counts, and checksum chains; the operator aggregates that closed catalog but
  cannot select an arbitrary table. Historical `(S=NULL,A=NULL)` rows gain only
  the sole legacy S, while live rows above the watermark must already carry the
  exact S and reviewed actor (except actorless subject-owned LabMarker seeds).
  Stage 3A must be `COMPLETED` first. Backup-v1 replacement atomically re-bases
  these fixed table checkpoints because it safely rebinds S while deliberately
  dropping unprovable historical A; it never re-bases provider/raw/file phases.
  All 17 writers remain paused for the complete multi-batch maintenance window,
  and final per-table rehashing proves that business data and timestamps did not
  change.
- Stage 3C uses `stage3.inherited_children.hrt.v1` for exactly
  `hrt_cycle_items` and `hrt_cycle_template_items`. It requires completed Stage
  3A and all Stage-3B checkpoints, copies only the reviewed parent S into
  historical null-S children, and rejects foreign parent/child or unsafe
  compound graphs. The two independent checkpoints retain the bounded
  status/apply, stop/resume, final locked rehash, and no-PHI JSON contracts.
  Backup v1 resets them atomically after the raw and normalized checkpoint
  transitions. The remaining body-scan, Hevy, and compound children wait for
  their provenance-aware parent phases.
- Stage 3D uses `stage3.provider_raw_linked.v1` for exactly `garmin_daily`,
  `garmin_activities`, `garmin_intraday`, and `hevy_workouts`. Each normalized
  S/C pair is copied only from its exact completed-Stage-3A raw link; A is never
  invented or rewritten. Raw external IDs, provider/type roots, future scoped
  keys, and transitional Hevy children are validated under a full provider-writer
  maintenance pause. Backup v1 cannot reconstruct C, so every non-empty restored
  provider checkpoint is `RESTORE_BLOCKED` pending a provenance-bearing backup
  or reviewed remap; empty snapshots complete.
- Stage 3E uses `stage3.inherited_children.hevy.v1` for exactly
  `hevy_exercises` and `hevy_sets`. It requires the completed Stage-3D parent
  graph, copies only exact inherited S/C, and rejects partial, foreign, orphaned,
  or live-null child roots. Both tables are volatile rebuild outputs, so the
  initial snapshot is finalized under a full Hevy writer pause and later status
  validates the current strict graph. Backup v1 records non-empty child
  checkpoints as `RESTORE_BLOCKED`; empty snapshots complete.
- Stage 3F uses `stage3.mixed_catalog.hrt.v1` for exactly `hrt_compounds`
  followed by `hrt_compound_components`. Checked-in `system` definitions remain
  global and must match the current YAML scalars and complete component
  multiset. Historical `manual`/`mcp` definitions outside the curated key set
  gain only the sole S; A and every medical value remain unchanged, and custom
  components inherit only their exact parent S. Linked doses and cycle items
  are validation-only and must retain a matching snapshot key. Initial
  completion freezes all-row data while durable post-completion evidence covers
  the custom subset only, allowing legitimate curated catalog reseeds without
  hiding custom deletion, reparenting, or ownership drift. Backup v1 preserves
  the reviewed source/key marker, so its exact two checkpoints reset to bounded
  RUNNING/empty-COMPLETED states rather than guessing any C/F provenance.
- Stage 3G uses `stage3.mixed_catalog.conflict_rules.v1` for exactly
  `conflict_rules`. Authentic checked-in definitions stay global; reviewed
  historical custom rules gain only S and preserve `active` plus all rule data.
  Initial completion locks and hashes the full snapshot, while durable
  post-completion evidence covers the custom subset so catalog reseeds cannot
  erase custom history. Backup v1 can reset this phase from its retained
  subject-bound marker. The separate subject activation-setting migration and
  strict conflict registration/read cutover remain pending.
- Stage 3H uses `stage3.file_backed.progress_photos.v1` for exactly
  `progress_photos`. Under a full photo-writer maintenance pause it assigns S
  and a metadata-only legacy FileAsset to reviewed fully-unowned history while
  preserving A as null and leaving the stored bytes and every photo field
  untouched. A checkpoint-bounded consumer bridge accepts that actorless S+F
  graph without weakening the exact live S+A+F path. Duplicate keys/F links,
  unsafe or document-alias paths, partial roots, and unlinked live photo assets
  fail closed. Completed verification is deletion-aware: supported deletion
  retires the asset and removes the fact, then the current bijective graph is
  revalidated instead of requiring deleted IDs to survive. Backup v1 cannot
  carry file bytes or prove A/F, so a nonempty restore is `RESTORE_BLOCKED` and
  creates no placeholder; an empty restore completes exactly.
- Stage 3I uses `stage3.channel_optional.day_context.v1` for exactly
  `day_context`. It assigns only the sole S to reviewed fully-unowned history
  and preserves every answer, plan, timestamp, source, and existing valid A/C;
  neither an actor nor a Telegram recipient connection is inferred. Initial
  completion runs under a complete day-context writer pause. Because the row
  for a date is deliberately overwritten, later completed checks validate the
  current ownership/provenance graph without comparing frozen data or ownership
  digests, while retaining frozen IDs and cardinality as migration evidence.
  Backup v1 retains the
  subject-bound marker and content, so the exact checkpoint resets to bounded
  RUNNING for a nonempty snapshot or COMPLETED for an empty one after Stage 3H
  handling and before replacement.
- Stage 3J uses `stage3.channel_optional.signals.v1` for exactly `signals`. It
  assigns only the sole S to reviewed fully-unowned history and preserves every
  signal value, batch/raw link, timestamp, and valid optional owner/Telegram
  recipient root. MCP facts remain raw/channel-neutral; Telegram raw links and
  batch membership are validated without inferring stripped A/C. A late
  actorless reparse is accepted only from the exact S+C/A-null Telegram raw row
  already frozen by Stage 3A. Initial
  completion runs under a complete signal-writer pause, while later completed
  checks validate the current volatile graph so supported misparse, delete,
  reparse, and new-ingest transitions remain legal. Backup v1 retains `raw_id`
  and content, rebinds S, and resets the exact checkpoint after Stage 3I to
  RUNNING for a nonempty snapshot or COMPLETED for an empty one.
- Stage 3K uses `stage3.retained_artifact.shared_reports.v1` for exactly
  `shared_reports`. It adds only the sole S to reviewed fully-unowned retained
  reports and preserves creator/revoker gaps plus every token, password hash,
  frozen snapshot, lifecycle value, counter, and timestamp. A checkpoint-aware
  boundary keeps only the unprocessed frozen tail on the fully-null bridge and
  requires strict ownership for rows created above the snapshot HWM. Backup v1
  intentionally neither exports nor replaces published reports; import
  prepares or preserves the retained Stage-3K checkpoint after Stage 3J reset
  and validates it again after portable replacement.
- Stage 3L uses `stage3.channel_optional.weight_logs.v1` for exactly
  `weight_logs`. It adds only the sole S to reviewed fully-unowned historical
  weights, preserves nullable owner A and nullable same-subject Garmin/AI-gateway
  C, and never copies a connection down from the raw payload a fact links.
  Manual/MCP, Garmin, and body-scan raw provenance and parser-invocation
  exclusivity are validated read-only, and the one-active-weight-per-date
  invariant is proved independently of the legacy global index. Backup v1
  retains `raw_payload_id` and content, rebinds S, and resets the exact
  checkpoint after Stage 3K to RUNNING for a nonempty snapshot or COMPLETED for
  an empty one.
- Stage 3M uses `stage3.raw_linked_facts.lab_results.v1` for exactly
  `lab_results`. It adds only the sole S to reviewed fully-unowned historical
  results and preserves the nullable owner A, the raw link, and every measured
  value. Parser provenance is validated read-only on the linked raw payload:
  subject-funded gateway history, a platform parse backed by a matching
  same-subject lab document and one succeeded invocation, or a fileless raw that
  may not claim a parser. Backup v1 retains `raw_payload_id` and content,
  rebinds S, and resets the exact checkpoint after Stage 3L.
- Stage 3N uses `stage3.raw_linked_facts.genetic_variants.v1` for exactly
  `genetic_variants`. It adds only the sole S to reviewed fully-unowned
  historical variants and preserves the nullable owner A, the VCF batch link,
  and every interpreted value. Manual and MCP variants stay rawless, an imported
  variant keeps its durable streamed VCF batch with null connection and file
  roots, and the one-variant-per-rsID invariant is proved independently of the
  legacy global index. Backup v1 retains `raw_payload_id` and content, rebinds
  S, and resets the exact checkpoint after Stage 3M.
- Stage 3O uses `stage3.file_backed.body_scans.v1` for exactly `body_scans`. It
  adds only the sole S to reviewed fully-unowned scans and, for a scan that kept
  its sheet, one metadata-only FileAsset root with a null uploader. Manual scans
  stay file- and raw-free, structured MCP scans stay file-free, and parsed
  provenance is validated read-only on the raw payload. The body-scan reader now
  recognises the reviewed placeholder so migrated sheet history stays legible.
  Backup v1 carries neither sheet bytes nor trustworthy A/F, so a nonempty
  restored snapshot is recorded as `RESTORE_BLOCKED`.
- Stage 3P uses `stage3.inherited_children.body_scan_metrics.v1` for exactly
  `body_scan_metrics`. It copies only the reviewed parent scan's subject down to
  its metrics and changes no measured value. A child never leads its parent, a
  live child requires the strict parent graph, and parents are locked before
  children so a concurrent scan adoption cannot race the child update. Backup v1
  rebinds the child subject and resets the exact checkpoint after Stage 3O.
- Stage 3Q uses `stage3.provider_outbox.garmin_weight_exports.v1` for exactly
  `garmin_weight_exports`. It adds the sole S plus the exact reviewed legacy
  Garmin account a queued row was destined for, and invents no requester. A
  missing, rotated, or non-legacy account fails the read-only preflight while
  adoption is pending. Backup v1 cannot carry a required destination, so a
  nonempty restored snapshot is recorded as `RESTORE_BLOCKED` and the operator
  refuses to advance.
- Stage 3R uses `stage3.retained_artifact.weekly_digests.v1` for exactly
  `weekly_digests`. It adds only the sole S to reviewed fully-unowned artifacts
  and preserves the narrative, its context, the model, and both funding roots.
  Subject-funded and platform-funded provenance are mutually exclusive, a linked
  invocation must belong to the subject and match its kind, and a digest above
  the frozen watermark must carry reviewed AI funding. Backup v1 neither exports
  nor replaces digests; import prepares or preserves the retained checkpoint.
- Stage 3S uses `stage3.delivery_artifact.notifications.v1` for exactly
  `notifications`. It adds the sole S, the reviewed owner as recipient, and the
  exact reviewed legacy Telegram root together, because a delivered message needs
  all three or none. A rotated or additional recipient fails the read-only
  preflight, and an AI reply must link a same-subject invocation that succeeded.
  `notifications` becomes retained rather than portable: backup v1 carries
  neither recipient nor channel, so import prepares or preserves the retained
  checkpoint instead of replacing the delivery log.
- Stage 3T uses `stage3.subject_optional.system_alerts.v1` for exactly
  `system_alerts` and completes the backfill catalogue. It classifies each alert
  through the writer's own reviewed key allowlist and adds only what the class
  proves: a health or conflict alert gains the sole S, a provider alert also
  gains its exact reviewed legacy connection, and an installation-wide platform
  alert keeps neither root. An unclassified key fails closed. Backup v1 rebinds S
  but strips C, so a restored provider alert is completed again.
- Stage 4 uses `stage4.whole_lake_validation.v1` and revision `0046`. The
  revision adds six parent/child subject-equality foreign keys as composite
  references to the parent's `(id, subject_id)`, installed `NOT VALID` on
  PostgreSQL so the migration never scans an unproved lake; the operation makes
  them valid only after proving the graph. Its check inventory is derived from
  the schema metadata and the ownership registry, so a persisted but
  unclassified table fails the run. One pass proves required S presence, that no
  S/A/C/F/raw reference leaves the reviewed roots, parent/child and
  raw/normalized S equality, scoped-versus-legacy shadow read equality, and that
  exactly one subject exists. A curated catalog parent carries no S and its
  inherited components carry none either: what is proved there is equality, not
  presence. Every Stage-3 phase must be terminal first, and the evidence is a
  chained digest of the whole graph, so later writes invalidate it instead of
  granting a stale proof. Old global uniqueness is deliberately retained here.
- Stage 5A uses `stage5.scoped_key_audit.v1` and the reviewed catalog in
  `vitals/scoped_keys.py`: twelve legacy global keys and the sixteen scoped
  indexes that replace them, scoped by subject, by connection, by curated-versus-
  custom catalog row, or by alert class. The audit proves read-only that no row
  would collide under a proposed key and that no row is missing the scope its key
  depends on — a scoped unique index over a null scope column keeps no uniqueness
  at all, so the cutover would silently lose the rule. It requires Stage 4 to have
  proved this exact lake, and it creates, drops, and rewrites nothing but its own
  checkpoint. `skincare_logs` and `supplements` are out of scope: they carry no
  global uniqueness today and so never blocked a second subject.
- Stage 5B installs all sixteen scoped keys in revision `0047`, beside the legacy
  global keys rather than instead of them. Every replacement is strictly weaker
  than the key it narrows, so installation rejects nothing and no reader or
  writer changes. PostgreSQL builds them `CONCURRENTLY`; downgrade drops them
  transactionally so a refused downgrade rolls the whole attempt back.
- Stage 5C switches every key-based write path to the scoped key: Garmin days
  and activities, Hevy workouts, and weight-export intents resolve inside their
  connection; day context inside its subject; the compound and rule catalogs
  read only the platform half of their key; and an active alert resolves inside
  the root its class belongs to. A row outside the caller's scope is no longer
  read into the write path at all. While the legacy global keys stand, each path
  carries one narrowly scoped bridge reporting an out-of-scope occupant as a
  typed cutover error; each bridge is removed with its key.
- Stage 5D drops all twelve legacy global keys in revision `0048`, together with
  every temporary bridge that stood in for one. Two subjects now write the same
  weigh-in date, marker name, rsID, and provider external id concurrently on
  real PostgreSQL, while one subject still cannot hold the same key twice. What
  replaces each bridge is a real invariant: a mis-shaped active alert is still
  refused, a genetics rename cannot land on an rsID the subject already holds,
  and the catalogs cannot see a subject's own row at all. Downgrade recreates
  the keys only while the data still satisfies them.
- Stage 5 is complete: the audit, the scoped keys, the switched write paths and
  the drop of the global keys. PR-04 then closed what a second subject still
  needed — see its section below.
- Remaining before a multi-user release: lossless VCF chunking, full MCP
  principal propagation, and the `AccessContext` cutover that replaces the
  sole-subject resolver with a real principal (PR-05 and PR-10).
  Registration stays disabled; the current code is a safe single-subject
  migration bridge, not a multi-user release boundary.

Scope:

- add nullable `subject_id` (and connector/actor fields where required) to every
  PHI table, raw payload, alert, notification, report, file reference, outbox, and
  child table;
- split global `AppSetting` state into user, subject, and platform settings;
- backfill the legacy subject in bounded batches and verify counts/checksums;
- retain old columns, readers, and global uniqueness while nullable expansion,
  dual-write, and backfill are validated;
- run a separate gated scoped-key cutover after backfill: create and validate
  subject/connection-scoped unique indexes, switch every key-based write/read,
  then remove the corresponding global uniqueness;
- keep every path to a second writable subject disabled until that cutover has
  passed.

Tests:

- expand/backfill validation proves orphan count is zero, child rows cannot
  cross subjects, and old/new counts and checksums agree while old uniqueness is
  still present;
- after the gated key cutover, two subjects may use the same date, rsID, alert
  key, notification dedupe key, and external payload ID, and two connections may
  use the same Garmin activity ID or Hevy workout ID without collision;
- a real `0034` snapshot upgrades with counts, provenance, raw links, and frozen
  reports intact.

Rollback: nullable expansion and legacy columns allow code rollback before a
second subject can write. A removed global unique may be recreated only after
proving the data still satisfies it. Once a second subject has written data,
especially a duplicate of a legacy global key, downgrade to the single-subject
schema is forbidden; recovery requires a verified backup and forward fix.

### PR 04 — Scoped services, policy engine, and PostgreSQL RLS — **merged**

Scope:

- require `AccessContext` or explicit subject/actor values at every core service;
- replace bare `session.get(Model, id)` and unscoped updates/deletes for PHI;
- scope conflict resolvers, analytics, LLM context, exports, and module gates;
- set transaction-local RLS context and enable FORCE RLS after application
  filters are complete;
- introduce a static/contract test that inventories all subject-owned tables and
  write paths.

Tests:

- complete A/B IDOR matrix at service level;
- conflict rules and LLM/report context never combine subjects;
- PostgreSQL RLS denies missing/wrong context and pooled connections do not leak
  the previous transaction's subject;
- revoke between authorization and write fails closed.

Rollback: feature flag the new policy adapter only while no second subject can
register. RLS can be disabled in a controlled rollback without dropping scope
columns.

Delivered:

- The legacy scope registry reached zero. `vitals/legacy_scope.py` started at
  266 functions across 25 modules that would read or write across the whole
  installation when the caller left the scope out; there are none, and the
  contract test asserts equality rather than a lower bound so a bridge cannot
  reopen without deleting that assertion.
- The conflict engine has no unscoped path. The seven `legacy_resolver=`
  registrations, the engine's second resolver arm, `evaluate`, `enforce`,
  `enforce_day_end` and the seven unscoped domain readers are gone;
  `register_domain_resolver` refuses a reader without a keyword-only `scope`.
- No production path writes an ownerless row. Garmin's and Hevy's unscoped sync,
  pulse, ingest and reparse entry points are deleted along with
  `raw_payload_service.upsert_raw_payload`; every one had an owned twin the live
  callers were already using.
- Revision `0049` makes all thirty-nine remaining registered-required references
  `NOT NULL`, in the models and the database together, and refuses to run while
  any target column still holds unstamped rows.
  `PRE_OWNERSHIP_CONTRACT_REVISION` records the deploy order in code: migrate to
  `0048`, finish the backfill, migrate to head.
- Revision `0050` puts `FORCE ROW LEVEL SECURITY` and a subject policy on the
  forty-one tables whose `subject_id` is mandatory.
  `rls_session.bind_session_subject` sets the transaction-local value the policy
  reads; an unbound session sees nothing rather than everything.
- Revision `0051` covers the ten remaining tables that name a subject, with a
  second predicate for the ones where a NULL subject is a real state rather than
  an unfinished backfill.
- Revision `0053` admits a second scope, `vitals.platform_scope`, to all
  fifty-one policies, for the four paths that legitimately act for the
  installation rather than for a person: the published report a visitor opens
  with a token, and the three sweeps that run across every subject. Without it
  each of them reads nothing and reports success — a shared link answering
  "not found" indistinguishably from a revoked one, and three jobs going green
  while doing nothing at all. `enter_platform_scope` is the only way in, and a
  contract test enumerates every caller.

Partly delivered since: the policy engine now has a caller.
`vitals/services/access_resolution.py` resolves a real `AccessContext` — a
principal with its roles, the selected subject, and that subject's owner — and
`require_access` decides one exact resource and action through
`vitals.access.is_allowed`. `resolve_legacy_ownership_context` builds the
snapshot whenever a human is behind the operation, and the export routes are
the first to be decided by it rather than by being logged in; a refusal is 403
and says nothing about whose record was reached for.

What that changes is the shape of the refusal. The legacy resolver fails as soon
as a second subject exists, for either person, because it cannot tell whose
installation it is. `resolve_access_context` selects by ownership instead, so a
second person's record becomes ordinary denied access rather than an error about
the database's cardinality. Two properties are deliberate: resolving a context
authorizes nothing — the question "may I reach that record?" has to be
answerable without entering its scope — and a role is never a grant, so a doctor
or a platform superadmin is a stranger to every record without a live,
actor-bound, exactly-scoped one.

Still PR-10's: moving the remaining services and the composition readers off the
explicit `subject_id` they take today onto the context, and the relationship and
support grants that make a cross-subject answer anything other than no. `system_alerts`,
`conflict_rules` and the inherited children are also excluded from the blanket
RLS policy — they need "mine or the installation's" rather than "mine", which is
a different predicate and needs its own review.

### PR 05 — OIDC authentication, local sessions, and closed registration

Scope:

- replace environment-only login with an OIDC Authorization Code + PKCE boundary
  backed by a self-hosted identity provider; ZITADEL is the provisional default
  and Keycloak remains a standards-compatible alternative;
- delegate password hashing, password reset, email verification, brute-force
  protection, TOTP, WebAuthn/passkeys, recovery codes, and IdP login sessions to
  that provider instead of reimplementing authentication cryptography in Vitals;
- map the validated immutable `(issuer, sub)` identity to a local `User` and
  store only revocable Vitals application sessions, selected-subject state,
  session/token versions, and bounded recent-authentication/step-up evidence;
- validate discovery, exact issuer and audience, authorization-response issuer,
  state, nonce, PKCE, JWKS rotation, token times, authentication context, and
  logout/revocation without trusting email or display name as an identity key;
- rotate/invalidate legacy browser and MCP tokens at cutover;
- add `registration_mode = disabled | invite_only | admin_approved | open`, with
  every mode except `disabled` still feature-gated off initially; the IdP may
  authenticate or self-register an account, but Vitals alone decides whether a
  local user/subject may be provisioned;
- harden CSRF with tokens/Fetch Metadata for Internet-facing mutation routes.

Tests:

- session fixation, OIDC mix-up, login CSRF, code interception, stale JWKS,
  issuer/audience/nonce mismatch, user suspension, MFA/step-up freshness,
  upstream logout, and local session-version invalidation;
- provider conformance tests run against the pinned ZITADEL deployment and the
  generic OIDC adapter; no test-only identity claim may bypass token validation;
- no role escalation through registration form payloads;
- anonymous-surface contract remains explicit and fail closed.

Rollback: compatibility login is retained for one release behind an operator-only
emergency flag; rollback invalidates all new sessions rather than accepting both
credential models indefinitely.

Delivered, in four pieces, with two decisions taken against this plan:

- **ZITADEL**, and the adapter is provider-agnostic anyway — the choice is
  configuration, not code.
- **A hard cutover rather than the compatibility flag above.** The switch is
  `VITALS_OIDC_ISSUER`: while it is unset the existing login works, and setting
  it makes `/login` redirect to the provider and the password and TOTP paths
  404. That keeps the property the flag was for — nobody is locked out of their
  only copy of their medical record by a deploy — without two credential models
  ever being live at once. `authenticate()` refuses before it reaches the stored
  hash, so a bcrypt value surviving in the column is not a second way in.

What landed: the OIDC boundary (`vitals/services/oidc.py`) with every check in
one place because nothing downstream re-validates a login; revocable sessions
carrying the user id, session version and the provider's `auth_time`
(`session_service`); closed provisioning with a single operator-driven binding
for the pre-cutover owner (`federated_login_service`); and the two routes
themselves, whose handoff cookie carries state, nonce and verifier under its own
serializer salt and is cleared on every path out.

Both closed since: Fetch Metadata now guards every mutating route, and ZITADEL
runs behind a compose profile documented in `docs/OIDC_SETUP.md`.
The MCP OAuth server is deliberately untouched — Vitals issues those tokens
itself and PR-10 owns that surface; what changed for it is that a session
version bump now invalidates the browser sessions beside it.

**Registration and provisioning closed it.** The two remaining scope items had
been left because nothing needed them yet, and that was the problem: closed was
a property of there being nowhere for an account to come from.

- `registration_service` holds `registration_mode` in `platform_settings` with
  all four values from the scope above, `disabled` by default. Every other mode
  is gated behind `VITALS_REGISTRATION_UNLOCKED`, deliberately an environment
  variable: opening registration is a deployment decision that comes after a
  security review, and a mode an administrator can flip from a screen is not
  that. `invite_only` and `admin_approved` refuse with a message naming
  themselves as unimplemented rather than falling through to `open`, which is
  the shape of failure the module exists to prevent.
- `account_provisioning_service` is the one place a `HealthSubject` is born
  besides the legacy bootstrap. A subject needs four things — an owning account,
  a role, the integration roots every provider path resolves through, and a
  module map — and until now only the demo seeder assembled them, so the
  application and the script had two different ideas of what a subject is and
  the script's was the one anybody looked at. It never adopts `.env`'s provider
  credentials.
- `federated_login_service` consults the mode, and its refusal for a closed
  installation is byte-identical to its refusal for an unknown identity.
- `scripts/provision_account.py` is how an operator creates one today: no form,
  no route, no token, and whoever runs it already has a shell on the host.
- `scripts/link_identity.py` completes that closed operator flow by binding the
  new active account to the provider's exact `(issuer, subject)` identity. It
  does not search by email, cannot move an existing link, and is deliberately
  not exposed through a browser route.
- `scripts/registration_mode.py` reads and sets the stored mode, and is the only
  caller of `set_stored_mode`. Until it existed the mode had been described,
  gated and left without a handle: an installation could be unlocked and still
  had no way off `disabled`. It prints stored *and* effective, because those
  differ whenever the deployment gate is unset and "I set it to open and nothing
  happened" is otherwise a puzzle whose answer is one environment variable.

Release gate: pin and inventory the IdP image, verify backup/restore and upgrade
procedures, complete an AGPL/commercial-distribution review for ZITADEL, and keep
Vitals coupled only through OIDC/OAuth metadata and claims. A provider-specific
management API may automate invitations, but it must not become the PHI policy
engine.

### PR 06 — Private files, portability, and settings separation — **landed**

Delivered:

- `GET /files/{opaque_key}` serves private medical files by
  `FileAsset.opaque_key`, and `/static/uploads/{key:path}` is a seal that
  answers 404 to everybody so the static mount can never reach the private
  tree. Two checks the old path-addressed route needed — purpose gating and the
  `uploads/labs/x` alias — are gone with what they defended against.
- Upload confirmation was already bound to subject, raw payload, uploader and
  the intended model by the PR-03/PR-04 work; PR-06 pins the last of those with
  a test that offers a lab sheet through the body-scan door and vice versa.
- `export_subject` / `import_subject` are the personal half of portability,
  scoped to one subject and unable to touch another. Primary keys are
  reassigned because the id space is shared, references between carried rows
  are remapped, and references into the installation's catalog travel as a
  natural key.
- A full restore and a process restart are decided by
  `require_installation_operator` rather than by holding a session.
  `/settings/import` previously had no authorization at all.
- The settings page stops rendering the sign-in card once the provider owns
  sign-in — it was painting a stale TOTP secret beside buttons that 404.

Not done, and stated rather than implied: `import_full` still deletes each
portable table unqualified. That is correct for a whole-database restore, which
is now an operator's operation, but it leaves the operator path all-or-nothing
while the personal one is scoped. Moving the uploads tree out of
`web/static/uploads` to a private root would also make the file guarantee
structural rather than a matter of route ordering.

Original scope:

- move protected uploads behind `FileAsset` ownership checks and opaque keys;
- bind upload confirmation to subject, raw payload, uploader, and intended model;
- provide subject-scoped export/import that never wipes another subject;
- reserve full database backup/restore and process restart for operators;
- isolate account, profile, professional, integration, and system settings.

Tests:

- known file keys/raw payload IDs cannot be rebound or downloaded cross-subject;
- exports contain one subject and no credentials, roles, consent grants, live
  share tokens, or unrelated rows;
- import cannot delete or overwrite another subject.

Rollback: keep original files until verified migration checksums and access logs
match. No destructive global restore is exposed to ordinary users.

### PR 07 — Professional profiles, invitations, relationships, and consent — **landed**

Delivered, as five tables and four services:

- `professional_profiles` holds what somebody claims about themselves and an
  operator's verdict on it. Verification answers a question about the world
  outside this installation; it grants access to nothing, and the table has no
  `subject_id` because nothing in it belongs to a patient.
- `professional_invitations` is a one-time, expiring, address-bound link whose
  token is stored only as a hash. Every refusal — spent, expired, revoked, wrong
  address, never existed — is the same message, because told apart they map who
  is being treated by whom.
- `care_relationships` and `consent_grants` are the two halves access needs, and
  `resolve_access_context` now loads them into the `RelationshipGrant` the
  policy engine has been able to evaluate since PR-04 and had never been given.
  Consent is versioned rather than edited, so what applied last month survives
  this month's change.
- A doctor and a trainer are offered the same whole record. What separates them
  is that they are two different people — two relationships, two sets of their
  own notes — and the kind is a fact rather than a label: a professional whose
  profile says doctor cannot be taken on as somebody's trainer. Every default on
  patient facts is read-only for both.
- `professional_notes` and `care_plans` are where a professional's contribution
  goes instead. Only the author may change one, and neither has a delete path.

Revisions `0054` through `0057`, each new subject-bearing table carrying the
two-clause policy from the start. The RLS contract now reads its covered set
from every policy revision, so the next table is one line rather than a rewrite.

Original scope:

- add professional verification states and operator verification workflow;
- implement one-time hashed invitations with email binding and expiry;
- activate access only after both relationship acceptance and subject consent;
- add versioned domain/action grants, pause, expiry, and immediate revocation;
- default doctors/trainers to read-only patient facts and create separate
  `ProfessionalNote`/`CarePlan` records for their contributions.

Tests:

- role without relationship denies; relationship without consent denies;
- doctor/trainer defaults differ by action but not by domain — the original
  plan split them by domain too, and that was decided against: the separation
  between the two is that they are different people, not that one may see less;
- a professional cannot alter patient-origin facts or another professional's
  notes;
- revocation takes effect on the next service, web, file, job, and token action.

Rollback: relationship/grant rows may remain dormant; disabling the feature
removes all professional access without changing patient ownership.

### PR 08 — Professional UX and explicit patient context — **landed, minus the inbox**

Delivered:

- `/care` is the professional's roster and `/care/{subject_id}` one patient's
  record; `/settings/care` is the patient's side — who holds their record, what
  each sees, and pause/withdraw/end. `/care/accept/{token}` takes up an
  invitation, and a GET only asks: a one-time link must not be spent by a link
  preview.
- **The selected patient travels in the URL, never in the session.** That is the
  design rather than a URL style — a stale tab submitting an old form must land
  on the patient it was rendered for, not on whoever is selected now. Driven by
  a test that runs exactly that sequence. The patient's own routes resolve their
  subject from who they are, for the same underlying rule: the subject comes
  from whichever source cannot go stale.
- Every page says on what grounds it is open, and refusals are uniform — absent,
  no relationship, paused, lapsed are one 404.
- Two real defects fell out. The page chrome resolved its subject through the
  sole-owner adapter and threw on every request once a second person existed;
  it now resolves the signed-in account's own record, and a new handler turns a
  legacy route's refusal into a 409 instead of a 500. And htmx was caching a
  snapshot of every boosted page in `localStorage`, leaving somebody's record in
  the browser after the session ended.

- **The professional sees the record, not only the notes about it.** The policy
  always granted it — `default_scopes` returns every domain for both kinds,
  because the kind decides who is writing and not what may be read — but the
  screen showed notes and plans and nothing else. It now assembles the patient's
  record through `digest_service.assemble_context`, which is already the
  doctor's-report assembler, and renders it as per-domain summaries.

  Consent is applied as a **whitelist**, not through the module gate. The first
  attempt filtered by passing `enabled_modules`, which would not have shown up in
  a demo: `assemble_context` forces every *core* module on whatever it is handed
  — weight, labs and garmin among them — so a patient who withheld their weight
  would still have had it rendered. What the patient withheld is named rather
  than quietly absent; a clinician reasoning from a partial record has to know it
  is partial. The card is dated, because it shows the same closed period every
  report here uses and the latest reading can be a day behind the patient's own
  dashboard.

- **Navigation is what the account can actually reach.** Two defects, one behind
  the other. Every template response added by this PR omitted `username`, and
  `base.html` hides the entire chrome behind it — so the rail, the bottom bar and
  the sign-out button vanished on exactly the screens a doctor lives on, and a
  doctor redirected there had no way anywhere, including out. With the chrome
  back, the rail was offering Today, every module section, Share and Settings to
  an account with no record of its own, each of which bounces straight back.
  Those are now gated on `request.state.has_own_record`.

  Which is the rule the original scope called "modules ∩ role ∩ consent", arrived
  at from the other end: not about roles, but about whether the thing a link
  leads to exists for this reader.

Not done: the professional inbox. Invitations are found by their link, and
`care.invitations.accept` is explicit that **the token is what authorizes
reading the row at all** — so a list by email is not a missing screen but a
different security model, and one worth deciding on rather than implementing
sideways.

Per-patient narrowing of the chrome is also still open: the navigation is the
signed-in account's own, correct as far as it goes.

The compiled-CSS contract from the original test list now exists —
`tests/test_design_modifier_contract.py`, written from a real miss: a `v-dot`
modifier borrowed from another base rendered as the plain style, so a lab value
out of range looked exactly like one inside it. Accessibility and EN/RU parity
contracts are still untouched.

Original scope:

- add roster, pending invitation, consent center, and professional inbox;
- put opaque subject IDs in professional routes and HTMX form actions;
- display a persistent selected-subject banner and authorization basis;
- calculate navigation as enabled modules intersected with role and consent;
- clear/namespace browser state on logout and subject switch.

Tests:

- stale tabs and HTMX history cannot write to the previously selected subject;
- nav counts and partials reveal no PHI before patient selection;
- mobile and desktop accessibility, visible focus, 44px targets, reduced motion,
  EN/RU parity, and compiled CSS contracts.

Rollback: professional UI can be disabled while scoped APIs remain authoritative.

### Retiring the sole-subject gates — **the page paths are done**

Thirty-six live gates across fourteen service modules, each a compatibility
bridge that fail-closes on "more than one health subject in the database". They
were correct while a second subject meant a state nothing understood; PR-07 made
a second subject the point, and they are now what stands between a shared
installation and a working app.

They are being retired one at a time. Each guards something specific, and the
pattern that works is not to loosen the count but to find the *half* of the
operation that genuinely needs a sole subject and stop only that.

Retired so far:

- `legacy_ownership.resolve_legacy_ownership_context` — asked whether the
  database held one subject when the check below it already required the actor
  to *own* the subject. Selects the actor's own record now.
- `scoped_settings_service` — the shared `app_settings` key is what stops
  meaning anything with two people, not the scoped row. Reads fall through to
  the default, writes skip the mirror, adoption stops entirely.
- `alerts_service`'s fully-unowned bridge — it widens exactly one predicate,
  `subject_id IS NULL AND integration_connection_id IS NULL`, and demanded a
  sole subject whenever it was requested, including when no such row existed.
  Two questions had been fused: *is there an alert nobody owns* is what the
  bridge is for, and only if that is yes does *is this one person* have to hold.
  `_prepare_context` now answers the first under the governance lock it already
  takes, and returns the bridge that actually applies — `REJECT` when there is
  nothing to adopt. Callers use the returned value, which is the load-bearing
  part: skipping the proof while still widening the query is the one combination
  that could show one person another's alert. Opened `/nutrition`,
  `/supplements`, `/genetics`.
- `proactive.prefs.get_preferences_bundle` — not a count at all. A subject with
  no notification-policy rows was read as *corrupt* rather than
  *unconfigured*, and only the legacy owner's rows are seeded at startup, so
  `/settings` crashed for everybody else. Missing partitions now fall back to
  the defaults on the human read; the write paths still require all three,
  because there a missing one means a half-written split.

Two shapes of defect, and it is worth naming them separately:

1. **A gate that fires when it has nothing to guard.** The fix is never a
   loosened count. It is to ask what the bridge is *for* — is there anything
   unowned to adopt? — and to make the widening and the proof agree, so that
   when the answer is no, neither happens.
2. **A refusal with nowhere to land.** `/nutrition`, `/supplements`,
   `/skincare`, `/genetics` and `/settings` served 500 with a stack trace
   because their bridge exception was not registered on the handler. Refusing
   was right; arriving as a crash was not, and it sends whoever meets it looking
   for a bug that is not there.

- `conflict_engine`'s `FULLY_UNOWNED` bridge — the largest, at seven pages. It
  widens nine predicates, and **seven of them cannot match a row at head**: they
  test `subject_id IS NULL` on columns revision 0049 made `NOT NULL`, and the
  only schema where they are satisfiable is the pre-0049 one that
  `tests/schema_modes.py` builds on purpose. Two are live, both mixed catalogs
  where a NULL subject is a real state — an unclassified `conflict_rules` row
  (`code IS NULL`) and a non-curated unowned `hrt_compounds` row.
  The engine does not know its domains, so each domain registers its probe
  beside the widening it mirrors. `conflict_activation_service` had the same
  gate over the same rows and now shares the same probe. Opened `/labs`, `/hrt`,
  `/skincare`, `/glp1`, `/interactions`.
- `garmin_weight_service`'s outbox bridge — one predicate, on a `NOT NULL`
  column. Opened `/weight`, `/weight/measures`, `/settings`.
- `digest_service` and `share_service` — the last two. Opened `/today`,
  `/reports`, `/share`. Share already had a second proof written the right way,
  in its expired-report purge, which counts subjects only after finding null
  rows; that is what the other four should have looked like from the start.

An earlier note here said ten nullable tables made the conflict bridge hard.
That was reading the wrong ten. Migration 0051 already separates them: five are
shared catalogs where NULL means *the installation's own row*, and five are
inherited children where NULL means *not backfilled* — and those the row-security
policy already makes invisible to every bound session.

**Where this leaves a shared installation.** Twenty-five of twenty-seven pages
answer 200. `/settings/export` answers 409 because backup format v1 describes an
installation holding one person, which is the format speaking rather than a
gate, and it names the per-subject export that does work. `/external/summary`
answers 503 because the external API is switched off.

The way off every one of these bridges is the same and already shipped:
`scripts/backfill_*_subject_ownership.py`, run while the installation is still
one person, which is exactly when adopting an unowned row into that person is
the right thing to do. Afterwards the probe answers no and the bridge is inert.
A new commercial installation never had such rows and never pays the proof.

**Not a gate, and found the same way — signing in as a doctor.** Every personal
page told them the migration did not support several records yet. They keep no
health record of their own, so those pages have nothing to be about for them,
and the message sent them looking for a setting that will never exist. Behind it
was the real defect: the nav decided whether to offer the patient roster from
the chrome scope, which is `None` for anybody who owns no record — so the link
was hidden from precisely the people who have a roster, and a doctor signing in
had no way to reach their patients at all.

The working list is checked in, in two places. `web/main.py` turns each refusal
into a 409 and logs *which bridge* refused along with the route, so a running
shared installation names its own backlog. And
`tests/test_shared_installation_pages.py` walks every page against a two-subject
database, three times over: as the record's owner, and as an account with no
record of its own. One test asserts no page answers 500, one pins exactly which
pages still refuse — in both directions, so the list cannot go stale in either —
and one asserts that an account without a record is told that, and not something
else.

**Writes are covered now too.** Everything above walked pages with `GET`, and
`POST /settings/proactive` was the live gate underneath: it answered 409
through `LegacyProactivePreferencesBridgeClosedError`, so nobody on a shared
installation could save their own notification settings. Found by clicking Save
in a browser against the seeded installation — the suite could not see it,
because it never posted.

It was the same shape as the alerts bridge: the proof and the widening had been
fused. `prefs._lock_write_roots` demanded a sole subject for every caller, when
what a second person invalidates is only the shared `app_settings` mirror. It
reports the cardinality now; the mirror is skipped and the scoped row is
written, and the two callers that genuinely need exactly one subject — the
startup adoption of the legacy row, and the actorless startup read — refuse on
that answer themselves.

The scheduler was the second half and is worth naming separately, because it is
not a gate. `apply_schedule` rebuilds the one process-wide registry from
whatever was just saved, so on a shared installation the second person's Save
re-times the first person's brief. Startup had already decided this question —
it keeps the default schedule rather than faking one from somebody's row — and
`prefs.governs_the_process_schedule` now gives the save path the same answer.
The page says which half did not take effect rather than reporting a plain
"saved" that is true about the row and false about the effect.

`tests/test_shared_installation_pages.py` carries the write half in two sweeps.
The weak one posts empty bodies to every mutating route for the same
no-stack-trace property, and is honest that thirty-two of them stop at 422 with
the bridge two layers down never asked. The strong one carries a body each route
accepts: twenty-one domain write paths, asserted to work for the record's own
owner with somebody else in the database.

### PR 09 — Subject integrations, platform AI gateway, scheduler, and notifications — **partly landed**

Landed already, because the scheduler could not wait:

- **A scheduled job says whose record it runs for.** Every job arrived at the
  ownership resolver with neither an actor nor a subject, which means "the sole
  subject, or refuse" — so on a two-person installation the digest, the
  reminders and the nightly sweeps all stopped, fail-closed and invisible on
  screen. The resolver now has a second entry point whose subject is
  **mandatory**, and `vitals/scheduler/fanout.py` runs a job once per subject.
  One subject's failure is logged and does not stop the others; the tick still
  ends failed so the scheduler alerts.
- **Each run uses that subject's clock.** `health_subjects.timezone` had held the
  answer since the column existed and nothing read it: "today" came from
  `VITALS_TIMEZONE`. `nutrition_day_end` exists to run once a day's totals are
  final, and on the wrong clock it finalised a day still in progress. The same
  fix covers the request path, so a patient abroad sees their own date.
- **Telegram is gone**, which retired four of the eight jobs that could not be
  fanned out. See the CHANGELOG entry; the delivery journal survives for push.
- **The settings the removals left behind went too**, which is the half of a
  deletion that is easy to skip. Four `VITALS_TELEGRAM_*` variables nothing read;
  the evening block's time field, still in the stored policy where a strict
  decoder would have raised on it (revision 0059 rewrites the rows); a week
  template whose inputs had stopped being passed, so `/settings` rendered a
  heading over an empty box and still answered 200; a module gate that could only
  return `False` after its module left the registry. None of it was caught by a
  suite. All of it was visible on the page.

**Per-subject provider credentials landed**, which is what those four jobs were
waiting on. `integration_credentials` holds one Fernet ciphertext per connection
under the installation's `VITALS_CREDENTIAL_KEY`; `provider_credentials_service`
resolves a connection into a `Config` carrying that account's credentials, its
own token directory and the namespace its Redis keys hang off; and every
construction of a Garmin or Hevy client goes through it.

Three things are worth recording beyond the credential itself:

- **The credential was the obvious half.** The cached token session, the login
  breaker's counters, the `sync:last_success` marker and the disk token store
  were flat process-wide keys. Shared, they mean one person's session resuming
  as another's and one person's failed logins pausing everybody. All are
  namespaced by connection, the installation owner included — an exception there
  would have to be decided on every lookup from facts that are ambiguous on
  exactly the installations that matter, and it costs the owner one login on the
  first sync after the upgrade.
- **`legacy_env:` was written on every subject's roots.** The tenancy bootstrap
  did not know whose they were, so the ref said "my Garmin password is in
  `.env`" for patients whose password is not in there — the operator's is. Only
  the boot path reconciling `VITALS_AUTH_USERNAME` writes it now, and revision
  `0060` cleared it from every Garmin and Hevy connection it was never about.
- **Two jobs asked the wrong question first.** `garmin_service.sync_job` and
  `hevy_service.sync_job` built a client, and so answered "is this configured?",
  before anything had said whose record the run was for. Reordered.

**Every job about a record now runs once per record.** `daily_brief` and
`nudges` fan out per subject; the four provider jobs fan out per *connection*,
because a subject who has not connected a watch has nothing for them to do and
enumerating them would mean four scheduled no-ops a day per person.

`subject_id` is mandatory on every one of them, which is what
`vitals/legacy_scope.py` is a ratchet for: an omittable scope is exactly how
these jobs came to mean "the sole subject, or refuse". The MCP caller asking to
sync their own Garmin now gets `sync_now_for_actor`, resolving the record the
*actor* owns — two callers that meant different things and had been sharing one
argument. What stayed on the job is `actor_user_id`, which is attribution rather
than scope and is unset for a scheduled run.

The failure alert moved with them, and that is worth naming separately. The
shared runner recorded one outcome per *tick*, through a resolver asking for the
sole subject — right by accident while an installation was one person, and on a
two-person one a refusal the handler swallowed, so a failing sync raised nothing
at all while `/health` stayed green. `record_subject_job_outcome` takes a
mandatory subject and is called once per record by the fan-out; the runner keeps
only the platform-family jobs, which are about the installation's own state.

What is left in PR-09 is the notification transport. There is none: Telegram was
removed and web push has not landed, so the proactive layer composes on schedule
and every send resolves to "no endpoint".

**The shape of the rest.** `.env` should hold only what belongs to the
installation: the database and Redis, the session secret, the identity provider,
the AI gateway, endpoint addresses. Everything about a person belongs in the
database. Three of those are paid for now — the timezone was there all along;
the proactive schedule; and the profile, which is a subject-scoped setting with
every reader taking a subject.

The profile is worth recording as two defects rather than one, because only the
first looked like a defect. The five fields were printed on every patient's
report as though they were theirs, and were omitted for a while as a
placeholder. The second was the Navy formula: it takes a height and a sex, so
every patient's body-fat percentage and lean body mass were computed from the
installation owner's geometry, cached for the process, and a wrong number in a
medical record reads exactly like a right one. The rule that came out of it is
that **absent is not a default** — the estimate is skipped rather than computed
from half a profile, and the settings form renders empty boxes rather than
pre-filling somebody else's numbers.

What is left is integration credentials, and the authentication that is still
one username and one password hash.

Scope:

- create encrypted/reference-backed per-subject `IntegrationConnection` records
  for Garmin, Hevy, push subscriptions, and future subject providers;
- create one separately modeled `PlatformIntegrationConnection` for OpenRouter,
  administered only by active platform superadmins, plus subject-owned
  `AIInvocation` reservation/usage rows for every model call;
- namespace credentials, token stores, Redis keys, cursors, breakers, dedupe, and
  raw natural keys by subject/connection;
- enforce platform and per-subject AI budgets, model/purpose allowlists,
  idempotency, ambiguous paid-call outcomes, and usage reconciliation without
  storing PHI in the billing/control plane;
- replace global scheduled syncs with dispatchers that lease due connections;
- use per-connection locks/heartbeats and independent transactions;
- persist notification intents before provider I/O with explicit
  `pending`/`sent`/`ambiguous` outcomes and a reconciliation policy for timeouts
  after possible provider acceptance;
- keep notification previews free of PHI by default.

Tests:

- equal upstream IDs across connections remain isolated;
- a non-admin subject owner may use an allowed AI feature but cannot configure
  the gateway; a superadmin without subject authorization may configure the
  gateway but cannot compose prompts or read artifacts;
- concurrent quota reservations and retries buy at most one call, gateway
  rotation keeps the exact historical provenance of an already-dispatched call,
  and revocation before dispatch causes zero network calls;
- Daily Brief product identity remains stable across mutable model/prompt policy
  configuration, and a stale prepared model is cancelled without replacement;
- a failed connector does not roll back or block another connection;
- per-connection rate limits, leases, outbox claims, ambiguous-send recovery, and
  token directories cannot collide or double-send a confirmed intent;
- two timezones/DST boundaries schedule the correct subject-local day;
- no test calls a real provider or sends a real message.

Rollback: dual-read legacy credentials until each connection is verified. Copy,
do not remove, Garmin token material before successful cutover.

### PR 10 — MCP 2026-07-28, Python SDK v2, external API, reports, and LLM isolation

Protocol source of truth:

- [MCP specification 2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28);
- [MCP 2026-07-28 key changes](https://modelcontextprotocol.io/specification/2026-07-28/changelog);
- [official MCP Python SDK v2](https://py.sdk.modelcontextprotocol.io/).

The earlier label “MCP v2” meant Vitals' second-generation subject-aware
authorization model. It now also means a real wire-protocol migration to the
current `2026-07-28` standard. These are separate gates: using the new SDK does
not prove subject authorization, and scoped tokens do not prove protocol
compliance.

Scope:

- replace the pinned FastMCP compatibility server with an audited, exactly
  pinned release of the official Tier-1 Python MCP SDK v2 that supports protocol
  revision `2026-07-28`; keep the old dependency only until wire/tool parity is
  proven, then remove it rather than operating two authorities indefinitely;
- implement stateless, self-contained requests: carry
  `io.modelcontextprotocol/protocolVersion`, client capabilities, and client
  identity in request `_meta`; return server identity in result `_meta`; do not
  use protocol sessions, `Mcp-Session-Id`, or the removed
  `initialize`/`notifications/initialized` handshake as an authorization state;
- implement mandatory `server/discover`, per-request version negotiation, and
  typed `UnsupportedProtocolVersionError`; if the rollout temporarily serves
  `2025-11-25` clients, isolate that compatibility adapter and never let it
  bypass the same principal/policy checks;
- expose remote MCP over the `2026-07-28` Streamable HTTP POST contract with the
  required protocol/method/name headers; do not add deprecated HTTP+SSE, SSE
  resumption, request redelivery, Roots, Sampling, or protocol Logging to the new
  implementation;
- emit required `resultType`, deterministic tool/resource/prompt listings,
  private `cacheScope`, bounded `ttlMs`, JSON Schema 2020-12-compatible
  input/output schemas, and structured results whose PHI fields remain inside
  the authorized subject scope;
- use `subscriptions/listen` only for explicitly negotiated change
  notifications; use explicit server-minted handles for any cross-call workflow
  state, with subject, principal, expiry, replay, and revocation binding;
- issue short-lived OAuth/PAT tokens with stable user `sub`, subject, audience,
  `jti`, action/domain scopes, consent version, and revocation state;
- implement the MCP OAuth 2.1 protected-resource metadata flow, issuer-bound
  client credentials, authorization-response issuer validation, PKCE, and
  Client ID Metadata Documents; do not build new registration around deprecated
  OAuth Dynamic Client Registration;
- reuse the browser IdP for principal authentication only when its authorization
  server contract satisfies the MCP profile. If the pinned IdP does not advertise
  and safely validate Client ID Metadata Documents, put a narrow standards-based
  authorization adapter in front of it; never weaken the `2026-07-28` target to
  whatever vendor-specific endpoint happens to exist;
- treat metadata-document fetching as hostile SSRF input: HTTPS only, public
  destinations, bounded redirects/body/time, exact document URL/client-id match,
  strict redirect URI validation, and cache/revalidation rules are mandatory;
- propagate the principal into MCP tool context and re-authorize every call;
- make tool listing and direct invocation enforce the same module/policy rules;
- route MCP through core services and preserve `Source.MCP` plus actor;
- scope public reports, external summary APIs, sync quotas, exports, and LLM
  context; remove global snapshot/export tools from ordinary grants;
- never issue a roster-wide professional or superadmin MCP token.

Tests:

- run wire-level compliance tests through the official Python SDK v2 client and
  MCP Inspector fixtures, not only by calling decorated Python functions;
- prove `server/discover`, supported/unsupported version negotiation, required
  headers and `_meta`, stateless retry with a new request ID, `resultType`,
  deterministic listings, private cache metadata, and bounded pagination;
- prove the new endpoint works without `initialize` or `Mcp-Session-Id`, rejects
  removed/deprecated flows, and never treats a transport handle or subscription
  ID as authorization;
- prove OAuth protected-resource discovery, PKCE, audience/issuer/client binding,
  expiration, replay, consent and token revocation, and immediate user suspension;
- token A cannot select subject B, even with a known row ID or direct tool call;
- omitted-field partial updates retain data; writes retain provenance;
- consent/token revocation, module disabling, and user suspension take effect
  immediately;
- prompt/context snapshots contain one subject and bounded domains only.

Rollback: keep the legacy FastMCP endpoint disabled after identity/protocol
cutover; do not fall back to a global token that cannot express the subject.
During a bounded compatibility window, route an explicitly negotiated older
protocol revision through the same authorization services and audit stream;
never silently downgrade `2026-07-28` requests.

### PR 11 — Care-team messaging — **landed, minus attachments**

Delivered, as three tables and one service:

- `care_threads`, `care_thread_participants` and `care_messages` (revision
  `0061`). Each child carries its own `subject_id` with a composite foreign key
  back to `(thread, subject)` — a message filed under somebody else's thread
  would be invisible to its own patient and visible to another, and the
  constraint is why it cannot exist.
- **The subject is a participant from the moment a thread exists, and cannot be
  removed by anybody including themselves.** That is enforced in
  `remove_participant` rather than documented, and it is the difference between
  this feature and the hidden clinical channel the decision log rules out. The
  patient's own access needs no consent at all — `is_allowed` short-circuits on
  self-ownership — so "patient-visible" is structural rather than a promise.
- **Being in the room is a row, and it is not permission.** A participant row
  says somebody was let in and names the care relationship they joined under;
  whether they may act today is asked of the policy on every call. A paused
  consent stops the conversation without deleting it, and the patient keeps
  every word of it.
- **Reading and sending are separately revocable.** `care_team.message` is an
  operation with two actions — `read` and `message` — so a patient can let a
  doctor look back at what was said without being able to add to it.
  `PolicyAction.MESSAGE` and the `'message'` action in the `consent_scopes`
  check constraint had been in the vocabulary since it was laid down with no
  caller; this is the first.
- **Nothing is deleted.** A message is corrected in place by its author, keeping
  authorship and gaining an edit time; a participant who leaves keeps their row;
  a thread is closed rather than removed, and reopens.
- One set of screens. The professional reaches them from the patient's record;
  the patient reaches the same ones from `/messages`, which resolves their
  record from who they are. A separate patient-facing view of a clinical
  conversation is a place for the two to drift apart, and the argument for a
  patient-visible thread is that they cannot.

**Not done:** private attachments, and the reason is a real one rather than
scope. `GET /files/{opaque_key}` resolves its subject through the sole-owner
adapter, so a professional opening a patient's attachment gets a 404 that has
nothing to do with permission. Giving that route a policy-aware branch — or
hanging the download off `/care/{subject_id}/…` where the subject travels in the
path — is its own change with its own authorization story.

**No notification either**, which is PR-09's remaining gap rather than this
one's: the transport went with Telegram and web push has not landed, so a
message waits on the screen. The scope item about previews containing no PHI has
nothing to be about yet, and `AuditEvent`'s metadata allowlist already makes a
body impossible to put in one.

Original scope:

- add subject-owned threads, explicit participants, messages, and private
  attachments;
- include the subject as a visible participant by default;
- require active relationships and `care_team.message` consent at send and read;
- re-check authorization for notification delivery and attachment access.

Tests:

- participant removal/revocation blocks the next read and send;
- doctor and trainer cannot create a hidden thread without the subject;
- message edits/deletes retain authorship and audit history;
- outbound notification previews contain no PHI.

Rollback: disable sending, retain messages and audit history, and preserve
subject access to the existing record.

### PR 12 — Support console and repair workflow

Scope:

- expose operational dashboards without PHI;
- add step-up support grant request/approval/revoke flows and visible support
  sessions;
- implement read-only mode first, then separately reviewed repair actions;
- make export exceptional and independently approved;
- add subject-visible access history and incident retention controls.

Tests:

- superadmin without a live grant sees no PHI;
- wrong subject, expired grant, missing scope, stale MFA, and revoked grant deny;
- every sensitive read/write/export has one immutable, correlated audit event;
- repair captures actor/reason/bounded diff and cannot mutate unrelated rows.

Rollback: revoke all active support grants and disable the console; operational
administration remains available without PHI.

### PR 13 — Commercial readiness and registration opening

Scope:

- quotas, abuse controls, email delivery, account lifecycle, retention/deletion,
  billing hooks if selected, and operator runbooks;
- threat model, privacy model, terms/consent copy, incident response, key rotation,
  disaster recovery, backup encryption, and jurisdiction-specific review;
- load/failure testing and observability with redaction verification;
- switch registration from `disabled` only after every gate below passes.

Exit criteria:

- independent security review closes critical/high findings;
- migration rehearsal and restore rehearsal pass on production-like PostgreSQL;
- cross-surface isolation suite, RLS suite, revocation suite, and support-access
  suite are green;
- alerts, dashboards, logs, traces, backups, and analytics contain no unexpected
  PHI or secrets;
- rollback and incident runbooks are exercised, not merely written.

### PR 14 — Contract migration

Scope:

- make scope columns non-null and remove compatibility reads/dual writes,
  legacy settings, and any obsolete constraints explicitly deferred by the
  PR-03 scoped-key cutover, only after all previous releases are stable;
- publish a backup-required recovery path for deployments that now contain more
  than one subject.

Rollback: schema downgrade is allowed only where it is mathematically lossless.
Once multiple subjects exist, recovery uses a verified backup and forward fix;
it must never silently merge patients into a single-user schema.

## Required test matrix

The canonical synthetic fixture contains:

- member A and member B with identical dates and upstream identifiers;
- verified doctor D and trainer T, each with a self-owned subject;
- platform superadmin S with no standing patient access;
- active, expired, paused, revoked, and absent relationships/grants;
- two integration connections and two timezones, including a DST boundary.

Every relevant surface is tested for read, list/search, create, update, delete,
attach, share, export, and sync where applicable:

| Surface | Isolation/security assertions |
| --- | --- |
| Core services | Mandatory subject filter; no bare-ID IDOR; provenance and conflict resolvers scoped. |
| Web/HTMX | Explicit policy action, selected-subject binding, uniform not-found response, CSRF and stale-tab safety. |
| Files/uploads | Owner/subject/purpose binding, streaming caps, no key guessing or raw-payload rebinding. |
| MCP/PAT | Principal propagation, subject/scopes/audience/JTI/revoke, tool-list/direct-call parity, no global export. |
| Reports/LLM | One-subject bounded context, frozen report ownership, list/revoke/download isolation, redacted prompts/logs. |
| Integrations | Connection-scoped credentials/raw keys/cursors/breakers/outbox, no external calls in ordinary saves. |
| Scheduler/Redis | Per-connection leases/locks/budgets/dedupe; failure and timezone isolation. |
| Notifications | Recipient mapping, consent re-check, no PHI in a push preview, idempotency per recipient. |
| Portability | Subject-only export/import, no secrets/roles/consents/live links, never global wipe for a user. |
| Support | No role-only PHI, step-up/TTL/scope/revoke, immutable audit, repair diff, patient-visible history. |
| PostgreSQL | RLS, FORCE RLS, composite FK/unique/partial indexes, pooled context cleanup, concurrent revoke. |

Additional gates:

- Alembic upgrade/downgrade and model parity for every revision;
- real `0034` snapshot migration with row counts, checksums, provenance, raw links,
  frozen reports, and orphan checks;
- unit tests on SQLite plus PostgreSQL 15 integration tests for production-only
  behavior;
- concurrency/failure injection around consent revoke, scheduler leases, outbox,
  and connection-pool reuse;
- EN/RU copy parity and mobile/desktop accessibility for every UI PR;
- `ruff check .`, `git diff --check`, focused tests, then the full fast suite;
- provider clients remain fake in tests; no production credentials, health data,
  Telegram messages, or live Garmin/Hevy/OpenRouter calls.

## Release and review rules

- One concern per PR; schema expansion precedes behavior cutover.
- Registration stays `disabled` until PR 13 gates pass.
- Each PR updates this status, its decision log, `CHANGELOG.md`, and any affected
  security/architecture documentation.
- Each PR records commands actually run and distinguishes passed, skipped, and
  unavailable checks.
- A release with scope changes invalidates stale policy caches and tokens.
- Any newly discovered subject-owned table/tool/route/job is added to the
  ownership inventory and the parametrized isolation sweep before merge.

## Status checklist

- [x] Audit current models, web/auth, uploads, services, integrations, scheduler,
  exports, MCP, and existing tests for single-user assumptions.
- [x] Select isolated commercial fork and current upstream base without rewriting
  historical fork branches.
- [x] Merge PR 01 identity/support foundation.
- [x] Bootstrap current owner and introduce `AccessContext`.
- [x] Backfill subject ownership across the lake.
- [x] Pass cross-subject service isolation and PostgreSQL RLS gates.
- [x] Cut over OIDC authentication and per-user Vitals sessions/step-up state —
  PR-05. Registration is still closed, and closed by `registration_service`
  rather than by there being nowhere for an account to come from: four modes,
  `disabled` by default, and a deployment gate in front of the other three that
  is an environment variable rather than a settings page. A `HealthSubject` is
  born in `account_provisioning_service` and nowhere else; an operator reaches
  it through `scripts/provision_account.py`.
- [x] Isolate files, settings, portability — PR-06.
- [~] Isolate connectors, scheduler, and messaging — PR-09, all but the
  transport. Every job about a record runs once per record and on that record's
  own clock; the four provider jobs fan out per connection, and each account has
  its own credential, token store, session cache and login breaker. Messaging
  still has no transport: Telegram was removed and web push has not landed, so
  every send resolves to "no endpoint".
- [x] Add verified professionals, relationships, and consent — PR-07, with the
  professional UX in PR-08 (minus the inbox).
- [ ] Replace MCP/external auth with subject-scoped revocable grants.
- [x] Add the patient-visible care-team thread — PR-11, minus private
  attachments: the file download route resolves its subject through the
  sole-owner adapter, so a professional opening a patient's attachment gets a
  404 that is not about permission.
- [x] Ship the controlled support console and audit UX — read mode only; repair,
      export, operational dashboards and the break-glass path remain unbuilt.
- [ ] Complete commercial security/legal/operations review.
- [ ] Open registration.
- [ ] Run the final contract migration.

## Decision log

| Date | Decision | Rationale |
| --- | --- | --- |
| 2026-08-19 | Build commercialization in `LeMillion1/vitals`, not upstream `ilodezis/vitals`. | The upstream product and security model explicitly promise a single-user self-hosted service. |
| 2026-08-19 | Base commercial work on current `origin/master`; do not rewrite fork `master`. | The fork is behind, and its divergent patches are already represented upstream. A separate commercial base keeps history recoverable. |
| 2026-08-19 | Use actor + health subject, not a single `user_id`, as the core boundary. | Professionals act on another person's data; identity ownership and data subject are not interchangeable. |
| 2026-08-19 | Roles are additive; professional access requires relationship + consent. | A doctor/trainer may also be a member, and self-asserted roles must not expose PHI. |
| 2026-08-19 | Platform superadmins have no standing PHI access. | Support needs are real, but invisible impersonation or global MCP access is unacceptable. Scoped, time-limited grants preserve repair capability and accountability. |
| 2026-08-20 | OpenRouter is one superadmin-managed platform gateway, while every paid use is a subject-owned `AIInvocation`. | The platform pays for all users, but provider billing/configuration must not become a PHI grant. A separate platform root preserves subject-connection composite FKs and lets quotas, provenance, and no-standing-admin-access be enforced independently. |
| 2026-08-20 | Delegate login credentials and MFA to a self-hosted OIDC IdP; keep Vitals authorization local. | ZITADEL is the provisional default and Keycloak remains compatible, but neither an IdP role nor a token claim grants PHI directly. Vitals maps immutable issuer/subject identities into revocable local sessions and `AccessContext`; licensing, pinned-image operations, and MCP Client ID Metadata Document conformance remain release gates. |
| 2026-08-19 | Patient-visible care-team threads precede any hidden professional channel. | This is the safest useful communication model and avoids inventing a private clinical channel without product/legal approval. |
| 2026-08-19 | Registration is implemented only after isolation, then opened last. | A working signup form before complete subject isolation creates a direct health-data breach risk. |
| 2026-08-19 | Preserve legacy browser cookies through their existing TTL using a strict versioned compatibility envelope. | A flag-day logout is unnecessary in the bootstrap PR, but unknown token shapes and authorization facts in signed-readable cookies must fail closed. |
| 2026-08-19 | Treat password rotation as an explicit environment/DB dual-write until database auth cuts over. | Strict startup hash reconciliation would otherwise turn a legitimate settings change into a startup outage; compensation narrows the unavoidable file/database crash window. |
| 2026-08-19 | Separate nullable ownership expansion/backfill from the scoped-key cutover, and complete both before a second subject is writable. | Keeping a global unique constraint cannot permit the same date or upstream ID in two subjects. After scoped duplicate data exists, a downgrade to the global-key schema would be lossy and is forbidden. |
| 2026-08-21 | Keep Alembic schema-only and run each data-backfill phase through a fixed, bounded, resumable operator command. | A production lake rewrite must commit in reviewable batches, preserve deterministic evidence across restart, expose no PHI in operator output, and block schema downgrade once its durable checkpoint exists. |
| 2026-08-24 | Remove the Telegram transport, the `signals` domain and `day_context` outright rather than making them per-subject. | One bot token and one chat id in the environment is a single-user shape; a shared installation cannot have it. They were also why four scheduled jobs could not be fanned out. The delivery journal (`notification_delivery_intents`) is transport-agnostic by design and is kept as the seam web push plugs into. The cost is real and is recorded rather than hidden: nothing captures free text now, so the symptoms section of the doctor's report — the one thing no device produces — is empty. |
| 2026-08-24 | A feature removal includes its settings, in the same release. | Deleting a feature and deleting its knobs are two jobs and the suites only notice the first. What survived the removal above for two days: four `VITALS_TELEGRAM_*` variables nothing read; a week-template block whose inputs had stopped being passed, so `/settings` rendered a heading over an empty box and still answered 200; a module gate that could only return `False`; an AI prompt describing a context key that no longer exists. |
| 2026-08-24 | Retiring a field from a stored preference policy requires a data migration in the same revision. | `prefs._strict_object` compares a stored row's key set against the code's with `!=`, deliberately, because a preference that has drifted from the code is worth failing on. Removing `evening_time` from the code alone would have made every read raise on any installation that had ever saved its proactive settings. Revision `0059` rewrites the rows. |
| 2026-08-24 | A settings control whose effect is currently zero comes off the card; its stored value stays. | Quiet hours, the daily message budget and the nudge switches all gate a send, and there is nothing to send with. The delivery engine still reads the stored policy and a first web push has to be governed by something, so the handler now overlays only the fields the form still posts — `Form(default)` would otherwise silently reset what the owner last chose. |
| 2026-08-24 | The profile moves to the subject, and an unset field stays unset rather than taking a default. | `.env` held one age, sex, height, programme, goals and set of nutrition targets for however many patients an installation has. Two defects sat behind that, and only the first looked like one: the five fields were printed on every patient's report as though they were theirs, and the Navy formula computed every patient's body fat and lean mass from the installation owner's height and sex. The second is the reason for the rule about defaults — 190 cm, male, 18 is not a convenience for somebody who has said nothing, it is a claim about their body that a formula then turns into a number in a medical record. Nutrition targets are the deliberate exception: a target is a goal, not a fact about a body. |
| 2026-08-24 | Provider credentials are encrypted per connection in the database, and every Redis key and token path around them is namespaced by connection — the installation owner included. | One Garmin account and one Hevy key in `.env` is the single-user shape that kept four jobs from running per subject. The credential is only half of it: the cached token session, the login breaker and the token store were flat keys, so two subjects would share a session and one person's failed logins would pause everybody. Exempting the owner from the namespace to save them a login was considered and rejected — it makes "which subject is the owner" a question every lookup has to answer, and the available answers (creation order, a discriminator the demo seeder also writes) are ambiguous on exactly the installations this matters on. The cost is one login on the first sync after the upgrade. The vault is the first encrypted-at-rest store here; `VITALS_CREDENTIAL_KEY` belongs to the installation, and losing it costs every stored credential and no health data. |
| 2026-08-24 | The provider jobs fan out per *connection*, and a job failure is filed against the record it happened to. | Per connection rather than per subject because a subject who has not connected a watch has nothing for those four jobs to do; enumerating them would be four scheduled no-ops a day per person, and a failure alert for each would be an alert about nothing. The outcome recording moved for a sharper reason: the shared runner recorded one outcome per *tick* through a resolver asking for the sole subject, which was right by accident on a one-person installation and, on a two-person one, a refusal the handler swallowed — so a failing sync raised no alert at all while `/health` stayed green. `record_subject_job_outcome` takes a mandatory subject; the runner keeps only the platform-family jobs, which have no record to be about. |
| 2026-08-24 | The care-team conversation is one set of screens, read by the patient and the professional alike, and reading it is revocable separately from writing into it. | A separate patient-facing view of a clinical conversation is a place for the two to drift apart, and the whole argument for a patient-visible thread is that they cannot — so the patient reaches the professional's screens rather than a rendering of them. The two consent actions came from asking what a patient would actually want to narrow: "stop writing to me but you may still look back at what was said" is a real request, and one action could not express it. Participation is a stored row rather than derived from having an active relationship, because deriving it would let a doctor taken on last week silently join a conversation that predates them, and make a doctor whose care ended vanish from a history the patient can still read. |
| 2026-08-24 | Revision `0005` was edited after having been applied, to stop it seeding five unowned rows. | The alternative was a product that cannot be installed. `alembic upgrade head` is the container's start command; on an empty database revision `0049` refused because those five `skincare_products` rows can never get an owner — identity bootstrap runs after migrations. A later revision cannot fix it, because `0049` comes first and is what fails, and a conditional cannot either, because the ownership columns do not exist yet at `0005`. Removing an insert is a no-op for anyone who already ran the revision; the only behaviour that changes is the broken one. The general rule stands, and the exception is written into the migration's own docstring rather than a commit message. |

## Continuation protocol

At the start of a new implementation session:

1. Read this document and `AGENTS.md`.
2. Check the current branch, fork remotes, worktree, Alembic head, and this status
   checklist; preserve `.idea/`, `docs/local/`, and unrelated changes.
3. Inspect the previous PR's migration and recorded validation output.
4. Select exactly one next PR, mark it in progress, and keep registration closed.
5. Update the decision log when a security boundary or migration shape changes.
6. End with focused/full/PostgreSQL validation results and the precise next gate.
7. **Open a browser on the seeded shared installation.** `scripts/seed_care_demo.py`
   builds one and prints a session cookie per account;
   `tests/test_shared_installation_pages.py` walks the same ground, and the
   difference between them keeps being where the defects are. Five separate ones
   were found in the first minute of clicking and were invisible to several
   thousand passing tests: the app refusing to start, every page answering 409,
   Save on the notification card answering 409, the patient's consent page
   answering a bare 404 to a doctor, and the conversation page answering 500.

8. **Then run `pytest tests/ui -m ui`, and read what it photographs.** An HTTP
   pass finds pages that answer wrongly; it cannot see a page that answers 200
   and *shows* the wrong thing. Seven defects were found only that way and each
   had a green suite over it: a reply rendered above the message it answered,
   three refusals rendered as one unstyled sentence with no link out, a labs
   header reading "0 markers" above a table of two, a per-marker chart drawing
   an empty grid beside a value plainly in the table, "Today's meals · 1 приём"
   on an English page, two of three macro labels truncated to "Pr…" and "Ca…" on
   a phone, and a support console whose only link lived on a page its intended
   audience is refused from.

   The suite seeds a database and starts a server of its own, so it is one
   command and not a checklist. Locators live on page objects, a `Role` opens
   screens by name rather than by URL, and every load collects console errors,
   unexpected 4xx/5xx and horizontal overflow — a scenario that passes its own
   assertions and leaves a 500 in the network log still fails. A failing test
   leaves the screen and its markup in `.ui-failures/` and prints the paths.

   Skipped when Playwright or a Chromium is absent, and excluded from the
   default run because it takes ninety seconds. It is not optional: assertions
   about what a page answers had a clean record while all seven of the above
   were live.
