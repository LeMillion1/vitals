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

from vitals.enums import Domain
from vitals.services import conflict_engine
from vitals.services import (
    body_scan_service,
    glp1_service,
    labs_service,
    nutrition_service,
    skincare_service,
    supplements_service,
    weight_service,
)
from vitals.services.hrt import records
from vitals.services.genetics import variants


def register_all_resolvers() -> None:
    """Register every domain's conflict resolver. Idempotent (re-registering a
    domain replaces it), so safe to call once per startup."""
    conflict_engine.register_domain_resolver(
        Domain.SUPPLEMENTS.value,
        supplements_service.resolve_active_scoped,
        legacy_probe=supplements_service.legacy_unowned_present,
    )
    conflict_engine.register_domain_resolver(
        Domain.GENETICS.value,
        variants.resolve_variants_scoped,
        legacy_probe=variants.legacy_unowned_present,
    )
    conflict_engine.register_domain_resolver(
        Domain.SKINCARE.value,
        skincare_service.resolve_today_scoped,
        legacy_probe=skincare_service.legacy_unowned_present,
    )
    conflict_engine.register_domain_resolver(
        Domain.GLP1.value,
        glp1_service.resolve_active_scoped,
        legacy_probe=glp1_service.legacy_unowned_present,
    )
    conflict_engine.register_domain_resolver(
        Domain.LABS.value,
        labs_service.resolve_latest_scoped,
        legacy_probe=labs_service.legacy_unowned_present,
    )
    conflict_engine.register_domain_resolver(
        Domain.NUTRITION.value,
        nutrition_service.resolve_today_scoped,
        legacy_probe=nutrition_service.legacy_unowned_present,
    )
    conflict_engine.register_domain_resolver(
        Domain.HRT.value,
        records.resolve_active_scoped,
        legacy_probe=records.legacy_unowned_present,
    )
    conflict_engine.register_domain_resolver(
        Domain.WEIGHT.value,
        weight_service.resolve_active_scoped,
        legacy_probe=conflict_engine.legacy_unowned_raw_present,
    )
    conflict_engine.register_domain_resolver(
        Domain.BODY_COMPOSITION.value,
        body_scan_service.resolve_active_scoped,
        legacy_probe=conflict_engine.legacy_unowned_raw_present,
    )
