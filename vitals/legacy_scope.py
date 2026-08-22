"""Machine-readable inventory of the legacy unscoped service bridges.

Stage 2 gave every core service an optional scope: pass ``subject_id`` (or an
``identity``/``context``, and sometimes ``include_legacy_unowned``) and the call
is scoped; omit it and the call reads or writes across the whole installation
exactly as the single-user application always did.  That optionality is what
kept the migration reversible while ownership was being backfilled.

It is also the last thing standing between this schema and a second person.  A
scoped unique key over a nullable column, a policy engine no service consults,
and row-level security applied under an application that still issues unscoped
reads are each worth nothing on their own.  PR-04 closes these bridges service
by service; this registry is what makes "closed" measurable.

Every entry names a function that still accepts an omittable scope.  The paired
contract test recomputes the inventory from the source and fails when a module
grows a bridge that is not listed here, so the set can only shrink.  When it is
empty, ``AccessContext`` is mandatory everywhere and the compatibility columns
can become ``NOT NULL``.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType


# module -> functions that still accept an omittable subject/identity/context,
# or an ``include_legacy_unowned`` escape hatch.
LEGACY_SCOPE_BRIDGES: Mapping[str, frozenset[str]] = MappingProxyType(
    {





    }
)


def bridge_total() -> int:
    """Return how many bridged functions remain."""

    return sum(len(names) for names in LEGACY_SCOPE_BRIDGES.values())


# module -> subject-owned models still fetched by bare primary key.  A
# ``session.get(Model, id)`` proves nothing about who the row belongs to: the
# caller has to have established that separately, and every one of these is a
# place where PR-04 has to make the scope explicit instead.
LEGACY_BARE_ID_READS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "vitals.services.alerts_service": frozenset({"SystemAlert"}),
        "vitals.services.body_scan_service": frozenset({"RawPayload"}),
        "vitals.services.garmin_service": frozenset({"RawPayload"}),
        "vitals.services.garmin_weight_service": frozenset({"AppSetting"}),
        "vitals.services.hrt_service": frozenset({"HrtCompound"}),
        "vitals.services.labs_service": frozenset({"RawPayload"}),
        "vitals.services.language_service": frozenset({"AppSetting"}),
        "vitals.services.proactive.inbound": frozenset({"RawPayload"}),
        "vitals.services.twofa_service": frozenset({"AppSetting"}),
    }
)


def bare_id_read_total() -> int:
    """Return how many module/model bare-key reads remain."""

    return sum(len(models) for models in LEGACY_BARE_ID_READS.values())


__all__ = [
    "LEGACY_BARE_ID_READS",
    "LEGACY_SCOPE_BRIDGES",
    "bare_id_read_total",
    "bridge_total",
]
