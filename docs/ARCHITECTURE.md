# Architecture

The visual architecture reference lives in [`ARCHITECTURE.html`](ARCHITECTURE.html) — an
artifact-body reference with eight diagrams: C4 context and containers, the
ownership graph, the data lake's raw/fact/artifact lifecycle, the write and read
paths, the conflict engine, the conversion timeline, and the cutover sequence.

It is HTML rather than Markdown because the diagrams carry most of its meaning
and are worth rendering rather than describing.

## Source-code boundaries

The diagrams describe runtime boundaries; the Python tree follows the same
rule. Core code lives under `vitals/` and never imports FastAPI or `web/`.
Delivery adapters live under `web/`, and scheduled jobs remain orchestration
boundaries rather than alternative business-service implementations.

Within `vitals/services/`, new code is grouped by bounded domain instead of
adding another `<noun>_service.py` to the root. A package exposes concepts, not
a service locator. The extracted boundaries are:

```text
vitals/services/care/
├── invitations.py     # patient offer and token lifecycle
├── professionals.py   # professional claim and verification
├── relationships.py   # relationship and consent lifecycle
├── record_projection.py # consent-first bounded record summaries
├── records.py         # authored notes and care plans
└── threads.py         # patient-visible care-team conversation

vitals/services/authentication/
├── admission/
│   ├── invitations.py # email-bound one-time account admission
│   ├── console.py     # redacted operator registration projection
│   ├── requests.py    # federated request and operator decision
│   └── retention.py   # bounded expiry and applicant-PII scrubbing
├── federation.py      # provider identity to local account
├── registration.py    # deployment-gated admission decision
├── provisioning.py    # account and optional health-record creation
├── oidc.py            # protocol verification boundary
├── sessions.py        # local session revocation
├── oauth_clients.py   # remote client metadata and SSRF boundary
├── connector_authorization.py # one OAuth account-to-subject choice
├── mcp_tokens.py      # connector credential lifecycle
└── legacy_two_factor.py # local-password cutover path only

web/authentication/
├── tokens.py          # signed credential envelopes and cookie transport
├── legacy.py          # password and second-factor HTTP routes
├── federated.py       # OIDC redirects and admission response mapping
└── routes.py          # explicit route aggregation for the application shell

web/auth.py            # compatibility imports only; no authentication flow

web/settings/
├── forms.py           # side-effect-free masked-secret form primitives
├── platform.py        # installation AI and MCP control-plane routes
└── routes/            # profile, providers, security, preferences, portability

vitals/services/genetics/
├── contracts.py / validation.py
├── queries.py / writes.py # scoped variant facts
├── vcf_ingestion.py / reparse.py # raw-first workflows
└── vcf.py             # pure VCF parsing and curated interpretation

vitals/services/hrt/
├── catalog.py         # curated compound catalog
├── records.py         # compounds, doses, and side effects
├── cycles.py          # cycle plans and release projection
├── templates.py       # portable cycle templates
└── reminders.py       # subject-scoped HRT reminder scheduling

vitals/services/credentials/
├── vault.py           # encrypted credential persistence
└── providers.py       # subject-scoped provider resolution

vitals/services/body_scan/
├── scans/             # contracts, normalization, facts, queries, alerts, replay
└── ai/                # scope, projection and document-review workflow

vitals/services/labs/
├── flags.py           # pure reference-range classification
├── markers.py         # marker identity and catalog
├── results.py         # scoped facts, provenance, conflicts, and queries
├── alerts.py          # derived out-of-range and retest alerts
├── ingestion.py       # raw-first extraction, confirmation, MCP ingest, replay
└── ai.py              # paid document dispatch and accounting lifecycle

vitals/services/weight/
├── contracts.py       # typed BodyScan and Garmin-outbox workflow ports
├── governance.py      # scoped conflict capability and write preparation
├── logs.py / measurements.py / noise.py / photos.py
│                         # independently owned facts and artifacts
├── queries.py         # subject-scoped read models and audience policies
├── writes.py          # fact commands; flush without hidden commit
├── analytics.py       # domain-facing analytical projections
└── alerts.py          # derived weight alerts

vitals/services/garmin/
├── normalization.py   # pure Garmin payload normalization
├── ownership.py       # connection and normalized-row ownership rules
├── raw_payloads.py    # raw-first provider payload persistence
├── ingestion.py       # idempotent normalized fact ingestion
├── queries.py         # connection-bound, subject-scoped reads
├── sync.py / jobs.py  # provider workflow and commit-owning entry points
└── alerts.py / advice.py / errors.py

vitals/services/hevy/
├── normalization.py   # pure Hevy workout normalization
├── ownership.py       # workout/exercise/set ownership rules
├── raw_payloads.py    # raw-first provider payload persistence
├── persistence.py     # idempotent workout graph persistence
├── ingestion.py       # validated raw-to-fact orchestration
├── queries.py         # subject-scoped workout graphs and summaries
└── sync.py / jobs.py  # provider workflow and commit-owning entry points

vitals/services/{glp1,nutrition,skincare,supplements}/
├── contracts and validation/parsing leaves
├── subject-scoped queries and commands
└── conflict/alert/job leaves where the domain needs them

vitals/services/{timeline,milestones}/
├── owned record commands and queries
└── derived cross-domain event/progress projections

vitals/services/projections/
└── data_overview.py   # reusable subject-scoped cross-domain counts

web/mcp/
├── access.py / identity.py / ownership.py
│                         # MCP authentication and authorization policy
├── arguments.py / serialization.py / errors.py
│                         # protocol input and output conventions
├── server.py / transport.py / resources.py / modules.py
│                         # server, HTTP, resource, and module-gate boundaries
├── record_catalog.py # compatibility metadata, never generic persistence
└── tools/             # domain and provider delivery adapters

vitals/services/conflicts/
├── catalog.py         # curated safety-rule definitions
├── activation.py      # subject-specific rule activation
├── engine/            # contracts, matching, scope, rules, evaluation, enforcement
└── registrations.py   # domain-resolver wiring

vitals/services/digest/
├── window.py / queries.py / ownership.py
├── projection/        # bounded clinical, lifestyle and provider context
└── prompt.py / generation.py / jobs.py

vitals/services/proactive/
├── preferences/       # typed preferences, codec, queries, writes, legacy bridge
├── brief/             # context, preparation, rendering, persistence and jobs
└── delivery/          # contracts, policy, preparation, dispatch, reconciliation

vitals/services/charts/
├── configuration.py   # subject-scoped saved charts and cache
└── data.py            # catalog and render-ready metric series

vitals/services/profile/ / preferences/
├── health.py          # subject-owned body facts, goals and timezone projection
└── language.py        # user-owned language and cache

vitals/services/modules/
├── registry.py        # pure module manifest and safe defaults
├── navigation.py      # pure rail/mobile navigation projections
└── preferences.py     # subject-scoped enablement and cache

vitals/services/dashboard/
├── today.py           # Today page composition
└── nav_status.py      # fail-safe navigation status projection

vitals/services/settings/
├── contracts.py       # allowlisted scopes, keys and validation errors
├── legacy.py          # exact-one-subject compatibility policy
└── scoped_store.py    # caller-committed scoped reads and writes

vitals/services/alerts/ / ai_gateway/
└── typed lifecycle and paid-provider workflow boundaries

vitals/services/{identity,authorization,tenancy,platform}/
├── identity/              # normalization, credentials, roles and governance
├── authorization/         # installation and subject-access decisions
├── tenancy/              # bootstrap and the explicit legacy ownership bridge
└── platform/             # platform-admin and installation AI control

vitals/services/{files,data_lake,external_api}/
├── files/                 # private asset lifecycle, queries and upload references
├── data_lake/             # raw-first contracts, persistence and reparse sweep
└── external_api/          # hashed capability-token lifecycle

vitals/services/{charts,dashboard,profile,preferences,modules,settings}/
├── charts/                # chart configuration and subject-scoped series data
├── dashboard/             # Today projection and navigation status
├── profile/ / preferences/# health profile and locale preferences
├── modules/               # registry, navigation and per-subject visibility
└── settings/              # scoped storage primitive

vitals/services/share/ / support_access/
└── ownership, policy, projections, lifecycle and boundary-owned jobs

vitals/operations/ownership/
├── portability_v1.py # destructive full-v1 restore coordinator
├── validate.py       # cross-table ownership cutover validation
├── audit.py          # scoped-key collision operator audit
└── <phase>.py        # 19 resumable ownership backfill programs

vitals/operations/portability/
├── export_v2.py      # encrypted personal archive coordinator
└── import_v2.py      # receipt-backed atomic replacement coordinator

vitals/services/portability/
├── v1_contract.py / v1_export.py / v1_import.py / llm_projection.py
├── archive.py / archive_reader.py # authenticated archive boundaries
├── connection_mapping.py          # explicit credential-free C mapping
├── resource_staging.py            # verified private-byte staging
├── replacement_preflight.py / replacement_apply.py
└── receipts.py / file_retirement.py

vitals/ownership_transition/
├── bridges.py        # read-only historical checkpoint projections
└── portability_v1.py # typed hook contract, no operation imports
```

Callers import the concept they use, for example
`from vitals.services.care import relationships`. The old flat module paths are
removed rather than kept as permanent forwarding shims: internal imports are
updated in the same commit, which prevents the obsolete structure from becoming
a second supported API. Pure computation lives in `vitals/analytics/`, and
persistence primitives live in `vitals/persistence/`; neither is an application
service. The tracked service root now contains only its package marker: every
application-service module has an owning bounded context. Architecture ratchets
reject both a new root module and any import of the 21 retired paths, while every
application-service leaf is capped at 1,300 lines so a bounded directory cannot
quietly become a renamed monolith.

Compatibility responsibilities remain deliberately visible inside their owners,
not as root-level facades. `tenancy.ownership` resolves the historical
single-owner/system call shape without mutating identity state; callers retire
it by supplying explicit subject, actor, and provider roots. `settings.legacy`
maintains reviewed singleton keys only while exactly one active owner makes
those keys unambiguous. The `alerts.legacy_subject` aggregate is valid only
while registration is disabled and fully unowned historical alerts can still
exist; the scoped alert lifecycle is its replacement. `identity.bootstrap`
similarly isolates the environment-backed owner cutover. Their names,
fail-closed checks, and removal conditions make transitional cost inspectable
instead of turning it into a permanent API.
At the web boundary, signed credentials are likewise independent from request
dependencies, rate limiting, templates, and domain services. OIDC verification
stays in the core provider adapter; `authentication.federation` decides whether
a validated identity becomes a session, an admission request, or an invitation
claim; the federated router only maps that result to cookies and redirects.
The domain-by-domain extraction order and cross-surface consistency rules live
in [`DOMAIN_REFACTORING_ROADMAP.md`](DOMAIN_REFACTORING_ROADMAP.md).
Stable domain names, navigation aliases, record-section membership, and the
meaning of context-level `domain` fields live in `vitals.domain_taxonomy`.
Surface inclusion remains explicit and independently tested for sharing, care,
charts, MCP, digest, conflicts, and portability.

Domain query APIs require a `subject_id`; provider reads additionally validate
that the provenance connection belongs to that subject. Cross-domain and
audience projections may share these column-minimal query slices, but they do
not share output schemas by accident. Care keeps consent and linked-raw
ownership checks, emergency access keeps its deliberately narrower break-glass
shape, and MCP keeps its frozen protocol contract. The MCP router is the single
composition registry: decorators and domain behavior live in adapter leaves,
while the router only wires dependencies and retains named compatibility
exports for callers and contract tests.
Operational programs may depend on application services, but application
services must not import `vitals.operations`. The static architecture contract
enforces that direction and keeps the two remaining request-time checkpoint
reads in the lower, non-mutating `ownership_transition` seam.

**How to open it.** The file is authored as an *artifact body* — no `<!doctype>`,
`<html>`, `<head>` or `<body>` of its own — so publishing it as an artifact is
the intended path, and the artifact host renders every `<pre class="mermaid">`
itself.

Opened straight from `docs/` it now renders too, which it did not until
2026-08-24: there was nothing to turn the diagram source into a diagram, so all
eight showed their own mermaid markup and the page read as broken. A small module
script at the end of the file imports mermaid from a CDN and runs it, guarded so
it does nothing when something else has already drawn the diagrams. Three
consequences worth knowing:

- **It needs network, and only for the diagrams.** Offline, the prose and tables
  are fine and the diagram source stays visible with a console warning. That is
  the honest failure: worse than a diagram, better than an empty box.
- **In an artifact the import is blocked** by the CSP, which is correct and not
  an error — the host has already rendered them, and the guard sees that.
- **`file://` will not do**, because a module script cannot be imported from it.
  Serve the directory (`python3 -m http.server`) or publish it.

## Keeping it true

Its figures come from the code it describes, and a drift there is silent — the
page renders just as happily with a wrong number. When any of these change, the
page needs the same edit:

| The page says | Comes from | Today |
| --- | --- | --- |
| table count, ownership classes | `vitals/ownership.py` | 88 tables, 32 of them `subject_data` |
| mandatory-subject table count | `subject_id` NOT NULL in `Base.metadata` | 60 |
| the backfill phases | `OWNERSHIP_BACKFILL_SEQUENCE` in `vitals/ownership_deploy.py` | 19 |
| the domains | `vitals.enums.Domain` | 14 |
| external integration modules | tracked non-`__init__` modules in `vitals/integrations/` | 5 |
| the scheduled jobs | `vitals/scheduler/jobs.py` | 16, of which 11 fan out per record |
| migration count | `migrations/versions/` | 84, head `0084` |
| RLS table count | table coverage from revisions `0050` through `0079`, plus the `0083` worker-capability policy rewrite, asserted in `tests/test_row_level_security.py` | 71 |
| platform-scope functions | the permitted list in `tests/test_row_level_security.py` | 9 |
| routers, tracked application-service modules | tracked non-`__init__` files in `web/routers/`, `vitals/services/` | 37 and 246 |

The **39 columns** the timeline attributes to revision `0049` is deliberately
*not* in that table: it is the length of that revision's own
`REQUIRED_OWNERSHIP_COLUMNS`, fixed at the moment it ran. A migration's list is
history and does not move when a later revision drops a table, so a reader who
recomputes it from today's schema and finds a smaller number has found a
different fact, not a drift.

Recompute the live column with:

```bash
.venv/bin/python -c "import vitals.models; from vitals.models.base import Base; from vitals.ownership import OWNERSHIP_REGISTRY; from vitals.ownership_deploy import OWNERSHIP_BACKFILL_SEQUENCE as S; from vitals.enums import Domain; t=Base.metadata.tables; print('tables', len(t)); print('mandatory', sum(1 for x in t.values() if 'subject_id' in x.columns and not x.columns['subject_id'].nullable)); print('rls', sum(1 for x in t.values() if 'subject_id' in x.columns)); print('phases', len(S)); print('domains', len(list(Domain)))"
```

The eight diagrams are validated by parsing them with mermaid itself; a syntax
error renders as an error box rather than failing loudly, so it is worth
re-checking after an edit rather than trusting the page to complain.
