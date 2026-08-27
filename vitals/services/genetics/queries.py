"""Bounded, provenance-validated Genetics reads."""

from __future__ import annotations

import uuid
from typing import Sequence

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import Source
from vitals.models.genetics import GeneticVariant
from vitals.models.raw_payload import RawPayload
from vitals.services.conflicts import engine

from vitals.services.genetics.contracts import (
    MAX_LIST_LIMIT,
    BoundedVariantPage,
    GeneticsOwnershipError,
    GeneticsRawProvenanceError,
    GeneticsValidationError,
)
from vitals.services.genetics.validation import (
    _load_raw,
    _raw_is_fully_unowned,
    _reject_partial_legacy_rows,
    _subject_owner_user_id,
    _subject_scope,
    _validate_filter,
    _validate_limit,
    _validate_variant_graph,
)


async def list_variants(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    gene: str | None = None,
    rsid: str | None = None,
    limit: int | None = None,
) -> Sequence[GeneticVariant]:
    gene = _validate_filter("gene", gene)
    rsid = _validate_filter("rsid", rsid)
    _validate_limit(limit)
    if not isinstance(subject_id, uuid.UUID):
        raise GeneticsValidationError("subject_id must be a UUID")
    stmt = (
        select(GeneticVariant)
        .where(_subject_scope(subject_id))
        .execution_options(populate_existing=True)
    )
    if gene is not None:
        stmt = stmt.where(func.lower(GeneticVariant.gene) == gene.lower())
    if rsid is not None:
        stmt = stmt.where(func.lower(GeneticVariant.rsid) == rsid.lower())
    if True:
        # A variant with an actor but no subject is broken provenance, not
        # merely another person's row.
        await _reject_partial_legacy_rows(session, gene=gene, rsid=rsid)
        high_water_id = int(
            await session.scalar(stmt.with_only_columns(func.max(GeneticVariant.id)).order_by(None))
            or 0
        )
        last_validated_id = 0
        while last_validated_id < high_water_id:
            validation_rows = list(
                await session.scalars(
                    stmt.where(
                        GeneticVariant.id > last_validated_id,
                        GeneticVariant.id <= high_water_id,
                    )
                    .order_by(GeneticVariant.id)
                    .limit(MAX_LIST_LIMIT)
                )
            )
            if not validation_rows:
                break
            raw_rsid_cache: dict[int, frozenset[str]] = {}
            for row in validation_rows:
                await _validate_variant_graph(
                    session,
                    row=row,
                    subject_id=subject_id,
                    for_update=False,
                    raw_rsid_cache=raw_rsid_cache,
                )
            last_validated_id = validation_rows[-1].id
            if len(validation_rows) < MAX_LIST_LIMIT:
                break

    stmt = stmt.order_by(
        func.lower(GeneticVariant.gene),
        func.lower(GeneticVariant.rsid),
        GeneticVariant.id,
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    rows = list(await session.scalars(stmt))
    if subject_id is not None:
        raw_rsid_cache: dict[int, frozenset[str]] = {}
        for row in rows:
            await _validate_variant_graph(
                session,
                row=row,
                subject_id=subject_id,
                for_update=False,
                raw_rsid_cache=raw_rsid_cache,
            )
    return rows


async def bounded_variants(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    limit: int = MAX_LIST_LIMIT,
) -> BoundedVariantPage:
    """Validate only the bounded page that an authorized care screen renders."""

    _validate_limit(limit)
    if limit is None:  # Kept explicit for type checkers; validation allows None.
        raise GeneticsValidationError("bounded genetics reads require a limit")
    if not isinstance(subject_id, uuid.UUID):
        raise GeneticsValidationError("subject_id must be a UUID")
    await _reject_partial_legacy_rows(session, gene=None, rsid=None)
    stmt = (
        select(GeneticVariant)
        .where(_subject_scope(subject_id))
        .order_by(
            func.lower(GeneticVariant.gene),
            case((GeneticVariant.rsid.is_(None), 1), else_=0),
            func.lower(GeneticVariant.rsid),
            GeneticVariant.id,
        )
        .limit(limit + 1)
        .execution_options(populate_existing=True)
    )
    candidates = list(await session.scalars(stmt))
    rows = candidates[:limit]
    raw_rsid_cache: dict[int, frozenset[str]] = {}
    raw_cache: dict[int, RawPayload] = {}
    owner_user_id = await _subject_owner_user_id(session, subject_id)
    for row in rows:
        await _validate_variant_graph(
            session,
            row=row,
            subject_id=subject_id,
            for_update=False,
            raw_rsid_cache=raw_rsid_cache,
            raw_cache=raw_cache,
            owner_user_id=owner_user_id,
        )
    return BoundedVariantPage(
        rows=tuple(rows),
        truncated=len(candidates) > limit,
    )


async def get_variant(
    session: AsyncSession,
    variant_id: int,
    *,
    subject_id: uuid.UUID,
) -> GeneticVariant | None:
    if not isinstance(variant_id, int) or isinstance(variant_id, bool) or variant_id < 1:
        raise GeneticsValidationError("variant_id must be a positive integer")
    if not isinstance(subject_id, uuid.UUID):
        raise GeneticsValidationError("subject_id must be a UUID")
    candidate = await session.scalar(
        select(GeneticVariant)
        .where(GeneticVariant.id == variant_id)
        .execution_options(populate_existing=True)
    )
    if candidate is None:
        return None
    if candidate.subject_id is None and candidate.actor_user_id is not None:
        raise GeneticsOwnershipError("genetic variant has partial ownership provenance")
    if candidate.subject_id != subject_id:
        return None
    await _validate_variant_graph(
        session,
        row=candidate,
        subject_id=subject_id,
        for_update=False,
    )
    return candidate


async def legacy_unowned_present(session: AsyncSession) -> bool:
    """Mirror of this module's widening in :func:`resolve_variants_scoped`.

    Kept beside it so the two change together: the engine skips its
    sole-subject proof when every probe says no, so a probe that missed a row
    its resolver would still adopt is the one way this goes wrong.
    """

    found = await session.scalar(
        select(GeneticVariant.id)
        .where(
            GeneticVariant.subject_id.is_(None),
            GeneticVariant.actor_user_id.is_(None),
        )
        .limit(1)
    )
    return found is not None


async def resolve_variants_scoped(
    session: AsyncSession,
    *,
    scope: engine.ConflictScope,
) -> list[dict]:
    """Resolve one subject's markers for the conflict engine.

    The engine still offers callers a fully-unowned bridge, and a resolver has
    to honour the scope it is handed; this is the last reader in the module that
    can see a row with no subject. It goes when that bridge does.
    """

    variants = list(await list_variants(session, subject_id=scope.subject_id))
    if scope.include_legacy_unowned:
        # The bridge adds rows that belong to nobody. Their graph carries no
        # subject to validate against, so what is proved instead is that any
        # raw they cite belongs to nobody either — a raw with an actor and no
        # subject is broken provenance, not legacy data.
        bridged = list(
            await session.scalars(
                select(GeneticVariant).where(
                    GeneticVariant.subject_id.is_(None),
                    GeneticVariant.actor_user_id.is_(None),
                )
            )
        )
        for row in bridged:
            if row.raw_payload_id is None:
                continue
            if row.source in {Source.MANUAL.value, Source.MCP.value}:
                raise GeneticsRawProvenanceError(
                    "manual and MCP genetics facts require null raw provenance"
                )
            raw = await _load_raw(session, row.raw_payload_id, for_update=False)
            if not _raw_is_fully_unowned(raw):
                raise GeneticsRawProvenanceError(
                    "VCF raw payload has partial legacy S/A/C/F provenance"
                )
        variants.extend(bridged)
    return [
        {"marker": row.marker, "gene": row.gene, "genotype": row.genotype}
        for row in variants
        if row.marker
    ]
