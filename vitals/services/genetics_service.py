"""Genetics reference service (Phase 3).

Static reference rows (added by hand or via ``scripts/import_vcf.py``). Their
``marker`` slugs are exposed to the conflict engine via :func:`resolve_variants`,
so a carrier variant can block a contraindicated supplement.
"""
from __future__ import annotations

from typing import Optional, Sequence

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import Source
from vitals.models.genetics import DOMAIN, GeneticVariant
from vitals.models.raw_payload import RawPayload
from vitals.services import conflict_engine

# How many parsed VCF rows one import keeps in the data lake.
# ponytail: a consumer genome is ~600k rsID rows; storing all of them would put a
# ~20 MB JSON blob in one raw_payloads row (and hold it in RAM while parsing), so
# the import keeps the first 50k. If a re-parse ever needs the whole genome,
# store the gzipped file on disk and keep only its path here.
MAX_RAW_VARIANTS = 50_000


async def list_variants(session: AsyncSession) -> Sequence[GeneticVariant]:
    result = await session.execute(
        select(GeneticVariant).order_by(GeneticVariant.gene)
    )
    return result.scalars().all()


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
) -> GeneticVariant:
    row = GeneticVariant(
        domain=DOMAIN,
        source=source,
        gene=gene,
        rsid=rsid,
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


async def upsert_by_rsid(
    session: AsyncSession,
    *,
    gene: str,
    rsid: str,
    genotype: Optional[str] = None,
    marker: Optional[str] = None,
    impact: Optional[str] = None,
    impact_domain: Optional[str] = None,
    interpretation: Optional[str] = None,
    action_notes: Optional[str] = None,
    source: str = Source.VCF_IMPORT.value,
) -> GeneticVariant:
    """Insert or update a variant keyed by rsid (used by the VCF importer so a
    re-import refreshes genotypes instead of duplicating)."""
    result = await session.execute(
        select(GeneticVariant).where(GeneticVariant.rsid == rsid)
    )
    row = result.scalar_one_or_none()
    if row is None:
        return await add_variant(
            session,
            gene=gene,
            rsid=rsid,
            genotype=genotype,
            marker=marker,
            impact=impact,
            impact_domain=impact_domain,
            interpretation=interpretation,
            action_notes=action_notes,
            source=source,
        )
    row.gene = gene
    row.genotype = genotype
    if marker is not None:
        row.marker = marker
    if impact is not None:
        row.impact = impact
    if impact_domain is not None:
        row.impact_domain = impact_domain
    if interpretation is not None:
        row.interpretation = interpretation
    if action_notes is not None:
        row.action_notes = action_notes
    row.source = source
    await session.flush()
    return row


async def store_raw_vcf(
    session: AsyncSession,
    *,
    filename: Optional[str],
    variants: Sequence[Sequence[str]],
    truncated: bool = False,
) -> None:
    """Keep the parsed rows of an imported VCF in ``raw_payloads``.

    The importer writes only the rsIDs it can interpret today
    (``genetics_vcf.INTERPRETATIONS``); everything else used to be dropped on the
    floor, so expanding that table meant asking the owner to find and re-upload
    the file. Each variant is stored as ``[rsid, ref, alt, genotype]`` — the
    ParsedVariant fields, which is all ``interpret()`` needs to re-derive rows
    later — rather than the original VCF text.
    """
    from vitals.services import raw_payload_service

    await raw_payload_service.upsert_raw_payload(
        session,
        domain=DOMAIN,
        source=Source.VCF_IMPORT.value,
        external_id=(filename or "vcf")[:128],
        payload={"filename": filename, "truncated": truncated, "variants": list(variants)},
    )


async def delete_variant(session: AsyncSession, variant_id: int) -> bool:
    row = await session.get(GeneticVariant, variant_id)
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    return True


async def resolve_variants(session: AsyncSession) -> list[dict]:
    """Conflict-engine resolver: variants as match items (marker slugs)."""
    result = await session.execute(select(GeneticVariant))
    return [
        {"marker": v.marker, "gene": v.gene, "genotype": v.genotype}
        for v in result.scalars().all()
        if v.marker
    ]


async def resolve_variants_scoped(
    session: AsyncSession,
    *,
    scope: conflict_engine.ConflictScope,
) -> list[dict]:
    """Conflict resolver restricted to one subject's genetic facts."""

    exact_raw, fully_unowned_raw = conflict_engine.raw_payload_scope_conditions(
        scope
    )
    fact_scope = GeneticVariant.subject_id == scope.subject_id
    if scope.include_legacy_unowned:
        fact_scope = or_(
            fact_scope,
            and_(
                GeneticVariant.subject_id.is_(None),
                GeneticVariant.actor_user_id.is_(None),
            ),
        )
    allowed_linked_raw = exact_raw
    if scope.include_legacy_unowned:
        # Raw-first backfill may already have attached the exact subject root
        # while its normalized genetics row is still fully legacy-owned.
        allowed_linked_raw = or_(allowed_linked_raw, fully_unowned_raw)
    invalid_raw_id = await session.scalar(
        select(1)
        .select_from(GeneticVariant)
        .outerjoin(
            RawPayload,
            GeneticVariant.raw_payload_id == RawPayload.id,
        )
        .where(
            fact_scope,
            GeneticVariant.raw_payload_id.is_not(None),
            allowed_linked_raw.is_not(True),
        )
        .limit(1)
    )
    if invalid_raw_id is not None:
        raise conflict_engine.ConflictRawOwnershipError(
            "genetic variant links to foreign or partial raw provenance"
        )
    variants = list(
        await session.scalars(
            select(GeneticVariant)
            .outerjoin(
                RawPayload,
                GeneticVariant.raw_payload_id == RawPayload.id,
            )
            .where(
                fact_scope,
                or_(
                    GeneticVariant.raw_payload_id.is_(None),
                    allowed_linked_raw,
                ),
            )
        )
    )
    return [
        {"marker": row.marker, "gene": row.gene, "genotype": row.genotype}
        for row in variants
        if row.marker
    ]
