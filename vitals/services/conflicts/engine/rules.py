"""Conflict rule catalog integrity and scoped loading."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Sequence

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import Domain
from vitals.models.conflict_rule import ConflictRule
from vitals.services.conflicts.engine.contracts import (
    ConflictCatalogIntegrityError,
    ConflictScope,
)
from vitals.services.conflicts.engine.scope import _domain_value, _validate_scope

_CURATED_RULE_FIELDS = (
    "rule_type",
    "domain_a",
    "condition_a",
    "domain_b",
    "condition_b",
    "severity",
    "message",
    "params",
    "category",
    "source",
    "evidence",
)


def _curated_rule_definitions() -> dict[str, dict[str, Any]]:
    from vitals.services.conflicts.catalog import load_rule_catalog

    return {entry["code"]: entry for entry in load_rule_catalog()}


def _require_catalog_rule_integrity(
    rows: Sequence[ConflictRule],
    catalog: Mapping[str, Mapping[str, Any]],
) -> None:
    for row in rows:
        if row.subject_id is not None or row.code is None:
            continue
        definition = catalog.get(row.code)
        if definition is None:
            raise ConflictCatalogIntegrityError("unrecognized global conflict rule provenance")
        if any(
            getattr(row, field_name) != definition.get(field_name)
            for field_name in _CURATED_RULE_FIELDS
        ):
            raise ConflictCatalogIntegrityError(
                "global conflict rule differs from the checked-in catalog"
            )


async def load_scoped_rules(
    session: AsyncSession,
    *,
    scope: ConflictScope,
    domain: Domain | str | None = None,
    active_only: bool = True,
) -> Sequence[ConflictRule]:
    """Load global definitions plus custom rules of exactly one subject."""

    await _validate_scope(session, scope)
    return await _load_scoped_rules_unchecked(
        session,
        scope=scope,
        domain=domain,
        active_only=active_only,
    )


async def _load_scoped_rules_unchecked(
    session: AsyncSession,
    *,
    scope: ConflictScope,
    domain: Domain | str | None,
    active_only: bool = True,
) -> Sequence[ConflictRule]:
    catalog = _curated_rule_definitions()
    curated_codes = tuple(catalog)
    # A portable ``code`` value is not itself provenance: only membership in the
    # checked-in catalog can classify an S=NULL definition as global. An
    # unclassified S=NULL row is legacy custom state and is accepted only by the
    # exact-one bridge.
    ownership_scope = or_(
        ConflictRule.subject_id == scope.subject_id,
        and_(
            ConflictRule.subject_id.is_(None),
            ConflictRule.code.in_(curated_codes),
        ),
    )
    if scope.include_legacy_unowned:
        ownership_scope = or_(
            ownership_scope,
            and_(
                ConflictRule.subject_id.is_(None),
                ConflictRule.code.is_(None),
            ),
        )
    result = await session.execute(
        select(ConflictRule).where(ownership_scope).order_by(ConflictRule.id)
    )
    rows = result.scalars().all()
    # Authenticate every candidate before trusting mutable DB domain columns.
    # Otherwise a forged catalog row can move itself out of the requested
    # domain and silently disable a checked-in safety definition before the
    # integrity check ever sees it.
    _require_catalog_rule_integrity(rows, catalog)
    # Curated definitions are global, but their activation belongs to the
    # selected health subject.  Import lazily because the activation service
    # reuses ``LegacyConflictBridge`` as part of its public typed contract.
    from vitals.services.conflicts import activation

    activation_state = await activation.read_activation_state(
        session,
        subject_id=scope.subject_id,
        legacy_bridge=scope.legacy_bridge,
    )
    rule_activation = activation.effective_rule_activation(
        rows,
        activation_state,
    )
    if active_only:
        rows = [row for row in rows if rule_activation[row.id]]
    if domain is not None:
        domain_value = _domain_value(domain)
        rows = [row for row in rows if row.domain_a == domain_value or row.domain_b == domain_value]
    return rows
