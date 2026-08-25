# Multi-user implementation audit

Last verified: 2026-08-25, branch `commercial/main`.

This is the current-state companion to the commercial roadmap and ownership
cutover history. It separates shipped behavior from target design and records
the commands or source registries behind quantitative claims. No production
database, `.env`, upload, backup, credential, Garmin session, or OAuth token was
read during the audit.

## Executive verdict

The conversion is substantial and real, but not finished. Vitals now has local
users and additive roles, one owned health subject per patient account,
subject-scoped facts, 65 PostgreSQL RLS tables, professional relationships and
consent, care-team conversations, OIDC login, revocable browser sessions,
per-subject integrations, and controlled read-only support access.

The earlier documentation overstated completion in four important areas. The
first finding was closed after the audit by commits `f2ea770` and `7523d26`;
the others remain useful provenance for the remediations named below:

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
   of 76 live tables. It now includes the subsequently added MCP scope table and
   matches all 79 machine-registry entries.
4. `ARCHITECTURE.html` was a historical snapshot presented as a live reference.
   Its schema, migration, router, service, RLS, ownership-class, and roadmap
   counters were synchronized during this audit.

## Reproducible current facts

| Fact | Current evidence | Status |
| --- | --- | --- |
| Alembic head | `.venv/bin/python -m alembic heads` → `0068 (head)`; 68 files in `migrations/versions` | Verified |
| SQLAlchemy tables | `len(Base.metadata.tables)` → 79 | Verified |
| Ownership registry | `len(OWNERSHIP_REGISTRY)` → 79 | Verified and exhaustive in code |
| Subject-scoped tables | 65 metadata tables carry `subject_id`; the RLS revision union covers the same set | Verified |
| Required subject columns | 54 of the 65 `subject_id` columns are non-null | Verified |
| Ownership cutover | 18 ordered backfill phases and 18 matching scripts | Verified |
| Domain enum | 14 health domains | Verified |
| Web routers | 28 modules under `web/routers` | Verified |
| Application services | 96 tracked non-`__init__` modules after the care, authentication, analytics, and persistence moves | Verified |
| Flat service debt | 77 tracked root modules under `vitals/services`; guarded against growth by `test_architecture_boundaries.py` | Verified, still too high |
| Browser scenarios | 35 scenarios selected by `pytest tests/ui -m ui` | Verified collection |
| Commercial Git history | 247 commits after base `c91456a`; 137 contain an explicit Claude Opus co-author trailer | Git metadata only |

Historical pass counts in roadmap prose and HTML are not evidence for the
current commit. Only a command executed against the current tree is recorded as
a current validation result.

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
- Registration remains closed. `invite_only` and `admin_approved` registration
  modes are target design, not implemented behavior.

### Data isolation

- 65 tables are subject-scoped and covered by FORCE RLS in PostgreSQL.
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
- Repair, exceptional export, two-person break-glass approval, operational
  dashboards, and retention tooling remain deliberately refused or absent.
- The UI renders expiry as a date rather than an exact time/countdown and does
  not clearly persist who ended a grant. These are current UX defects.

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

The following roadmap targets are not shipped: web-push permission UI,
consent-rechecked care delivery and its sender (encrypted account/device
subscriptions landed in `0068`),
invitation inbox, multi-subject full backup, support repair/export
and break-glass, registration modes, lab
marker collision migration, private-byte relocation outside static storage, and
final legacy configuration contraction.

### `COMMERCIAL_OWNERSHIP_INVENTORY.md` — strong rationale, now exhaustive

The provenance rules, ownership classes, raw-first policy, scoped natural-key
reasoning, composite-FK rules, and historical cutover analysis are strong. The
machine registry is the source of truth and is complete.

The prose inventory previously listed only 64 live and two dropped tables. It
now includes the missing identity, professional-care, support-request, and
credential rows, the MCP scope child, the care attachment row, and the web-push
subscription row and matches all 79 live tables in the
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
were synchronized during this audit. It now documents the first bounded-context
moves, but the live flat-service debt is still substantial.

### `ARCHITECTURE.html` — synchronized during this audit

The displayed schema, migration, domain, router, application-service, RLS,
ownership-class, PR-05, and PR-07 facts now match the current registries. They
remain hand-maintained duplicates; generating them from code is still safer
than relying on future reviewers to update every copy.

### `DESIGN_SYSTEM.md` — strongest live document

The Masthead shell, tokens, responsive rules, no-raw-hex/no-monospace policy,
touch targets, focus treatment, and mobile-table contracts have direct tests.
The document is too long and does not define professional, patient-consent,
messaging, or controlled-support interaction patterns. Split normative rules
from historical rationale and add those role journeys as first-class patterns.

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

The first shared UI run reported 25 passed and 10 failures after a revoked
support-record navigation timed out; the remaining nine were cascading server
timeouts. The product flow passed in isolation. The deterministic full-run cause
was the UI fixture piping Uvicorn access logs without draining the pipe: once
the OS buffer filled, the server blocked while handling a request. The fixture
now writes its synthetic server log to the run's temporary directory, and the
complete suite passes: 35 scenarios in 93.49 seconds.

The largest remaining confirmed UX gaps are support expiry shown without time/countdown,
low-contrast `--faint` microcopy outside the touched care screens, and roster
states beyond conversation urgency.

## Architecture status and next moves

The core does not import FastAPI or `web`, and the import-time graph is acyclic.
The care domain now owns five related services, authentication owns six protocol
and credential boundaries, analytics moved out of the application-service
layer, and RLS/transaction primitives moved to `vitals.persistence`. Static
tests prevent core→web, services→operations, pure-analytics→I/O, flat-service
growth, and import-time cycles.

The next large correction is not a blind folder move. Eighteen ownership
backfill programs contain about one third of service LOC, but live portability,
share, and weight services still import historical backfill behavior. Those
reverse dependencies must be removed before the backfills move to
`vitals/operations/ownership`; otherwise the new tree would encode a
`services → operations → services` cycle.

After that boundary is cut, move bounded contexts in dependency order and split
the 2,000–4,000-line monoliths under characterization tests. Delivery adapters
(`mcp.py`, settings, care/consent routers) should become thin only after the
application-service APIs stabilize.

## Evidence executed during the audit

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
