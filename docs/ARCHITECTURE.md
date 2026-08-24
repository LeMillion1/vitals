# Architecture

The architecture reference lives in [`ARCHITECTURE.html`](ARCHITECTURE.html) — a
single self-contained page with eight diagrams: C4 context and containers, the
ownership graph, the data lake's raw/fact/artifact lifecycle, the write and read
paths, the conflict engine, the conversion timeline, and the cutover sequence.

It is HTML rather than Markdown because the diagrams carry most of its meaning
and are worth rendering rather than describing.

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
| table count, ownership classes | `vitals/ownership.py` | 69 tables, 32 of them `subject_data` |
| mandatory-subject table count | `subject_id` NOT NULL in `Base.metadata` | 46 |
| the backfill phases | `OWNERSHIP_BACKFILL_SEQUENCE` in `vitals/ownership_deploy.py` | 18 |
| the domains | `vitals.enums.Domain` | 14 |
| the scheduled jobs | `vitals/scheduler/jobs.py` | 14 |
| migration count | `migrations/versions/` | 60, head `0060` |
| RLS table count | revisions `0050` + `0051` + `0060`, asserted in `tests/test_row_level_security.py` | 56 |
| platform-scope call sites | the permitted list in `tests/test_row_level_security.py` | 6 |
| routers, service modules | `web/routers/`, `vitals/services/` | 26 and 94 |

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
