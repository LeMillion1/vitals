# Commercial Multi-user Roadmap

Status: active design and implementation plan

Last reviewed: 2026-08-19

Current implementation branch: `commercial/pr-03-subject-ownership`

Commercial base: current `origin/master`; publish it as a separate branch in
`vlakimov/vitals` instead of rewriting the fork's historical `master`

This document is the durable hand-off for turning Vitals from a single-user,
self-hosted application into a commercial multi-user service. It records the PR
sequence, security invariants, migration strategy, test gates, and decisions
that must survive across implementation sessions.

The existing fork's six divergent commits are patch-equivalent to changes that
already exist upstream. New commercial work therefore starts from the current
upstream head. The fork's `master` branch is not force-updated or otherwise
rewritten.

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

## Target domain model

| Concept | Purpose |
| --- | --- |
| `User` | Stable identity, normalized login/email, password hash, status, session version, MFA and verification state. |
| `UserRole` | Additive `member`, `doctor`, `trainer`, and `platform_superadmin` assignments with grant provenance. |
| `HealthSubject` | Owner of PHI, profile timezone, lifecycle state, and subject-scoped preferences. Usually self-owned, but not conflated with the acting user. |
| `ProfessionalProfile` | Doctor/trainer type, verification status, jurisdiction, and credential metadata. Self-selected intent never means verified access. |
| `CareRelationship` | Subject-to-professional invitation and lifecycle: proposed, accepted, active, paused, revoked, or expired. |
| `ConsentGrant` / scopes | Versioned subject consent for domain, action, purpose, time range, expiry, and revocation. |
| `AccessContext` | Boundary value containing principal, selected subject, role assignments, relationship, consent version/scopes, session/token, and optional support grant. |
| `IntegrationConnection` | Per-subject provider credentials/token reference, cursor, breaker, sync state, and lease namespace. |
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

### PR 03 — Subject ownership expansion and backfill

The canonical table-by-table contract is
`docs/COMMERCIAL_OWNERSHIP_INVENTORY.md`. It classifies all 55 current tables,
the missing ownership roots, natural keys, cross-surface dependencies, backfill
order, and rollback boundary. The runtime write-path contract is maintained in
`docs/COMMERCIAL_DUAL_WRITE_MATRIX.md`.

Implementation progress on `commercial/pr-03-subject-ownership`:

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
- Stage 2 dual-write, bounded backfill, validation, scoped-key cutover, and
  scoped-read/RLS cutover remain pending. Registration is still disabled.

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

### PR 04 — Scoped services, policy engine, and PostgreSQL RLS

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

### PR 05 — Database authentication, sessions, MFA, and closed registration

Scope:

- move login from environment-only identity to database users;
- store revocable auth sessions, per-user MFA factors/recovery codes, password
  reset, email verification, and rate-limit namespaces;
- rotate/invalidate legacy browser and MCP tokens at cutover;
- add `registration_mode = disabled | invite_only | admin_approved | open`, with
  every mode except `disabled` still feature-gated off initially;
- harden CSRF with tokens/Fetch Metadata for Internet-facing mutation routes.

Tests:

- session fixation, password change, user suspension, MFA replay, recovery, and
  session-version invalidation;
- no role escalation through registration form payloads;
- anonymous-surface contract remains explicit and fail closed.

Rollback: compatibility login is retained for one release behind an operator-only
emergency flag; rollback invalidates all new sessions rather than accepting both
credential models indefinitely.

### PR 06 — Private files, portability, and settings separation

Scope:

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

### PR 07 — Professional profiles, invitations, relationships, and consent

Scope:

- add professional verification states and operator verification workflow;
- implement one-time hashed invitations with email binding and expiry;
- activate access only after both relationship acceptance and subject consent;
- add versioned domain/action grants, pause, expiry, and immediate revocation;
- default doctors/trainers to read-only patient facts and create separate
  `ProfessionalNote`/`CarePlan` records for their contributions.

Tests:

- role without relationship denies; relationship without consent denies;
- doctor/trainer defaults differ by domain and action;
- a professional cannot alter patient-origin facts or another professional's
  notes;
- revocation takes effect on the next service, web, file, job, and token action.

Rollback: relationship/grant rows may remain dormant; disabling the feature
removes all professional access without changing patient ownership.

### PR 08 — Professional UX and explicit patient context

Scope:

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

### PR 09 — Per-subject integrations, scheduler, and notifications

Scope:

- create encrypted/reference-backed `IntegrationConnection` records for Garmin,
  Hevy, OpenRouter, Telegram recipients, and future providers;
- namespace credentials, token stores, Redis keys, cursors, breakers, dedupe, and
  raw natural keys by subject/connection;
- replace global scheduled syncs with dispatchers that lease due connections;
- use per-connection locks/heartbeats and independent transactions;
- keep Telegram notifications free of PHI by default.

Tests:

- equal upstream IDs across connections remain isolated;
- a failed connector does not roll back or block another connection;
- per-connection rate limits, leases, at-most-once outbox rules, and token
  directories cannot collide;
- two timezones/DST boundaries schedule the correct subject-local day;
- no test calls a real provider or sends a real message.

Rollback: dual-read legacy credentials until each connection is verified. Copy,
do not remove, Garmin token material before successful cutover.

### PR 10 — MCP v2, external API, reports, and LLM isolation

Scope:

- issue short-lived OAuth/PAT tokens with stable user `sub`, subject, audience,
  `jti`, action/domain scopes, consent version, and revocation state;
- propagate the principal into MCP tool context and re-authorize every call;
- make tool listing and direct invocation enforce the same module/policy rules;
- route MCP through core services and preserve `Source.MCP` plus actor;
- scope public reports, external summary APIs, sync quotas, exports, and LLM
  context; remove global snapshot/export tools from ordinary grants;
- never issue a roster-wide professional or superadmin MCP token.

Tests:

- token A cannot select subject B, even with a known row ID or direct tool call;
- omitted-field partial updates retain data; writes retain provenance;
- consent/token revocation, module disabling, and user suspension take effect
  immediately;
- prompt/context snapshots contain one subject and bounded domains only.

Rollback: keep MCP v1 disabled after identity cutover; do not fall back to a
global token that cannot express the subject.

### PR 11 — Care-team messaging

Scope:

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
| Notifications | Recipient mapping, consent re-check, no PHI in push/Telegram preview, idempotency per recipient. |
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
- [ ] Backfill subject ownership across the lake.
- [ ] Pass cross-subject service isolation and PostgreSQL RLS gates.
- [ ] Cut over database auth and per-user sessions/MFA.
- [ ] Isolate files, settings, portability, connectors, scheduler, and messaging.
- [ ] Add verified professionals, relationships, and consent.
- [ ] Replace MCP/external auth with subject-scoped revocable grants.
- [ ] Add the patient-visible care-team thread.
- [ ] Ship the controlled support console and audit UX.
- [ ] Complete commercial security/legal/operations review.
- [ ] Open registration.
- [ ] Run the final contract migration.

## Decision log

| Date | Decision | Rationale |
| --- | --- | --- |
| 2026-08-19 | Build commercialization in `vlakimov/vitals`, not upstream `ilodezis/vitals`. | The upstream product and security model explicitly promise a single-user self-hosted service. |
| 2026-08-19 | Base commercial work on current `origin/master`; do not rewrite fork `master`. | The fork is behind, and its divergent patches are already represented upstream. A separate commercial base keeps history recoverable. |
| 2026-08-19 | Use actor + health subject, not a single `user_id`, as the core boundary. | Professionals act on another person's data; identity ownership and data subject are not interchangeable. |
| 2026-08-19 | Roles are additive; professional access requires relationship + consent. | A doctor/trainer may also be a member, and self-asserted roles must not expose PHI. |
| 2026-08-19 | Platform superadmins have no standing PHI access. | Support needs are real, but invisible impersonation or global MCP access is unacceptable. Scoped, time-limited grants preserve repair capability and accountability. |
| 2026-08-19 | Patient-visible care-team threads precede any hidden professional channel. | This is the safest useful communication model and avoids inventing a private clinical channel without product/legal approval. |
| 2026-08-19 | Registration is implemented only after isolation, then opened last. | A working signup form before complete subject isolation creates a direct health-data breach risk. |
| 2026-08-19 | Preserve legacy browser cookies through their existing TTL using a strict versioned compatibility envelope. | A flag-day logout is unnecessary in the bootstrap PR, but unknown token shapes and authorization facts in signed-readable cookies must fail closed. |
| 2026-08-19 | Treat password rotation as an explicit environment/DB dual-write until database auth cuts over. | Strict startup hash reconciliation would otherwise turn a legitimate settings change into a startup outage; compensation narrows the unavoidable file/database crash window. |
| 2026-08-19 | Separate nullable ownership expansion/backfill from the scoped-key cutover, and complete both before a second subject is writable. | Keeping a global unique constraint cannot permit the same date or upstream ID in two subjects. After scoped duplicate data exists, a downgrade to the global-key schema would be lossy and is forbidden. |

## Continuation protocol

At the start of a new implementation session:

1. Read this document and `AGENTS.md`.
2. Check the current branch, fork remotes, worktree, Alembic head, and this status
   checklist; preserve `.idea/`, `docs/local/`, and unrelated changes.
3. Inspect the previous PR's migration and recorded validation output.
4. Select exactly one next PR, mark it in progress, and keep registration closed.
5. Update the decision log when a security boundary or migration shape changes.
6. End with focused/full/PostgreSQL validation results and the precise next gate.
