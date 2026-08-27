"""Conflict-engine domain-resolver registrations.

The engine evaluates data-driven rules but needs to know each domain's *current*
active state — that's module-specific, so modules register a resolver. This
module gathers those registrations behind :func:`register_all_resolvers`, invoked
once from the web lifespan (and by tests that exercise cross-domain rules).

Every resolver here is subject-scoped. Seven domains used to register a second,
unscoped reader alongside it, so that a write path arriving without a subject
still got an answer — assembled from every row in the installation. That is
gone: a rule is evaluated for one person or not at all. Rows the backfill has
not stamped yet are still reachable, but only through the scope's explicit
``FULLY_UNOWNED`` bridge, which requires a subject to bridge *from*.

Each registration also names the probe that answers whether that bridge would
widen anything here, so the engine can tell "there is a row nobody owns" apart
from "this installation has two people" — it needs a sole subject only for the
first, and only when the answer is yes. Weight and body composition register the
engine's raw-payload probe: neither resolver widens, but both widen to unowned
raw provenance on the write path.

Kept out of service-import time so importing a service for a unit test never
mutates the global resolver registry (the test fixture clears it per test).
"""

from __future__ import annotations

from vitals.services.genetics import queries as genetics_queries

from vitals.services.supplements import conflicts as supplement_conflicts

from vitals.services.glp1 import queries as glp1_queries
from vitals.services.nutrition import conflicts as nutrition_conflicts
from vitals.services.skincare import conflicts as skincare_conflicts

from vitals.enums import Domain
from vitals.services.conflicts import engine

from vitals.services.body_scan.scans import queries as body_scan_queries
from vitals.services.hrt import records
from vitals.services.labs.results import (
    legacy_unowned_present as labs_legacy_unowned_present,
)
from vitals.services.labs.results import (
    resolve_latest_scoped as resolve_latest_labs_scoped,
)
from vitals.services.weight import queries as weight_queries


def register_all_resolvers() -> None:
    """Register every domain's conflict resolver. Idempotent (re-registering a
    domain replaces it), so safe to call once per startup."""
    engine.register_domain_resolver(
        Domain.SUPPLEMENTS.value,
        supplement_conflicts.resolve_active_scoped,
        legacy_probe=supplement_conflicts.legacy_unowned_present,
    )
    engine.register_domain_resolver(
        Domain.GENETICS.value,
        genetics_queries.resolve_variants_scoped,
        legacy_probe=genetics_queries.legacy_unowned_present,
    )
    engine.register_domain_resolver(
        Domain.SKINCARE.value,
        skincare_conflicts.resolve_today_scoped,
        legacy_probe=skincare_conflicts.legacy_unowned_present,
    )
    engine.register_domain_resolver(
        Domain.GLP1.value,
        glp1_queries.resolve_active_scoped,
        legacy_probe=glp1_queries.legacy_unowned_present,
    )
    engine.register_domain_resolver(
        Domain.LABS.value,
        resolve_latest_labs_scoped,
        legacy_probe=labs_legacy_unowned_present,
    )
    engine.register_domain_resolver(
        Domain.NUTRITION.value,
        nutrition_conflicts.resolve_today_scoped,
        legacy_probe=nutrition_conflicts.legacy_unowned_present,
    )
    engine.register_domain_resolver(
        Domain.HRT.value,
        records.resolve_active_scoped,
        legacy_probe=records.legacy_unowned_present,
    )
    engine.register_domain_resolver(
        Domain.WEIGHT.value,
        weight_queries.resolve_active_scoped,
        legacy_probe=engine.legacy_unowned_raw_present,
    )
    engine.register_domain_resolver(
        Domain.BODY_COMPOSITION.value,
        body_scan_queries.resolve_active_scoped,
        legacy_probe=engine.legacy_unowned_raw_present,
    )
