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

vitals/services/genetics/
├── variants.py        # scoped variant facts and raw-first ingestion
└── vcf.py             # pure VCF parsing and curated interpretation

vitals/operations/ownership/
├── portability_v1.py # destructive full-v1 restore coordinator
├── validate.py       # cross-table ownership cutover validation
├── audit.py          # scoped-key collision operator audit
└── <phase>.py        # 19 resumable ownership backfill programs

vitals/operations/portability/
├── export_v2.py      # encrypted personal archive coordinator
└── import_v2.py      # receipt-backed atomic replacement coordinator

vitals/services/portability/
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
service. Further domains should be extracted in bounded,
behavior-preserving commits with their focused tests and the full fast suite.
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
| routers, tracked application-service modules | tracked non-`__init__` files in `web/routers/`, `vitals/services/` | 34 and 103 |

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
