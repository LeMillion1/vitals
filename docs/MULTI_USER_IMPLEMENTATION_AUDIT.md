# Multi-user implementation audit

Last verified: 2026-08-25, branch `commercial/main`, through commit `84a0631`.

This is the current-state companion to the commercial roadmap and ownership
cutover history. It separates shipped behavior from target design and records
the commands or source registries behind quantitative claims. No production
database, `.env`, upload, backup, credential, Garmin session, or OAuth token was
read during the audit.

## Executive verdict

The conversion is substantial and real, but not finished. Vitals now has local
users and additive roles, one owned health subject per patient account,
subject-scoped facts, 62 PostgreSQL RLS tables, professional relationships and
consent, care-team conversations, OIDC login, revocable browser sessions,
per-subject integrations, and controlled read-only support access.

The earlier documentation overstated completion in four important areas:

1. PR-10 protocol compatibility shipped, but its target authorization model did
   not. MCP credentials remain long-lived account credentials without a health
   subject, domain/action scopes, relationship, or consent version.
2. The OIDC and support claims were ahead of the routes. Partial OIDC config,
   the known first administrator password, local-only logout, unenforced browser
   session versions, and support actions without step-up were found in this
   audit. Commits `b6e3e91`, `7ff5271`, `c159b5d`, and `84a0631` close those
   specific gaps.
3. The ownership inventory calls itself exhaustive but omits 12 of 76 live
   tables. The machine registry is exhaustive; the prose table is not.
4. `ARCHITECTURE.html` is a historical snapshot presented as a live reference.
   Its schema, migration, router, service, RLS, and roadmap counters are stale.

## Reproducible current facts

| Fact | Current evidence | Status |
| --- | --- | --- |
| Alembic head | `.venv/bin/python -m alembic heads` → `0064 (head)`; 64 files in `migrations/versions` | Verified |
| SQLAlchemy tables | `len(Base.metadata.tables)` → 76 | Verified |
| Ownership registry | `len(OWNERSHIP_REGISTRY)` → 76 | Verified and exhaustive in code |
| Subject-scoped tables | 62 metadata tables carry `subject_id`; the RLS revision union covers the same set | Verified |
| Required subject columns | 52 of the 62 `subject_id` columns are non-null | Verified |
| Ownership cutover | 18 ordered backfill phases and 18 matching scripts | Verified |
| Domain enum | 14 health domains | Verified |
| Web routers | 28 modules under `web/routers` | Verified |
| Application services | 95 tracked non-`__init__` modules after the care, analytics, and persistence moves | Verified |
| Flat service debt | 83 tracked root modules under `vitals/services`; guarded against growth by `test_architecture_boundaries.py` | Verified, still too high |
| Browser scenarios | 35 scenarios selected by `pytest tests/ui -m ui` | Verified collection |
| Commercial Git history | 220 commits after base `c91456a`; 137 contain an explicit Claude Opus co-author trailer | Git metadata only |

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

- 62 tables are subject-scoped and covered by FORCE RLS in PostgreSQL.
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
  decision is correct; the current redirect and lack of patient task/inbox make
  the handoff confusing.
- Consent stores granular resource/action scopes, while the patient screen only
  offers a default grant. Plan creation/lifecycle routes exist, but the record
  template exposes no plan form or lifecycle controls.
- Message attachments, unread state, a professional inbox, authored note/plan
  presentation, and a coherent plan UI remain unbuilt.

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
- The intended professional authorization grant is not real yet. MCP tokens
  live for 365 days, bind to an account/client/audience, and expose the broad
  `vitals:record` scope. They do not bind a health subject, relationship,
  consent version, resource domains, or actions.
- `mcp_token_service.verify()` does not currently validate the token's `iss`
  claim. This remains a security-hardening item.

## Documentation file verdicts

### `COMMERCIAL_MULTI_USER_ROADMAP.md` — useful target, unreliable status

The architectural principles and phased threat model are valuable. PR-01–09,
PR-11 without attachments, and PR-12 read mode broadly correspond to real code.
PR-10 is internally labelled merged, next, and complete while its own checklist
leaves the central grant conversion unchecked. “Merged PR” means a logical
phase, not a verifiable GitHub pull request: the commercial history has only one
merge commit. Keep this document as target/decision history and move current
status to this audit.

The following roadmap targets are not shipped: notifications/web push, care
attachments, invitation inbox, multi-subject full backup, support repair/export
and break-glass, scoped professional MCP/PAT grants, registration modes, lab
marker collision migration, private-byte relocation outside static storage, and
final legacy configuration contraction.

### `COMMERCIAL_OWNERSHIP_INVENTORY.md` — strong rationale, incomplete table

The provenance rules, ownership classes, raw-first policy, scoped natural-key
reasoning, composite-FK rules, and historical cutover analysis are strong. The
machine registry is the source of truth and is complete.

The prose inventory lists 64 live and two dropped tables, not all 76 live
tables. Missing rows are:

```text
care_plans
care_relationships
consent_grants
consent_scopes
external_api_tokens
mcp_access_tokens
professional_invitations
professional_notes
professional_profiles
support_access_request_scopes
support_access_requests
user_federated_identities
```

The historical Stage 3/4/5 narrative should be archived separately from a
generated current registry table.

### `COMMERCIAL_DUAL_WRITE_MATRIX.md` — valid historical dossier

Its introduction correctly says it is historical after migrations 0049/0050.
The migration reasoning and write-path inventory remain useful. Later present-
tense statements about global keys, FastMCP v1, exact-one scheduling, opaque
asset URLs, and unscoped charts/reports are stale. Treat it as a versioned
cutover dossier, never as runtime status.

### `OWNERSHIP_CUTOVER_RUNBOOK.md` — correct order, incomplete operation

The 18-phase order, checkpoint model, 0049 non-null guard, 0050+ FORCE RLS, and
fresh-install migration path are real. Two operational claims need correction:

- FastAPI lifespan, not “the first request”, bootstraps the owner.
- After `alembic upgrade 0048`, the normal image command immediately upgrades
  to head before starting Uvicorn. The runbook gives no safe one-off command to
  start the application at 0048 for root materialization. A rehearsal on the
  real production lake is not proved by a text-order test.

### `OIDC_SETUP.md` — corrected during this audit

The document now names every required IDP/application secret, fails partial
application configuration closed, replaces the known first administrator
password, describes the pinned v2.66.0 Apache-2.0 license accurately, documents
explicit account linking, provider logout, browser-session revocation, and
support step-up. Provider-specific backup/restore and upgrade rehearsal remain
operator work that local tests cannot prove.

### `ARCHITECTURE.md` — mostly current method, incomplete generated sources

Its reproducible-counter idea is right. The RLS source list omits revisions
0055–0057, and its assertion that the HTML is kept synchronized is false. It now
documents the first bounded-context moves, but the live flat-service debt is
still substantial.

### `ARCHITECTURE.html` — stale snapshot

Displayed claims include 62 or 72 tables instead of 76, 53 migrations instead
of 64, 15 domains instead of 14, 26 routers instead of 28, 59 RLS tables instead
of 62, and PR-05/PR-07 states that predate their implementations. Its ownership
class table contains only eight of the current classes. Generate its counters
from code or remove them; hand-maintained duplicate numbers have repeatedly
drifted.

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
The IdP advertised public self-registration even though Vitals registration is
closed, producing an orphan-account dead end. Its Russian copy contains typos,
mobile controls are below the project's 44 px target, and the registration back
icon lacks an accessible name.

The first shared UI run reported 25 passed and 10 failures after a revoked
support-record navigation timed out; the remaining nine were cascading server
timeouts. The exact HTTP flow was added as a three-second regression and passed,
and the isolated browser scenario passed in 11.98 seconds. The full 35-scenario
suite must be rerun without concurrent PostgreSQL/full-suite load before the
shared result can be classified as a product defect or a harness/load failure.

The largest confirmed UX gaps are the invitation→consent dead end, default-only
consent, missing plan controls, buried conversations without unread state,
support expiry shown without time/countdown, scope-inappropriate support
affordances, low-contrast `--faint` microcopy, and roster cards with little
prioritization.

## Architecture status and next moves

The core does not import FastAPI or `web`, and the import-time graph is acyclic.
The care domain now owns five related services, analytics moved out of the
application-service layer, and RLS/transaction primitives moved to
`vitals.persistence`. Static tests prevent core→web, services→operations,
pure-analytics→I/O, flat-service growth, and import-time cycles.

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

- focused ownership/runbook contracts: 12 passed;
- design/static/mobile contracts: 313 passed;
- MCP/LLM/external API focus: 37 passed;
- OIDC focus before remediation: 77 passed;
- analytics relocation focus: 59 passed;
- persistence relocation focus: 126 passed, 14 skipped;
- federated session enforcement focus: 147 passed, 1 skipped;
- support step-up/session-envelope focus: 132 passed, 1 skipped;
- isolated revoked-support HTTP regression: passed within its 3-second bound;
- isolated revoked-support browser scenario: 1 passed in 11.98 seconds.

Full fast, full PostgreSQL, full UI, Compose rebuild, Tailwind/static UI gates,
and final diff/lint checks must be recorded from the final commit rather than
copied from an earlier snapshot.
