# Domain refactoring roadmap

This roadmap turns the current domain inventory into small, behavior-preserving
architecture changes. It is deliberately not a proposal for one generic CRUD
framework: health writes have different provenance, conflict, ownership, raw
payload, and transaction rules. The goal is consistent boundaries and
vocabulary while retaining those differences.

## Current domain surface

Vitals has fourteen `Domain` values: thirteen record sections plus internal
`system`. Every record domain is represented in MCP access policy and digest
context, but other surfaces are intentionally selective.

| Domain | Current service shape | Important non-web surfaces |
| --- | --- | --- |
| `weight` | bounded package; facts, measurements, noise, photos, queries, analytics and explicit workflow ports | MCP, charts, share, portability, Garmin export |
| `body_comp` | `body_scan` package, projected into Weight | MCP, charts, share, raw/file/AI lineage |
| `glp1` | bounded package; commands, queries, plateau alerts and job | MCP, plateau job, conflicts, share |
| `supplements` | bounded package; parsing, queries, commands and conflicts | MCP, conflicts, share |
| `genetics` | `genetics` package | MCP, conflicts, raw-first VCF, share |
| `skincare` | bounded package; commands, queries and conflict projection | MCP, charts, conflicts, share |
| `workouts` | bounded Hevy provider package; module alias `hevy` | MCP, sync job, raw payloads, share |
| `garmin` | bounded Garmin provider package plus a separate weight-export workflow | MCP, sync jobs, raw payloads, share |
| `labs` | bounded package including raw/file/AI ingestion | MCP, conflicts, raw/file/AI lineage, share |
| `nutrition` | bounded package; commands, queries, analytics, conflicts and job | MCP, day-end job, conflicts, share |
| `hrt` | established `hrt` package | MCP, reminders, conflicts, share |
| `milestones` | bounded package; goals, queries, ownership and progress projection; module alias `reports` | MCP goals, digest jobs, portability |
| `timeline` | bounded package separating annotation records from the derived event projection | MCP, chart overlays, portability |
| `system` | several control/artifact contexts | alerts, reconciliation jobs; intentionally non-portable |

Disaster backup, personal portability, public sharing, digest context, LLM
export, charts, and MCP are distinct projections. Their inclusion policies must
remain explicit; sharing a query does not mean sharing an output schema or
privacy policy.

## Consistency rules

Each bounded domain should converge on these rules:

1. Models remain in `vitals.models` and retain ownership/provenance invariants.
2. Application behavior lives in a named package under `vitals.services`.
3. Protocol parsing and vendor quirks stay under `vitals.integrations` or a
   clearly named ingestion module.
4. Services mutate and `flush()`; routers and job entry points own commits.
5. External input is raw-first where an upstream payload exists.
6. Web and MCP adapters use the same service commands and subject-scoped query
   functions, but keep their own response and privacy contracts.
7. Every domain has an explicit disposition for modules, MCP, charts,
   conflicts, scheduler, portability, share, digest, and LLM export: exposed or
   intentionally excluded.

## Ordered work

The four phases below are implemented. Their static architecture tests now act
as ratchets: the removed flat modules cannot be imported again, domain and
provider reads stay subject-scoped, MCP keeps one frozen registry and no tool
implementations in its router, and reusable projections remain independent of
their audience-specific output contracts.

The follow-through also decomposed Body Scan, Genetics, conflicts, digest,
proactive preferences/brief/delivery, alerts, AI gateway, Garmin weight export,
sharing, support access, portability, identity, authorization, tenancy, platform
control, private files, the raw data lake, external API tokens, charts,
dashboard projections, profile/preferences, optional modules, and scoped
settings. The tracked `vitals/services` root now contains only `__init__.py`;
all 74 historical flat modules have an owning bounded context. Static tests
reject both new root modules and imports of the final 21 retired paths. Every
recursive application-service leaf remains guarded at 1,300 lines. Large
ownership backfill programs remain under `vitals.operations`: they are cohesive,
resumable, commit-bound operator workflows rather than request-time domain
services, so their explicit phase contracts are preserved.

### Phase 0 — contract ratchets

- Add a pure domain taxonomy manifest for canonical name, module key, aliases,
  record-section status, and whether a model's `domain` field is a discriminator
  or a target selector.
- Initially consume it only from tests. Keep surface-specific inclusion policy
  local to each surface.
- Pin MCP names and schemas, module gates, scheduler IDs, conflict resolver set,
  portability inventory, share domains, and digest paths.
- Explicitly document and test the different portability treatment of Garmin
  weight exports, system alerts, weekly digests, and delivery artifacts.

### Phase 1 — Labs package

Extract the current Labs services into:

```text
vitals/services/labs/
├── flags.py
├── markers.py
├── results.py
├── ingestion.py
├── alerts.py
└── ai.py
```

Move pure marker/flag behavior first, persistence second, and raw/file/AI
ingestion last. Preserve public service signatures during each focused change,
then remove the old flat path rather than keeping a permanent forwarding shim.

### Phase 2 — Weight package and explicit workflow ports

Extract logs, measurements, noise, photos, analytics, alerts, and writes into a
`weight` package. Keep BodyScan as its own domain. Define an explicit command for
BodyScan-to-Weight projection and an explicit outbox hook for Garmin export;
do not merge their state machines.

### Phase 3 — provider packages

Package Garmin and Hevy independently into normalization, ownership, ingestion,
queries, and sync modules. Do not introduce a generic provider repository in
the first move. Compare the resulting packages and extract only primitives that
are proven identical, especially connection and raw-payload validation.

### Phase 4 — delivery and cross-domain projections

Split MCP into common authentication/access/serialization plus domain adapters
with one explicit registry. Then migrate digest, share, LLM export, and MCP to
reusable subject-scoped query slices one domain at a time. Keep each audience's
windowing, privacy, immutability, and output schema separate.

Implemented slices include the health-profile projection, cross-domain data
overview, bounded Weight histories, and latest-per-marker Labs results. Care
and emergency policies intentionally remain separate wrappers around common
column-minimal reads. Garmin and Hevy MCP tools use their provider query and job
boundaries rather than delivery-layer ORM queries.

## Validation discipline

Each phase is one bounded-context change with no schema migration unless the
domain itself requires one. Run focused web/MCP/conflict/provenance/ownership
tests while iterating, PostgreSQL tests for raw JSONB or concurrency behavior,
and the full fast suite before handoff. Preserve `Source`, `Domain`, external
IDs, raw/file/connection/AI links, lock order, and commit ownership byte for
behavior.
