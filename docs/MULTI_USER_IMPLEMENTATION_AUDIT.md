# Multi-user implementation audit

Last verified: 2026-08-26, branch `commercial/main`.

This is the current-state companion to the commercial roadmap and ownership
cutover history. It separates shipped behavior from target design and records
the commands or source registries behind quantitative claims. No production
database, `.env`, upload, backup, credential, Garmin session, or OAuth token was
read during the audit.

## Executive verdict

The audited code transition is complete for the current release boundary.
Vitals now has local users and additive roles, one owned health subject per
patient account, subject-scoped facts, 71 PostgreSQL RLS tables, professional
relationships and consent, care-team conversations, OIDC login, revocable
browser sessions, per-subject integrations, and controlled read-only support
access.

This does not by itself authorize a public commercial launch. Registration
remains closed until the operator completes the external security, legal, and
operations review. Multi-subject installation backup, a professional invitation
inbox, and broader support repair and operations dashboards remain explicit
future roadmap items rather than hidden release claims.

The earlier documentation overstated completion in important areas. Those
findings remain useful provenance, but this document now records the later
remediations as shipped rather than leaving their former gaps in the current
verdict:

1. PR-10 protocol compatibility originally shipped without its target
   authorization model. Revision `0065` now binds connector credentials to one
   health subject, exact resource/action scopes, and—when a professional acts—a
   live relationship and immutable consent version. OAuth approval also makes
   the professional choose one currently authorized patient.
2. The OIDC and support claims were ahead of the routes. Partial OIDC config,
   the known first administrator password, local-only logout, unenforced browser
   session versions, and support actions without step-up were found in this
   audit. Commits `b6e3e91`, `7ff5271`, `c159b5d`, and `84a0631` close those
   specific gaps.
3. The ownership inventory called itself exhaustive but originally omitted 12
   of 76 live tables. It later reached 83 entries; five newer support,
   break-glass, and portability-receipt tables bring the live registry to 88 and
   are included in the inventory now.
4. `ARCHITECTURE.html` was a historical snapshot presented as a live reference.
   Its live counters and authorization bases have been resynchronized at 88
   tables, 71 RLS policies, revision `0080`, and the shipped break-glass path.

## Reproducible current facts

| Fact | Current evidence | Status |
| --- | --- | --- |
| Alembic head | `.venv/bin/python -m alembic heads` → `0080 (head)`; 80 files in `migrations/versions` | Verified |
| SQLAlchemy tables | `len(Base.metadata.tables)` → 88 | Verified |
| Ownership registry | `len(OWNERSHIP_REGISTRY)` → 88 | Verified and exhaustive in code |
| Subject-scoped tables | 71 metadata tables carry `subject_id`; the RLS revision union covers the same set | Verified |
| Required subject columns | 60 of the 71 `subject_id` columns are non-null | Verified |
| Ownership cutover | 18 ordered backfill phases and 18 matching scripts | Verified |
| Domain enum | 14 health domains | Verified |
| External integration modules | 5 tracked modules under `vitals/integrations` | Verified |
| Web routers | 34 tracked non-`__init__` modules under `web/routers` | Verified |
| Application services | 101 tracked non-`__init__` modules under `vitals/services`, recursively | Verified |
| Flat service debt | 54 tracked root modules under `vitals/services`; guarded against growth by `test_architecture_boundaries.py` | Verified, reduced by 20 |
| Browser scenarios | `pytest tests/ui -m ui -q` → 39 passed in 150.04s | Verified on the final runtime tree |

Historical pass counts in roadmap prose and HTML are not evidence for the
current commit. The final gate below records commands executed against the
final runtime tree; later commit `19e67f4` changes only a PostgreSQL corruption
test and was rerun in its complete shard.

## Delivered product boundaries

### Identity and subject ownership

- `users`, additive `user_roles`, `health_subjects`, immutable federated
  `(issuer, subject)` bindings, and session-version revocation exist.
- The bootstrap owner and additional operator-provisioned accounts can be bound
  explicitly to provider identities. Email and display name are not identity
  keys.
- Every federated protected request now checks the live active account and
  session version. Suspending an account or calling `revoke_all_sessions` closes
  the next request.
- Open registration remains a separate deployment-gated direct-provisioning
  path. Revision `0072` supplies
  constrained, purge-ready invitation and approval-request state, and
  `authentication.admission` implements their transactional domain lifecycles.
  The `invite_only` recipient now has a fragment-scrubbing browser exchange and
  fresh OIDC callback that atomically consumes the exact invitation. A protected
  operator screen shows redacted live invitations, returns a configured-origin
  link once, and revokes links even after closure. `admin_approved` now accepts
  only a verified unknown identity as a bounded member request, creates no
  session or account before a recent-auth superadmin decision, and exposes only
  a masked paginated queue. Approval rechecks the live mode and exact current
  issuer; old-provider requests remain reject-only. Hourly maintenance expires
  overdue proofs and scrubs terminal applicant PII after 90 days.

### Data isolation

- 71 tables are subject-scoped and covered by FORCE RLS in PostgreSQL.
- The application binds `vitals.subject_id` transaction-locally; unbound or
  wrong-subject sessions fail closed in PostgreSQL tests.
- Natural keys, raw payloads, normalized facts, integrations, settings, files,
  alerts, reports, and scheduled jobs have received the ownership cutover.
- PostgreSQL is still the only proof for RLS, partial indexes, JSONB, locking,
  and concurrency. SQLite tests are compatibility and explicit-scoping checks.

### Professional care

- Doctor/trainer profiles, operator verification, expiring email-bound
  invitations, relationships, versioned consent, subject-path rosters and
  records, notes, plans at the service/route layer, and patient-visible care
  threads exist.
- Accepting an invitation creates a relationship but no consent. That safety
  decision is correct. The professional now returns to the roster with an
  explicit waiting-for-sharing notice rather than entering a closed record.
  The roster and record resolver share the same expiry ceiling: a lapsed
  consent is labelled and cannot remain a link to a refused record.
- The patient screen now writes granular domain, authored-guidance, and message
  scopes; changing the selection creates a new immutable consent version. Plan
  creation, author-only activation and archival are visible, and notes/plans
  name their author.
- A new conversation now takes its topic and first message together. Each
  participant has an independent read cursor, and only newer messages from
  somebody else count as unread. The professional roster is the cross-patient
  inbox: unread records come first with a direct conversation action and recent
  message date. An accepted invitation now becomes a persistent patient task
  until the first consent decision. Patient desktop and phone navigation expose
  only the unread count and a direct conversation door; no sender, title or body
  reaches shared chrome. A message may carry one validated PDF or image: bytes
  live outside `/static`, composite foreign keys bind metadata to the same
  subject and message, and every download rechecks participation, live care,
  and consent. The legacy owner-only `/files` route was not widened.

### Controlled support

- A platform administrator requests named record sections for a reason and a
  bounded TTL; the patient may approve, decline, or revoke; the holder may hand
  access back. Read authorization is subject-, grantee-, status-, time-, mode-,
  and scope-specific.
- Every support mutation now requires authentication performed within the last
  15 minutes. OIDC step-up uses `prompt=login` and validates the returned
  `auth_time`; legacy mode clears the stale cookie and requires login again.
- The operator console opens one exact grant. The selected ID is rechecked
  against subject, holder, role, status, expiry, and scopes and is carried to
  the immutable read event. Multiple grants held by the same administrator are
  never merged or guessed between; ambiguous and invalid direct URLs fail
  before PHI is queried. A dual-role doctor/administrator keeps the professional
  path unless an explicit support link is selected.
- Every successful support-granted record response now commits one PHI-free,
  grant-correlated read event before medical HTML is returned. The patient's
  access centre shows the operator, exact local timestamp, and approved scopes
  derived from the subject-protected grant. The audit envelope contains no
  health-category list. Care and patient/admin support actions bind one exact
  subject before reaching PostgreSQL FORCE-RLS tables.
- The care record uses a dedicated strict projection instead of assembling a
  full digest and filtering its output. Only domains allowed by the live
  relationship consent or support grant and enabled by the patient reach their
  loaders; negative SQL tests cover withheld core and optional tables. Its
  report window follows the patient's timezone, every collection is bounded,
  truncation is explicit, Weight never selects raw JSON or notes, and latest
  lab results are ranked per marker before the alert view is assembled.
  Support receives only a neutral restricted-opening notice and cannot infer
  the names of enabled but ungranted modules.
- Body-composition and genetics still use their deep provenance validators.
  They therefore may inspect raw data inside the already granted domain to
  prove the normalized fact, although the projection renders only a bounded
  summary and genetics caches each shared VCF parse. Metadata-only validators
  remain a worthwhile performance hardening, not an authorization gap.
- Exceptional export is now a separate fixed operation rather than a widened
  read grant. The patient approves a two-hour, one-use personal portability
  download; the exact grant is locked and consumed with a PHI-free audit event
  before bytes leave, and no export artifact is stored. Invalid, widened,
  expired, revoked, consumed, or ambiguous grants fail before portability reads.
- One fixed reversible repair and an independent break-glass path are shipped.
  The repair requires a patient-approved exact grant, a separate patient review,
  stale-state checks, and retains its history; break-glass requires two active
  administrators other than the holder, exact read-only domains, a short TTL,
  patient transparency, and immediate patient revocation. Broader repair,
  operational dashboards, and retention tooling remain absent.
- Every simultaneous live support grant is now visible and independently
  revocable on the patient's access page, with the holder, approved sections,
  and exact local expiry. Shared chrome exposes only the active count and a
  neutral management link. Patient request history derives effective expiry
  directly from the database clock without writing on GET, prioritizes requests
  that still need an answer, and attributes live, natural-expiry, owner-revoked,
  and holder-returned grant lifecycles with exact local timestamps.

### OIDC

- Authorization Code + PKCE S256, state, nonce, issuer, audience, signature,
  token time, and optional authentication-freshness validation exist.
- The complete four-variable OIDC group is a hard cutover; a partial group now
  fails startup rather than silently leaving password login enabled.
- The optional ZITADEL profile now requires an explicit 32-character master
  key, database password, and non-default first administrator password.
- Logout clears the local cookie and redirects through the discovered
  `end_session_endpoint` with the registered client and post-logout URI.
- Provider outage still permits local logout. This is the fail-safe result.

### MCP and external access

- The MCP v2 wire protocol, official SDK compatibility, version negotiation,
  client metadata documents, SSRF controls, token `jti` revocation records,
  per-subject external API credentials, and subject-bounded tool execution are
  real.
- MCP tokens live for at most 365 days and bind to one account, client,
  audience, issuer, health subject, and a frozen set of concrete
  resource/action scopes. Professional tokens additionally bind the live care
  relationship, consent row, and consent version; each request rechecks them.
  All 69 tools have an explicit capability classification, and tool discovery
  and direct invocation enforce the same decision.
- MCP access tokens now validate both `aud` and `iss`; registry-backed tokens
  cannot omit either installation-binding claim.

### Portability

- The personal JSON export and import are subject-bound and exclude identity,
  roles, consent, support/audit state, credentials, and file bytes.
- The audit found that v1 also suppressed mandatory
  `integration_connection_id` and `file_asset_id` references while still
  carrying their dependent provider/photo rows. Such files could be exported
  but not restored. The schema-derived boundary now refuses those rows on
  export and rejects crafted/older files before deleting any existing data;
  both self-service HTTP paths return controlled errors instead of a late 500.
- V1 remains fail-closed containment rather than complete portability.
- Personal portability v2 is shipped as an owner-only encrypted and authenticated
  archive. It carries private resources and validated connection descriptors,
  requires an explicit mapping to live same-subject connections, stages private
  files with byte validation, replaces the record and writes an exact replay
  receipt in one coordinator-owned transaction, and reconciles an uncertain
  commit through that authoritative receipt. It transports neither credentials
  nor a plaintext spool.
- Multi-subject full installation backup and restore remain unimplemented; v2 is
  a personal-record format, not an installation backup.

## Documentation file verdicts

### `COMMERCIAL_MULTI_USER_ROADMAP.md` — useful target, unreliable status

The architectural principles and phased threat model are valuable. PR-01–09,
PR-11 without attachments, and PR-12 read mode broadly correspond to real code.
PR-10 previously called itself merged, next, and complete while its central
grant checkbox was still open. That contradiction is now resolved by the
`0065` credential cutover. “Merged PR” still means a logical phase, not a
verifiable GitHub pull request: the commercial history has only one merge
commit. Keep this document as target/decision history and use this audit for the
current state.

The following roadmap targets are not shipped: the deliberately deferred
professional invitation inbox, multi-subject full backup, broader support repair
and operational dashboards, commercial registration opening, and final
security/legal/operations readiness. The first repair, break-glass, canonical lab
marker migration, private-byte relocation, and personal portability v2 are live.

### `COMMERCIAL_OWNERSHIP_INVENTORY.md` — strong rationale, now exhaustive

The provenance rules, ownership classes, raw-first policy, scoped natural-key
reasoning, composite-FK rules, and historical cutover analysis are strong. The
machine registry is the source of truth and is complete.

The prose inventory previously listed only 64 live and two dropped tables. It
now includes the missing identity, professional-care, support-request, and
credential rows, the MCP scope child, the care attachment row, the web-push
subscription, care-push-delivery, professional-review rows, support repair,
break-glass, and portability receipts and matches all 88 live tables in the
machine registry.

The historical Stage 3/4/5 narrative should be archived separately from a
generated current registry table.

### `COMMERCIAL_DUAL_WRITE_MATRIX.md` — valid historical dossier

Its introduction correctly says it is historical after migrations 0049/0050.
The migration reasoning and write-path inventory remain useful. Later present-
tense statements about global keys, FastMCP v1, exact-one scheduling, opaque
asset URLs, and unscoped charts/reports are stale. Treat it as a versioned
cutover dossier, never as runtime status.

### `OWNERSHIP_CUTOVER_RUNBOOK.md` — corrected during this audit

The 18-phase order, checkpoint model, 0049 non-null guard, 0050+ FORCE RLS, and
fresh-install migration path are real. The runbook now says FastAPI lifespan,
not “the first request”, performs normal bootstrap and no longer tells an
operator to start current runtime code on the intermediate 0048 schema. The
bounded `scripts/bootstrap_ownership_roots.py` command creates only the owner,
resource roots and checked-in catalogs, commits once, and exits without the
scheduler or external integrations. A rehearsal on the real production lake is
still not proved by a local text-order test.

### `OIDC_SETUP.md` — corrected during this audit

The document now names every required IDP/application secret, fails partial
application configuration closed, replaces the known first administrator
password, describes the pinned v2.66.0 Apache-2.0 license accurately, documents
explicit account linking, provider logout, browser-session revocation, and
support step-up. Provider-specific backup/restore and upgrade rehearsal remain
operator work that local tests cannot prove.

### `ARCHITECTURE.md` — mostly current method, incomplete generated sources

Its reproducible-counter idea is right. The RLS source list and HTML counters
are synchronized through revision `0080`. It documents the first bounded-context
moves, but the live flat-service debt is still substantial.

### `ARCHITECTURE.html` — synchronized during this audit

The displayed schema, migration, domain, router, application-service, RLS,
ownership-class, authorization-basis, PR-05, and PR-07 facts now match the
current registries. They remain hand-maintained duplicates; generating them from
code is still safer than relying on future reviewers to update every copy.

### `DESIGN_SYSTEM.md` — strongest live document

The Masthead shell, tokens, responsive rules, no-raw-hex/no-monospace policy,
touch targets, focus treatment, and mobile-table contracts have direct tests.
The document is too long. It defines the shared patient/professional care
conversation and attachment pattern, but controlled support, registration, and
break-glass interaction patterns are still absent. Split normative rules from
historical rationale and add those role journeys as first-class patterns.

### `known-good-deps.txt` — correctly scoped snapshot

Its header says it is a production snapshot from 2026-07-28, not a lockfile.
Differences from current requirements are therefore expected. The claim that it
matches a running production image cannot be verified locally and was not used
as current evidence.

## UI/browser findings

The Compose application redirected correctly to its configured local ZITADEL.
The IdP originally advertised public self-registration even though Vitals
registration is closed, producing an orphan-account dead end. Fresh instances
now disable that upstream default and the runbook covers existing databases.
Its Russian copy still contains typos, mobile controls are below the project's
44 px target, and the registration back
icon lacks an accessible name.

The first live patient consent screen also rendered each shared domain three
times because it exposed the underlying read/list/search scope rows, and used
the missing `nav.milestones` key. It now collapses policy actions into one
human record-section summary and uses the dedicated consent translations.

The live professional thread route stacked the new-thread form, the complete
thread list, and the open conversation vertically. It now separates the list
and creation task from the focused conversation screen, with an explicit route
back, so the doctor or trainer does not scroll past two unrelated tasks before
reading a reply.

Care guidance and the live conversation also exposed account usernames as the
visible author and participant names. They now resolve professional profile
display names and the patient's record display name, retaining usernames only
as a fallback for an incomplete professional profile.

The professional shell also disappeared entirely below 768 px because hiding
the patient's five-slot record navigation had no role-specific replacement.
Professionals without their own record now keep a compact Patients/Support and
Sign out bar, matching the destinations the desktop rail already exposed.

At that historical UI checkpoint, the first shared run reported 25 passed and
10 failures after a revoked support-record navigation timed out; the remaining
nine were cascading server timeouts. The product flow passed in isolation. The
deterministic full-run cause was the UI fixture piping Uvicorn access logs without
draining the pipe: once the OS buffer filled, the server blocked while handling
a request. The fixture
now writes its synthetic server log to the run's temporary directory, and that
checkpoint's complete suite passed 35 scenarios in 93.49 seconds. The current
collection contains 39 scenarios; this historical timing is not a current gate.

The largest remaining confirmed UX gaps are low-contrast `--faint` microcopy
outside the touched care screens and roster states beyond conversation urgency.

## Architecture status and next moves

The core does not import FastAPI or `web`, and the import-time graph is acyclic.
The care domain now owns six related services, authentication owns its protocol,
admission, session, provisioning, and credential boundaries, analytics moved out
of the application-service layer, and RLS/transaction primitives moved to
`vitals.persistence`. Static
tests prevent core→web, services→operations, pure-analytics→I/O, flat-service
growth, and import-time cycles.

The ownership correction is now delivered. Eighteen resumable backfill programs,
ownership validation, and scoped-key audit live in
`vitals/operations/ownership`. The destructive full-v1 portability restore binds
those programs in an operational coordinator. Share and weight read the two
remaining historical checkpoint projections through a non-mutating
`vitals/ownership_transition` seam, so no application service imports an
operation and no compatibility forwarding modules preserve the old flat paths.
The architecture test fixes that direction and lowered the flat-service ceiling
from 74 modules to 54.

Continue moving bounded contexts in dependency order and split
the 2,000–4,000-line monoliths under characterization tests. Delivery adapters
(`mcp.py`, settings, care/consent routers) should become thin only after the
application-service APIs stabilize.

## Evidence executed during the audit

### Final release-boundary verification (2026-08-26)

The final runtime tree at `619c99b`, followed by the test-only PostgreSQL
fixture correction at `19e67f4`, passed the release gate:

- the full fast suite passed 5,425 tests, skipped 188, and deselected 39 in
  540.06 seconds;
- the complete live browser suite passed all 39 scenarios in 150.04 seconds;
- the PostgreSQL 15 suite passed all 5,613 selected tests across three isolated
  shards: 1,655 on shard 0, 2,197 on shard 1, and 1,761 on shard 2. Every shard
  started from an empty database and completed
  `base → 0080 → 0034 → 0080`. Shard 0 was repeated in full on `19e67f4` after
  PostgreSQL exposed four invalid historical-corruption fixtures; the corrected
  run had zero failures or errors;
- `ruff check .` passed on the runtime tree, focused Ruff passed after the
  test-only correction, and `git diff --check` passed;
- Compose rebuilt successfully with PostgreSQL and Redis healthy, exactly one
  loopback application mapping (`127.0.0.1:8100→8000`), `/health` reporting the
  database, Redis, and scheduler healthy, and Alembic at `0080 (head)`;
- live authenticated browser checks covered patient, doctor, trainer, and
  owner/operator journeys at desktop and 390 px phone width. Patient and
  professional conversations used role-relative sender labels and clear
  audience-specific composers; doctor and trainer record/conversation flows
  had no horizontal overflow; registration administration stayed closed; and
  the fresh-auth break-glass console accepted only an exact opaque subject ID,
  short TTL, and explicit read-only domains without enumerating patients or
  creating an emergency session.

The Compose/browser checks used only synthetic local accounts and a disposable
test identity bridge. No production data, credential, provider API, or message
transport was used.

Post-audit credential cutover validation on `7523d26`:

- 227 focused MCP tests passed for the subject/scope/consent token cutover;
- migration `0065` upgraded, downgraded, and upgraded again on PostgreSQL; 53
  focused PostgreSQL RLS/MCP tests passed;
- 518 focused OAuth, i18n, design, static, router, and mobile tests passed;
- the full fast suite passed 4,634 tests, skipped 168, and deselected 35;
- Tailwind rebuilt, `ruff check .` and `git diff --check` passed;
- Compose rebuilt at head and `/health` returned 200; the OAuth patient picker
  was inspected at 1440×900 and 390×844 with no browser console errors.

Post-audit care unread cutover validation on the `0066` tree:

- 59 focused care-thread, care-UI, and i18n tests passed; the broader UI/static
  contract selection passed 401 tests;
- a fresh PostgreSQL 15 instance completed `head → 0034 → head`, then 54 focused
  care and RLS tests passed;
- the full fast suite passed 4,637 tests, skipped 168, and deselected 35;
- Compose upgraded the retained synthetic stack to `0066`, `/health` returned
  200, and the unread-first conversation list was inspected at desktop and
  phone widths with no browser console errors.

The follow-up consent-expiry repair passed 64 focused care/i18n tests, 479
care/UI/auth/design tests, and the full 4,638-test fast suite (168 skipped, 35
deselected). Its roster state was inspected at desktop and 390 px phone widths
with no horizontal overflow or browser console errors.

The cross-patient inbox then passed 85 focused care tests, 492 care/UI/design
contracts, 80 PostgreSQL care/RLS tests after the full migration cycle, and the
full 4,639-test fast suite (168 skipped, 35 deselected). Its unread-first roster
was inspected at desktop and 390 px phone widths with no horizontal overflow or
browser console errors.

The accepted-invitation patient task passed 168 focused care/i18n/auth tests,
476 care/UI/auth/design tests, 48 PostgreSQL care tests after the full migration
cycle, and the full 4,641-test fast suite (168 skipped, 35 deselected). Its
global banner was inspected at desktop and 390 px phone widths; the phone action
is 44 px high, with no horizontal overflow or browser console errors.

The patient unread-navigation wiring passed 310 focused care/nav/mobile tests,
411 care/nav/UI/design contracts, and the unchanged full 4,641-test fast suite.
Desktop rail and phone More states were inspected at 390 px with no horizontal
overflow or browser console errors; shared chrome contained only the count.

Private care-message attachments on revision `0067` passed 548 focused
care/UI/static/design tests. A fresh PostgreSQL 15 instance completed the full
`head → 0034 → head` cycle and 199 care/RLS/schema tests; the full fast suite
passed 4,654 tests (168 skipped, 35 deselected). A clean synthetic Compose stack
built the image without the repository `.env`, migrated to `0067`, exposed a
writable `/data/private_files` volume outside static storage, and returned 200
from `/health` with PostgreSQL, Redis, and scheduler healthy; the temporary
containers, volumes, network, and image were then removed. `sh -n`, Tailwind,
full Ruff, and diff checks passed. The closed and
open attachment composer plus an existing download action were inspected at
1440×900 and 390×844. The phone page had no horizontal overflow
(`scrollWidth == clientWidth == 390`).

Account/device Web Push enrollment on revision `0068` then passed 508 focused
push, care, CSRF, anonymous-surface, i18n, design, router, mobile, and web tests;
the full fast suite passed 4,686 tests (168 skipped, 35 deselected), Tailwind
rebuilt with the isolated Node runtime, and full Ruff and diff checks passed.
The authenticated API exposes only availability and the public VAPID key, caps
streamed JSON at 8 KiB, and returns no endpoint, account, subscription, or
patient identifier. Patient inbox and doctor roster were inspected live at
desktop width, then the patient inbox at 390 px with no horizontal overflow.
That browser run caught and fixed a real cascade error where `.v-btn-ghost`
overrode Tailwind `.hidden`; denied-permission controls now compute to
`display:none`. No permission prompt was invoked.

The subject-isolated care-push outbox on revision `0069` passed 28 focused fast
outbox/ownership/RLS tests. A fresh PostgreSQL 15 instance completed the full
`head → 0034 → head` migration cycle and all 36 selected outbox/ownership/RLS
tests, including FORCE RLS and both subject- and account-equality composite
foreign keys. The full fast suite passed 4,696 tests (170 skipped, 35 UI
deselected); focused and full Ruff plus diff checks passed. No sender, network
transport, scheduler job, or service-worker notification content was enabled by
this revision.

The generic Web Push transport then passed 71 focused push, architecture, and
anonymous-surface tests and the full fast suite passed 4,727 tests (170 skipped,
35 UI deselected). Full Ruff and diff checks passed. A clean Python 3.13 Docker
image installed the exact `pywebpush 2.4.0` dependency, imported the adapter,
and completed real ECE encryption plus VAPID signing against a fake in-container
session; the captured request contained no plaintext and had redirects disabled,
while the raw provider body remained unread and its response was closed. The
temporary verification image was removed. It made no real provider request.

The consent-rechecking care dispatcher on revision `0070` then added a bounded
15-second shared-scheduler job. Claims lock with `SKIP LOCKED`, match a
professional's exact participant relationship and current role/consent, commit
before network I/O, and terminalize every trustworthy or uncertain outcome
without retry. Provider-gone cleanup compares the encrypted subscription
generation, so a late response cannot erase a browser that re-enrolled while
the request was in flight. Claims are HMAC-sealed, one-shot capabilities that
activate only after their exact outer transaction commits; savepoint commits
cannot release a credential to the network. The final dispatcher file passed
24 fast tests (one PostgreSQL-only skip), the full fast suite passed 4,751 tests
(171 skipped, 35 UI deselected), and PostgreSQL 15 completed the real
`head → 0034 → head` cycle plus 74 dispatcher/outbox/RLS/scheduler tests.
The root-scoped worker now accepts only the exact generic wakeup, selects
catalog-backed RU/EN copy from a locale learned only after device ownership is
proved, coalesces it under a non-PHI tag, and opens only `/messages`.
Its executable Node harness plus push/i18n/design/security contracts passed 87
focused tests, and the full fast suite advanced to 4,754 passed (171 skipped,
35 UI deselected). The existing Compose stack rebuilt on revision `0070` and
reported PostgreSQL, Redis, and the scheduler healthy. A live browser confirmed
that `/sw.js` was active at root scope, controlled the page, emitted no console
warning, and did not change the existing notification-permission state.

The `invite_only` recipient handoff then passed 599 focused OIDC, admission,
anonymous-surface, i18n, design, static, router, mobile, and web tests (nine
PostgreSQL-only skips). PostgreSQL 15 completed `head → 0034 → head` and all 160
selected admission/federation/RLS tests, including concurrent raw-bearer and
signed-claim consumption. The full fast suite passed 4,954 tests (183 skipped,
35 UI deselected), full Ruff passed, and Tailwind rebuilt from the selected Node
runtime. Compose rebuilt the application at revision `0072`; `/health` reported
the database, Redis, and scheduler healthy, and Alembic reported `0072 (head)`.
A live browser followed a synthetic `#token=...` link from another document,
removed the fragment, left the bearer absent from both URL and HTML, exchanged
it, and reached the ZITADEL fresh login; focused route tests separately proved
that the exchange clears old local authentication handles. A separate executable
JS failure harness forced `history.replaceState` to throw and observed zero
exchange requests. Synthetic invitations were then
revoked through the domain service and the test stack returned to `disabled`.

The operator invitation console on revision `0073` then passed 683 focused
admission, OIDC, support-access, i18n, design, static, router, mobile, and web
tests (nine PostgreSQL-only skips). The full fast suite passed 4,978 tests (183
skipped, 35 UI deselected), full Ruff and diff checks passed, and Tailwind
rebuilt without a generated CSS change. PostgreSQL 15 completed the real
`head → 0034 → head` migration cycle and all 74 final invitation-console,
admission-model, and migration tests; a broader pre-final admission/federation/
RLS selection had passed 142 tests. Compose rebuilt the application at revision
`0073`; `/health` reported the database, Redis, and scheduler healthy. A live
desktop browser confirmed the protected operator page, masked recipient labels,
unambiguous invitation references, and revoke controls. An older SSO session
was correctly redirected to fresh step-up before an issue mutation. The test
IdP password was unavailable, so that manual mutation was not completed in the
browser; focused HTTP tests proved issue, durable refresh/restart replay,
pagination, and revoke behavior on SQLite and PostgreSQL instead. Synthetic
roles and invitations were removed, registration returned to `disabled`, and
the original Compose configuration was restored. Phone-width behavior remains
covered by the mobile contracts rather than a live viewport capture in this
run.

The scheduled account-admission retention slice on revision `0074` passed 178
focused admission, model, migration, scheduler, RLS, and alert tests (17
PostgreSQL-only skips). The full fast suite passed 4,981 tests (183 skipped, 35
UI deselected), and full Ruff and diff checks passed. PostgreSQL 15 completed
the real `head → 0034 → head` cycle and all 167 selected retention, admission,
scheduler, and RLS tests. Compose rebuilt the current application, Alembic
reported `0074 (head)`, `/health` reported the database, Redis, and scheduler
healthy, and container introspection confirmed the hourly platform-classified
job with the shared 300-second lock. The slice changes no browser UI.

The professional-review navigation fix then passed 466 focused review, OIDC,
i18n, design, static, router, and mobile tests. Tailwind rebuilt without a
generated CSS change, focused Ruff and diff checks passed, and the unchanged
full fast gate remained at 4,981 passed (183 skipped, 35 UI deselected). All
four recent-auth review forms now use full browser POST navigation.

The strict care-projection hardening then passed 217 focused domain tests (six
skipped) and 415 support/i18n/design/static/mobile contracts. A separate
care/support selection passed 105 tests (three PostgreSQL-only skips). Ruff and
diff checks passed, and Tailwind rebuilt through the pinned local Node 24
runtime without a generated CSS change. PostgreSQL and Compose are rerun at the
combined controlled-support gate below rather than claimed by this query-only
slice.

Exact same-administrator grant selection then passed 102 focused access,
relationship, support, and templating tests (three PostgreSQL-only skips), plus
focused Ruff and diff checks. The new PostgreSQL RLS selector case is included
in the combined controlled-support gate rather than reported as a local pass.

The combined controlled-support gate completed the real PostgreSQL 15
`head → 0034 → head` migration cycle and passed 124 selected RLS, support,
projection, and relationship tests. The full fast suite passed 5,057 tests,
skipped 187, and deselected 37; full Ruff and diff checks passed. The complete
38-scenario browser suite exercised patient, doctor, trainer, support, and
administrator journeys, including two disjoint grants held by the same
administrator and opened through distinct exact links. Compose rebuilt the
current image, PostgreSQL reported healthy, `/health` returned 200, and `/`
redirected 303 to `/today`. The authenticated Compose browser inspected the
patient access centre, Today, conversations, and care home at 1,280 px with no
horizontal overflow or browser log errors; phone widths are covered by the live
Playwright scenarios.

The exceptional support-export slice then passed 127 focused support,
portability, import, and migration tests (four PostgreSQL-only skips), 349
i18n/design/static/router contracts, the 17-scenario live support-browser suite,
full Ruff and diff checks, and a Tailwind rebuild with no generated CSS change.
The PostgreSQL 15 migration rehearsal completed `head → 0034 → head`, including
the real `0075 → 0074 → 0075` path, and the corrected cross-dialect one-shot
case passed on PostgreSQL. The full fast suite passed 5,070 tests, skipped 187,
and deselected 39.

The entries below are the original audit-baseline runs on `0064`; they remain
historical evidence rather than claims that those exact commands were rerun
after the later revisions:

- focused ownership/runbook contracts: 12 passed;
- design/static/mobile contracts: 313 passed;
- MCP/LLM/external API focus: 37 passed;
- OIDC focus before remediation: 77 passed;
- analytics relocation focus: 59 passed;
- persistence relocation focus: 126 passed, 14 skipped;
- federated session enforcement focus: 147 passed, 1 skipped;
- support step-up/session-envelope focus: 132 passed, 1 skipped;
- isolated revoked-support HTTP regression: passed within its 3-second bound;
- full browser suite: 35 passed in 93.49 seconds;
- final fast suite: 4,621 passed, 168 skipped, 35 deselected in 129.71 seconds;
- PostgreSQL migration rehearsal: upgrade to `0064`, downgrade to `0034`, and
  upgrade back to `0064` completed successfully;
- every one of the 4,789 PostgreSQL-selected tests completed successfully across
  ordered fresh-process segments. The largest final segment passed 1,982 tests
  in 30 minutes. A single monolithic process still accumulates asyncpg
  cancellation/resource warnings and once returned an opaque CLI
  `internal_error` after 2,077 passes; that exact rehearsal passed in a fresh
  process. CI should shard this suite instead of treating that runner leak as a
  product failure;
- the PostgreSQL sweep exposed and corrected three SQLite-only historical-data
  fixtures: HRT child, HRT compound/component, and HRT template child ownership
  mismatches now use the explicit `unenforced_legacy_write` seam and pass on
  both databases;
- Tailwind rebuild completed; final `ruff check .` and `git diff --check`
  passed;
- Compose rebuilt the application image from the final tree. `/health` returned
  200, `/` redirected 303 to `/today`, OIDC discovery returned 200, PostgreSQL
  and Redis were healthy, and Alembic reported `0064 (head)`;
- the rebuilt browser session retained the doctor/patient conversation and the
  consent screen rendered human names, one record-section summary, clean
  collaboration copy, and no untranslated `nav.milestones` key.
