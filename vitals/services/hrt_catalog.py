"""HRT compound catalog — load + validate + upsert ``hrt_compounds.yaml``.

The compound reference (``vitals/data/hrt_compounds.yaml``) is hand-curated and
checked into the repo (like ``conflict_rules.yaml``). :func:`sync_catalog` upserts
it into ``hrt_compounds`` keyed on the stable ``key`` slug — idempotent, run on
every startup so the DB tracks the checked-in YAML without a data migration per
compound. ``active`` is never overwritten on an existing row: it's the user's
show/hide toggle, mirroring ``conflict_catalog.sync_catalog``.

Multi-ester blends (Sustanon) carry a ``components`` list in the YAML; those are
mirrored into ``hrt_compound_components`` (fully replaced on each sync — the
blend definition is catalog-owned, not user data).
"""
from __future__ import annotations

import logging
import math
from functools import lru_cache
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import DoseUnit, Route, Source
from vitals.models.hrt import DOMAIN, HrtCompound, HrtCompoundComponent

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_COMPOUNDS_PATH = _DATA_DIR / "hrt_compounds.yaml"

_VALID_ROUTES = {r.value for r in Route}
_VALID_UNITS = {u.value for u in DoseUnit}
_VALID_CLASSES = frozenset({
    "testosterone", "nandrolone", "trenbolone", "boldenone", "drostanolone",
    "methenolone", "dhb", "trestolone", "oral_aas", "ai", "serm", "support",
    "gh", "igf", "peptide",
})
_VALID_AROMATIZES = frozenset({"true", "false", "partial"})

# Scalar columns copied verbatim from a catalog entry onto the row. `key` and
# `components` are handled separately; `active`/`source`/`domain` are not
# catalog-owned.
_CATALOG_FIELDS = (
    "name", "name_ru", "compound_class", "ester", "route", "dose_unit",
    "conc_mg_ml", "tablet_mg", "half_life_hours", "active_fraction",
    "aromatizes", "aliases", "note",
)

_REQUIRED_KEYS = ("name", "compound_class", "route", "half_life_hours", "active_fraction")


class HrtCatalogCollisionError(ValueError):
    """A checked-in catalog key is occupied by a non-catalog row."""


def _require_positive_finite(key: str, field: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key}: {field} must be a positive finite number")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{key}: {field} must be a positive finite number")


def _validate_entry(key: str, entry: dict) -> None:
    if not isinstance(entry, dict):
        raise ValueError(f"hrt_compounds.yaml: entry {key!r} is not a mapping")
    missing = [k for k in _REQUIRED_KEYS if entry.get(k) is None]
    if missing:
        raise ValueError(f"hrt_compounds.yaml: {key!r} missing {missing}")
    if entry["compound_class"] not in _VALID_CLASSES:
        raise ValueError(f"{key}: invalid compound_class {entry['compound_class']!r}")
    if entry["route"] not in _VALID_ROUTES:
        raise ValueError(f"{key}: invalid route {entry['route']!r}")
    unit = entry.get("dose_unit", DoseUnit.MG.value)
    if unit not in _VALID_UNITS:
        raise ValueError(f"{key}: invalid dose_unit {unit!r}")
    arom = entry.get("aromatizes")
    if arom is not None and str(arom).lower() not in _VALID_AROMATIZES:
        raise ValueError(f"{key}: invalid aromatizes {arom!r}")
    for field in (
        "conc_mg_ml",
        "tablet_mg",
        "half_life_hours",
        "active_fraction",
    ):
        value = entry.get(field)
        if value is not None:
            _require_positive_finite(key, field, value)
    components = entry.get("components")
    if components is None:
        components = []
    elif not isinstance(components, list):
        raise ValueError(f"{key}: components must be a list")
    for comp in components:
        if not isinstance(comp, dict) or "ester" not in comp or "mg" not in comp:
            raise ValueError(f"{key}: component missing ester/mg: {comp!r}")
        if not isinstance(comp["ester"], str) or not comp["ester"].strip():
            raise ValueError(f"{key}: component ester must be non-empty")
        _require_positive_finite(key, "component mg", comp["mg"])


@lru_cache(maxsize=1)
def load_compound_catalog() -> tuple[tuple[str, dict], ...]:
    """Parse + validate ``hrt_compounds.yaml`` once per process. Raises
    ``ValueError`` on the first malformed entry — a bad catalog should fail
    loudly at startup, not silently seed garbage into the data lake."""
    with open(_COMPOUNDS_PATH, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    if not isinstance(raw, dict):
        raise ValueError("hrt_compounds.yaml: top level must be a mapping of key -> entry")
    items: list[tuple[str, dict]] = []
    for key, entry in raw.items():
        _validate_entry(key, entry)
        items.append((key, entry))
    return tuple(items)


def _normalize_values(entry: dict) -> dict:
    values = {field: entry.get(field) for field in _CATALOG_FIELDS}
    # Normalize the tri-state aromatizes to a lowercase string ('partial' stays).
    if values.get("aromatizes") is not None:
        values["aromatizes"] = str(values["aromatizes"]).lower()
    if values.get("dose_unit") is None:
        values["dose_unit"] = DoseUnit.MG.value
    if values.get("active_fraction") is None:
        values["active_fraction"] = 1.0
    return values


async def _after_catalog_rows_locked_for_test() -> None:
    """Deterministic concurrency-test seam after the catalog row locks."""


async def sync_catalog(session: AsyncSession) -> dict[str, int]:
    """Idempotent upsert of ``hrt_compounds.yaml`` into ``hrt_compounds``, keyed
    on ``key``. Catalog-owned scalar fields are refreshed on existing rows;
    ``active`` (the user's toggle) is left untouched. Blend components are fully
    replaced to match the YAML. Rows are stamped ``source='system'`` so they're
    distinguishable from user-added compounds (``source='manual'``)."""
    catalog = load_compound_catalog()
    catalog_keys = tuple(key for key, _entry in catalog)
    # The curated catalog owns the platform half of the compound key: a
    # subject's own compound may reuse the same key and is not the catalog's to
    # read or rewrite.
    result = await session.execute(
        select(HrtCompound)
        .where(
            HrtCompound.key.in_(catalog_keys),
            HrtCompound.subject_id.is_(None),
        )
        .order_by(HrtCompound.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    existing = {row.key: row for row in result.scalars().all()}
    await _after_catalog_rows_locked_for_test()

    # Validate the entire key namespace before changing any row. A manual/MCP
    # compound may legitimately predate a newly curated key; catalog startup
    # must never overwrite that user's medical definition or its components.
    for key, _entry in catalog:
        row = existing.get(key)
        if row is not None and not (
            row.domain == DOMAIN
            and row.source == Source.SYSTEM.value
            and row.actor_user_id is None
        ):
            raise HrtCatalogCollisionError(
                "HRT catalog synchronization found a protected custom-key collision"
            )

    inserted = 0
    updated = 0
    for key, entry in catalog:
        values = _normalize_values(entry)
        components = entry.get("components") or []
        row = existing.get(key)
        if row is None:
            row = HrtCompound(
                key=key,
                domain=DOMAIN,
                source=Source.SYSTEM.value,
                **values,
            )
            session.add(row)
            inserted += 1
        else:
            for field, value in values.items():
                setattr(row, field, value)
            updated += 1
        # Assign via the relationship so delete-orphan replaces any prior blend
        # components and the in-session collection stays consistent.
        row.components = [
            HrtCompoundComponent(ester=comp["ester"], mg=float(comp["mg"]))
            for comp in components
        ]

    await session.flush()
    logger.info(
        "hrt_catalog.sync_catalog: %d inserted, %d updated (of %d total)",
        inserted, updated, len(catalog),
    )
    return {"inserted": inserted, "updated": updated, "total": len(catalog)}
