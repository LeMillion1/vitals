"""Prepared subject-scoped GeneticVariant mutations."""

from __future__ import annotations

from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import Source
from vitals.models.genetics import DOMAIN, GeneticVariant
from vitals.models.raw_payload import RawPayload
from vitals.ownership import WriteIdentity
from vitals.services.conflicts import engine

from vitals.services.genetics.contracts import (
    PATCH_UNSET,
    GeneticsOwnershipError,
    GeneticsRawProvenanceError,
    GeneticsValidationError,
)
from vitals.services.genetics.validation import (
    _load_raw,
    _normalize_rsid,
    _require_scoped_prepared_write,
    _subject_owner_user_id,
    _subject_scope,
    _validate_raw_owner,
    _validate_raw_shape,
    _validate_source,
    _validate_variant_graph,
    _variant_is_fully_unowned,
)


async def _lock_scoped_variant(
    session: AsyncSession,
    variant_id: int,
    *,
    context: engine.ConflictWriteContext,
) -> GeneticVariant | None:
    row = await session.scalar(
        select(GeneticVariant)
        .where(
            GeneticVariant.id == variant_id,
            _subject_scope(
                context.identity.subject_id,
            ),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        candidate = await session.scalar(
            select(GeneticVariant)
            .where(GeneticVariant.id == variant_id)
            .execution_options(populate_existing=True)
        )
        if (
            candidate is not None
            and candidate.subject_id is None
            and candidate.actor_user_id is not None
        ):
            raise GeneticsOwnershipError("genetic variant has partial ownership provenance")
        return None
    await _validate_variant_graph(
        session,
        row=row,
        subject_id=context.identity.subject_id,
        for_update=True,
    )
    return row


async def add_variant(
    session: AsyncSession,
    *,
    gene: str,
    rsid: Optional[str] = None,
    genotype: Optional[str] = None,
    marker: Optional[str] = None,
    impact: Optional[str] = None,
    impact_domain: Optional[str] = None,
    interpretation: Optional[str] = None,
    action_notes: Optional[str] = None,
    source: str = Source.MANUAL.value,
    raw_payload_id: int | None = None,
    identity: WriteIdentity,
    prepared_conflict_write: engine.PreparedConflictWrite,
) -> GeneticVariant:
    context = _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    if context is not None:
        _validate_source(source)
        owner_user_id = await _subject_owner_user_id(session, identity.subject_id)
        if identity.actor_user_id != owner_user_id:
            raise engine.ConflictPreparedWriteError(
                "genetics writes require the subject owner actor"
            )
        if source in {Source.MANUAL.value, Source.MCP.value}:
            if raw_payload_id is not None:
                raise GeneticsRawProvenanceError(
                    "manual and MCP genetics facts require null raw provenance"
                )
        else:
            if raw_payload_id is None:
                raise GeneticsRawProvenanceError("owned VCF genetics facts require raw provenance")
            raw = await _load_raw(session, raw_payload_id, for_update=True)
            _validate_raw_shape(raw)
            _validate_raw_owner(
                raw,
                subject_id=identity.subject_id,
                actor_user_id=identity.actor_user_id,
            )
    if not isinstance(gene, str) or not gene.strip():
        raise GeneticsValidationError("gene must be a non-blank string")
    normalized_rsid = None if rsid is None else _normalize_rsid(rsid)
    row = GeneticVariant(
        subject_id=identity.subject_id,
        actor_user_id=identity.actor_user_id,
        domain=DOMAIN,
        source=source,
        raw_payload_id=raw_payload_id,
        gene=gene.strip(),
        rsid=normalized_rsid,
        genotype=genotype,
        marker=marker,
        impact=impact,
        impact_domain=impact_domain,
        interpretation=interpretation,
        action_notes=action_notes,
    )
    session.add(row)
    await session.flush()
    return row


def _apply_patch(row: GeneticVariant, **values: object) -> None:
    for field, value in values.items():
        if value is not PATCH_UNSET:
            setattr(row, field, value)


async def _lock_by_rsid(
    session: AsyncSession,
    *,
    rsid: str,
    context: engine.ConflictWriteContext | None,
    replacement_raw: RawPayload | None = None,
) -> GeneticVariant | None:
    rsid = _normalize_rsid(rsid)
    stmt = select(GeneticVariant).where(func.lower(GeneticVariant.rsid) == rsid)
    if context is not None:
        stmt = stmt.where(
            _subject_scope(
                context.identity.subject_id,
            )
        )
    rows = list(
        await session.scalars(stmt.with_for_update().execution_options(populate_existing=True))
    )
    if len(rows) > 1:
        raise GeneticsOwnershipError("multiple genetic variants occupy one rsID")
    if rows:
        row = rows[0]
        if context is not None:
            if (
                replacement_raw is not None
                and row.source == Source.VCF_IMPORT.value
                and row.raw_payload_id is None
            ):
                if row.domain != DOMAIN:
                    raise GeneticsOwnershipError("genetic variant has an invalid domain")
                owner_user_id = await _subject_owner_user_id(
                    session,
                    context.identity.subject_id,
                )
                valid_fact_root = (
                    row.subject_id == context.identity.subject_id
                    and row.actor_user_id in {owner_user_id, None}
                )
                if not valid_fact_root:
                    raise GeneticsOwnershipError(
                        "unlinked VCF fact has foreign or partial ownership"
                    )
                _validate_raw_shape(replacement_raw)
                _validate_raw_owner(
                    replacement_raw,
                    subject_id=context.identity.subject_id,
                    actor_user_id=owner_user_id,
                )
            else:
                await _validate_variant_graph(
                    session,
                    row=row,
                    subject_id=context.identity.subject_id,
                    for_update=True,
                )
        return row
    return None


async def upsert_by_rsid(
    session: AsyncSession,
    *,
    gene: str,
    rsid: str,
    genotype: object = PATCH_UNSET,
    marker: object = PATCH_UNSET,
    impact: object = PATCH_UNSET,
    impact_domain: object = PATCH_UNSET,
    interpretation: object = PATCH_UNSET,
    action_notes: object = PATCH_UNSET,
    source: str = Source.VCF_IMPORT.value,
    raw_payload_id: int | None = None,
    identity: WriteIdentity,
    prepared_conflict_write: engine.PreparedConflictWrite,
) -> GeneticVariant:
    """Locked rsID upsert; corrections preserve origin/source/raw provenance."""

    normalized_rsid = _normalize_rsid(rsid)
    if not isinstance(gene, str) or not gene.strip():
        raise GeneticsValidationError("gene must be a non-blank string")
    context = _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    if context is not None:
        _validate_source(source)
        owner_user_id = await _subject_owner_user_id(session, identity.subject_id)
        if identity.actor_user_id != owner_user_id:
            raise engine.ConflictPreparedWriteError(
                "genetics writes require the subject owner actor"
            )
        if source in {Source.MANUAL.value, Source.MCP.value} and raw_payload_id is not None:
            raise GeneticsRawProvenanceError(
                "manual and MCP genetics facts require null raw provenance"
            )
    row = await _lock_by_rsid(
        session,
        rsid=normalized_rsid,
        context=context,
    )
    if row is None:
        return await add_variant(
            session,
            gene=gene,
            rsid=normalized_rsid,
            genotype=(None if genotype is PATCH_UNSET else genotype),  # type: ignore[arg-type]
            marker=None if marker is PATCH_UNSET else marker,  # type: ignore[arg-type]
            impact=None if impact is PATCH_UNSET else impact,  # type: ignore[arg-type]
            impact_domain=(None if impact_domain is PATCH_UNSET else impact_domain),  # type: ignore[arg-type]
            interpretation=(None if interpretation is PATCH_UNSET else interpretation),  # type: ignore[arg-type]
            action_notes=(None if action_notes is PATCH_UNSET else action_notes),  # type: ignore[arg-type]
            source=source,
            raw_payload_id=raw_payload_id,
            identity=identity,
            prepared_conflict_write=prepared_conflict_write,
        )
    # Provenance is immutable on correction. The bridge may fill the entirely
    # absent legacy owner roots once, under the prepared S/A proof.
    if context is not None and _variant_is_fully_unowned(row):
        row.subject_id = context.identity.subject_id
    row.rsid = normalized_rsid
    row.gene = gene.strip()
    _apply_patch(
        row,
        genotype=genotype,
        marker=marker,
        impact=impact,
        impact_domain=impact_domain,
        interpretation=interpretation,
        action_notes=action_notes,
    )
    await session.flush()
    return row


async def delete_variant(
    session: AsyncSession,
    variant_id: int,
    *,
    identity: WriteIdentity,
    prepared_conflict_write: engine.PreparedConflictWrite,
) -> bool:
    context = _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    if True:
        owner_user_id = await _subject_owner_user_id(session, identity.subject_id)
        if identity.actor_user_id != owner_user_id:
            raise engine.ConflictPreparedWriteError(
                "genetics writes require the subject owner actor"
            )
        row = await _lock_scoped_variant(
            session,
            variant_id,
            context=context,
        )
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    return True
