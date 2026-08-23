# Architecture

The architecture reference lives in [`ARCHITECTURE.html`](ARCHITECTURE.html) — a
single self-contained page with eight diagrams: C4 context and containers, the
ownership graph, the data lake's raw/fact/artifact lifecycle, the write and read
paths, the conflict engine, the conversion timeline, and the cutover sequence.

It is HTML rather than Markdown because the diagrams carry most of its meaning
and are worth rendering rather than describing. Open it in a browser, or publish
it as an artifact.

## Keeping it true

Its figures come from the code it describes, and a drift there is silent — the
page renders just as happily with a wrong number. When any of these change, the
page needs the same edit:

| The page says | Comes from |
| --- | --- |
| table counts, ownership classes | `vitals/ownership.py` |
| the twenty backfill phases | `vitals/ownership_deploy.py` |
| the fifteen domains | `vitals.enums.Domain` |
| migration count | `migrations/versions/` |
| RLS table count | revisions `0050` + `0051` |
| platform-scope call sites | `tests/test_row_level_security.py` |

The eight diagrams are validated by parsing them with mermaid itself; a syntax
error renders as an error box rather than failing loudly, so it is worth
re-checking after an edit rather than trusting the page to complain.
