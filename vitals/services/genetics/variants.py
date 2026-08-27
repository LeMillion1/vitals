"""Subject-scoped genetics facts and raw-first VCF ingestion.

Genetic facts participate in conflict reads, but writing a genetic fact is not
itself a safety decision. Scoped writers consume the conflict engine's prepared
capability as a governance/subject/actor proof without calling ``enforce``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Sequence as SequenceABC
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Optional, Sequence

from sqlalchemy import (
    case,
    func,
    or_,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import Source
from vitals.models.genetics import DOMAIN, GeneticVariant
from vitals.models.identity import HealthSubject
from vitals.models.raw_payload import RawPayload
from vitals.ownership import WriteIdentity
from vitals.services import conflict_engine, raw_payload_service
from vitals.services.genetics.vcf import INTERPRETATIONS, ParsedVariant, interpret
from vitals.utils.timeutils import now_local

logger = logging.getLogger(__name__)

MAX_RAW_VARIANTS = 50_000
MAX_LIST_LIMIT = 100
VCF_RAW_FORMAT_VERSION = 2
PATCH_UNSET: Final = object()


class GeneticsServiceError(ValueError):
    """Base class for typed, fail-closed genetics service failures."""


class GeneticsValidationError(GeneticsServiceError):
    """A caller supplied an invalid genetics value or capability combination."""


class GeneticsOwnershipError(GeneticsServiceError):
    """A genetics fact is outside the requested ownership scope."""


class GeneticsRawProvenanceError(
    GeneticsOwnershipError,
    conflict_engine.ConflictRawOwnershipError,
):
    """A VCF raw/fact provenance graph is missing or inconsistent."""


class GeneticsRsidOccupiedError(GeneticsOwnershipError):
    """This subject already holds a variant for the requested rsID."""


class GeneticsNotFoundError(GeneticsServiceError):
    """A requested scoped genetic variant does not exist."""


@dataclass(frozen=True, slots=True)
class VcfIngestSummary:
    raw: RawPayload | None
    imported: int
    markers: int


@dataclass(frozen=True, slots=True)
class BoundedVariantPage:
    """A provenance-validated, bounded genetics projection."""

    rows: tuple[GeneticVariant, ...]
    truncated: bool


def _require_scoped_prepared_write(
    session: AsyncSession,
    *,
    identity: WriteIdentity | None,
    prepared: conflict_engine.PreparedConflictWrite | None,
) -> conflict_engine.ConflictWriteContext | None:
    if identity is None and prepared is None:
        return None
    if identity is None or prepared is None:
        raise conflict_engine.ConflictPreparedWriteError(
            "scoped genetics writes require identity and a prepared conflict write"
        )
    return conflict_engine.require_prepared_identity(
        session,
        prepared=prepared,
        identity=identity,
    )


def _subject_scope(subject_id: uuid.UUID):
    """A genome belongs to one person; an rsID identifies a locus, not them."""

    return GeneticVariant.subject_id == subject_id


def _raw_is_fully_unowned(raw: RawPayload) -> bool:
    return all(
        value is None
        for value in (
            raw.subject_id,
            raw.actor_user_id,
            raw.integration_connection_id,
            raw.file_asset_id,
        )
    )


def _variant_is_fully_unowned(row: GeneticVariant) -> bool:
    return row.subject_id is None and row.actor_user_id is None


def _validate_filter(name: str, value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise GeneticsValidationError(f"{name} must be a non-blank string or None")
    return value.strip()


def _normalize_rsid(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GeneticsValidationError("rsid must be a non-blank string")
    return value.strip().lower()


def _validate_limit(limit: int | None) -> None:
    if limit is None:
        return
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or limit < 1
        or limit > MAX_LIST_LIMIT
    ):
        raise GeneticsValidationError(
            f"limit must be an integer between 1 and {MAX_LIST_LIMIT}"
        )


async def _reject_partial_legacy_rows(
    session: AsyncSession,
    *,
    gene: str | None = None,
    rsid: str | None = None,
) -> None:
    stmt = select(GeneticVariant.id).where(
        GeneticVariant.subject_id.is_(None),
        GeneticVariant.actor_user_id.is_not(None),
    )
    if gene is not None:
        stmt = stmt.where(func.lower(GeneticVariant.gene) == gene.lower())
    if rsid is not None:
        stmt = stmt.where(func.lower(GeneticVariant.rsid) == rsid.lower())
    if await session.scalar(stmt.limit(1)) is not None:
        raise GeneticsOwnershipError(
            "partial legacy genetic ownership cannot cross the compatibility bridge"
        )


async def _reject_partial_legacy_raws(
    session: AsyncSession,
    *,
    pending_since: datetime | None = None,
) -> None:
    stmt = select(RawPayload.id).where(
        RawPayload.domain == DOMAIN,
        RawPayload.source == Source.VCF_IMPORT.value,
        RawPayload.subject_id.is_(None),
        or_(
            RawPayload.actor_user_id.is_not(None),
            RawPayload.integration_connection_id.is_not(None),
            RawPayload.file_asset_id.is_not(None),
        ),
    )
    if pending_since is not None:
        stmt = stmt.where(
            RawPayload.processed_at.is_(None),
            RawPayload.fetched_at >= pending_since,
        )
    if await session.scalar(
        stmt.order_by(RawPayload.id).limit(1).with_for_update()
    ) is not None:
        raise GeneticsRawProvenanceError(
            "VCF raw payload has partial legacy S/A/C/F provenance"
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
            await session.scalar(
                stmt.with_only_columns(func.max(GeneticVariant.id)).order_by(None)
            )
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
    if (
        not isinstance(variant_id, int)
        or isinstance(variant_id, bool)
        or variant_id < 1
    ):
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
        raise GeneticsOwnershipError(
            "genetic variant has partial ownership provenance"
        )
    if candidate.subject_id != subject_id:
        return None
    await _validate_variant_graph(
        session,
        row=candidate,
        subject_id=subject_id,
        for_update=False,
    )
    return candidate


def _validate_source(source: str) -> None:
    if source not in {
        Source.MANUAL.value,
        Source.MCP.value,
        Source.VCF_IMPORT.value,
    }:
        raise GeneticsValidationError("unsupported genetics provenance source")


async def _load_raw(
    session: AsyncSession,
    raw_payload_id: int,
    *,
    for_update: bool,
) -> RawPayload:
    if (
        not isinstance(raw_payload_id, int)
        or isinstance(raw_payload_id, bool)
        or raw_payload_id < 1
    ):
        raise GeneticsRawProvenanceError(
            "raw_payload_id must identify a persisted VCF raw payload"
        )
    stmt = (
        select(RawPayload)
        .where(RawPayload.id == raw_payload_id)
        .execution_options(populate_existing=True)
    )
    if for_update:
        stmt = stmt.with_for_update()
    raw = await session.scalar(stmt)
    if raw is None:
        raise GeneticsRawProvenanceError("VCF raw payload does not exist")
    return raw


def _vcf_external_id(payload: dict) -> str:
    """Return a stable idempotency key for one bounded VCF import revision."""

    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"vcf:{hashlib.sha256(encoded).hexdigest()}"


def _validate_raw_shape(raw: RawPayload) -> None:
    if raw.domain != DOMAIN or raw.source != Source.VCF_IMPORT.value:
        raise GeneticsRawProvenanceError(
            "genetics facts require a genetics/vcf_import raw payload"
        )
    # VCF uploads are streamed and intentionally have no durable FileAsset or
    # provider connection. Any C/F root is therefore forged provenance.
    if raw.integration_connection_id is not None or raw.file_asset_id is not None:
        raise GeneticsRawProvenanceError(
            "VCF provenance requires null provider connection and file roots"
        )
    if not isinstance(raw.payload, dict):
        raise GeneticsRawProvenanceError("VCF raw payload must be a JSON object")
    has_format_version = "format_version" in raw.payload
    format_version = raw.payload.get("format_version")
    if has_format_version and (
        not isinstance(format_version, int)
        or isinstance(format_version, bool)
        or format_version != VCF_RAW_FORMAT_VERSION
    ):
        raise GeneticsRawProvenanceError("VCF raw format version is unsupported")
    filename = raw.payload.get("filename")
    if filename is not None and not isinstance(filename, str):
        raise GeneticsRawProvenanceError("VCF raw filename must be a string or null")
    if not isinstance(raw.payload.get("variants"), list):
        raise GeneticsRawProvenanceError("VCF raw payload has no parsed variant batch")
    if format_version == VCF_RAW_FORMAT_VERSION and not isinstance(
        raw.payload.get("curated_variants"),
        list,
    ):
        raise GeneticsRawProvenanceError(
            "versioned VCF raw payload has no curated variant evidence"
        )
    if (
        format_version == VCF_RAW_FORMAT_VERSION
        and "only_interpreted" not in raw.payload
    ):
        raise GeneticsRawProvenanceError(
            "versioned VCF raw payload has no replay policy"
        )
    only_interpreted = raw.payload.get("only_interpreted")
    if only_interpreted is not None and not isinstance(only_interpreted, bool):
        raise GeneticsRawProvenanceError(
            "VCF raw only_interpreted policy must be boolean"
        )
    if (
        format_version == VCF_RAW_FORMAT_VERSION
        and "truncated" not in raw.payload
    ):
        raise GeneticsRawProvenanceError(
            "versioned VCF raw payload has no truncation policy"
        )
    truncated = raw.payload.get("truncated", False)
    if not isinstance(truncated, bool):
        raise GeneticsRawProvenanceError(
            "VCF raw truncation flag must be boolean"
        )
    expected_external_id = _vcf_external_id(raw.payload)
    legacy_external_id = (filename or "vcf")[:128]
    valid_external_ids = (
        {expected_external_id}
        if format_version == VCF_RAW_FORMAT_VERSION
        else {legacy_external_id, expected_external_id}
    )
    if raw.external_id not in valid_external_ids:
        raise GeneticsRawProvenanceError(
            "VCF raw revision does not match its external id"
        )


def _validate_raw_owner(
    raw: RawPayload,
    *,
    subject_id: uuid.UUID,
    actor_user_id: uuid.UUID | None,
) -> None:
    exact = (
        raw.subject_id == subject_id
        and raw.actor_user_id == actor_user_id
        and raw.integration_connection_id is None
        and raw.file_asset_id is None
    )
    historical_null_actor = (
        raw.subject_id == subject_id
        and raw.actor_user_id is None
        and raw.integration_connection_id is None
        and raw.file_asset_id is None
    )
    if not exact and not historical_null_actor:
        raise GeneticsRawProvenanceError(
            "VCF raw payload has foreign or partial S/A/C/F provenance"
        )


def _validate_raw_origin_rsid(
    row: GeneticVariant,
    raw: RawPayload,
    *,
    raw_rsid_cache: dict[int, frozenset[str]] | None = None,
) -> None:
    """Prove the linked VCF is origin evidence for this stable rsID.

    A later explicit human/MCP correction may legitimately change the genotype
    while retaining immutable origin provenance, so persisted reads validate the
    stable rsID membership rather than requiring value equality. Versioned raws
    retain every curated tail hit and therefore require membership; only legacy
    truncated first-50k payloads may omit one.
    """

    _validate_raw_shape(raw)
    if not isinstance(row.rsid, str) or not row.rsid.strip():
        raise GeneticsRawProvenanceError("VCF genetics fact has no stable rsID")
    assert isinstance(raw.payload, dict)
    raw_rsids = (
        raw_rsid_cache.get(raw.id)
        if raw_rsid_cache is not None
        else None
    )
    if raw_rsids is None:
        raw_rsids = frozenset(
            _normalize_rsid(item.rsid)
            for item in _raw_normalization_variants(raw)
        )
        if raw_rsid_cache is not None:
            raw_rsid_cache[raw.id] = raw_rsids
    strict_membership = (
        raw.payload.get("format_version") == VCF_RAW_FORMAT_VERSION
        or not raw.payload.get("truncated", False)
    )
    if _normalize_rsid(row.rsid) not in raw_rsids and strict_membership:
        raise GeneticsRawProvenanceError(
            "VCF genetics fact rsID is absent from its durable raw evidence"
        )


async def _subject_owner_user_id(
    session: AsyncSession,
    subject_id: uuid.UUID,
) -> uuid.UUID:
    owner_user_id = await session.scalar(
        select(HealthSubject.owner_user_id)
        .where(HealthSubject.id == subject_id)
        .execution_options(populate_existing=True)
    )
    if owner_user_id is None:
        raise GeneticsOwnershipError("genetics subject has no durable owner")
    return owner_user_id


async def _validate_variant_graph(
    session: AsyncSession,
    *,
    row: GeneticVariant,
    subject_id: uuid.UUID,
    for_update: bool,
    raw_rsid_cache: dict[int, frozenset[str]] | None = None,
    raw_cache: dict[int, RawPayload] | None = None,
    owner_user_id: uuid.UUID | None = None,
) -> None:
    if row.domain != DOMAIN:
        raise GeneticsOwnershipError("genetic variant has an invalid domain")
    if owner_user_id is None:
        owner_user_id = await _subject_owner_user_id(session, subject_id)
    row_is_legacy = _variant_is_fully_unowned(row)
    if row.subject_id == subject_id:
        if row.actor_user_id != owner_user_id and row.actor_user_id is not None:
            raise GeneticsOwnershipError(
                "genetic variant actor is foreign to its subject owner"
            )
    elif not row_is_legacy:
        raise GeneticsOwnershipError("genetic variant belongs to another subject")
    _validate_source(row.source)
    if row.source in {Source.MANUAL.value, Source.MCP.value}:
        if row.raw_payload_id is not None:
            raise GeneticsRawProvenanceError(
                "manual and MCP genetics facts require null raw provenance"
            )
        return
    if row.raw_payload_id is None:
        raise GeneticsRawProvenanceError("VCF genetics fact has no raw provenance")
    raw = raw_cache.get(row.raw_payload_id) if raw_cache is not None else None
    if raw is None:
        raw = await _load_raw(session, row.raw_payload_id, for_update=for_update)
        if raw_cache is not None:
            raw_cache[row.raw_payload_id] = raw
    _validate_raw_shape(raw)
    _validate_raw_origin_rsid(
        row,
        raw,
        raw_rsid_cache=raw_rsid_cache,
    )
    raw_exact_owner = (
        raw.subject_id == subject_id
        and raw.actor_user_id == owner_user_id
        and raw.integration_connection_id is None
        and raw.file_asset_id is None
    )
    raw_exact_historical_null = (
        raw.subject_id == subject_id
        and raw.actor_user_id is None
        and raw.integration_connection_id is None
        and raw.file_asset_id is None
    )
    exact_graph = (
        row.subject_id == subject_id
        and row.actor_user_id == owner_user_id
        and raw_exact_owner
    )
    bridged_graph = (
        (
            row_is_legacy
            and (
                raw_exact_owner
                or raw_exact_historical_null
                or _raw_is_fully_unowned(raw)
            )
        )
        or (
            row.subject_id == subject_id
            and row.actor_user_id in {None, owner_user_id}
            and (
                _raw_is_fully_unowned(raw)
                or raw_exact_historical_null
                or raw_exact_owner
            )
        )
    )
    if not exact_graph and not bridged_graph:
        raise GeneticsRawProvenanceError(
            "VCF fact/raw graph has foreign or partial S/A provenance"
        )


async def _lock_scoped_variant(
    session: AsyncSession,
    variant_id: int,
    *,
    context: conflict_engine.ConflictWriteContext,
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
            raise GeneticsOwnershipError(
                "genetic variant has partial ownership provenance"
            )
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
    prepared_conflict_write: conflict_engine.PreparedConflictWrite,
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
            raise conflict_engine.ConflictPreparedWriteError(
                "genetics writes require the subject owner actor"
            )
        if source in {Source.MANUAL.value, Source.MCP.value}:
            if raw_payload_id is not None:
                raise GeneticsRawProvenanceError(
                    "manual and MCP genetics facts require null raw provenance"
                )
        else:
            if raw_payload_id is None:
                raise GeneticsRawProvenanceError(
                    "owned VCF genetics facts require raw provenance"
                )
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
    context: conflict_engine.ConflictWriteContext | None,
    replacement_raw: RawPayload | None = None,
) -> GeneticVariant | None:
    rsid = _normalize_rsid(rsid)
    stmt = select(GeneticVariant).where(
        func.lower(GeneticVariant.rsid) == rsid
    )
    if context is not None:
        stmt = stmt.where(
            _subject_scope(
                context.identity.subject_id,
            )
        )
    rows = list(
        await session.scalars(
            stmt.with_for_update().execution_options(populate_existing=True)
        )
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
                    raise GeneticsOwnershipError(
                        "genetic variant has an invalid domain"
                    )
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
    prepared_conflict_write: conflict_engine.PreparedConflictWrite,
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
            raise conflict_engine.ConflictPreparedWriteError(
                "genetics writes require the subject owner actor"
            )
        if (
            source in {Source.MANUAL.value, Source.MCP.value}
            and raw_payload_id is not None
        ):
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
            genotype=(
                None if genotype is PATCH_UNSET else genotype
            ),  # type: ignore[arg-type]
            marker=None if marker is PATCH_UNSET else marker,  # type: ignore[arg-type]
            impact=None if impact is PATCH_UNSET else impact,  # type: ignore[arg-type]
            impact_domain=(
                None if impact_domain is PATCH_UNSET else impact_domain
            ),  # type: ignore[arg-type]
            interpretation=(
                None if interpretation is PATCH_UNSET else interpretation
            ),  # type: ignore[arg-type]
            action_notes=(
                None if action_notes is PATCH_UNSET else action_notes
            ),  # type: ignore[arg-type]
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


def _materialize_variants(
    variants: Sequence[ParsedVariant | Sequence[str]],
    *,
    field_name: str,
) -> list[ParsedVariant]:
    if not isinstance(variants, SequenceABC) or isinstance(variants, (str, bytes)):
        raise GeneticsValidationError(f"{field_name} must be a materialized sequence")
    materialized: list[ParsedVariant] = []
    for item in variants:
        if isinstance(item, ParsedVariant):
            parsed = item
        else:
            if (
                not isinstance(item, SequenceABC)
                or isinstance(item, (str, bytes))
                or len(item) != 4
            ):
                raise GeneticsValidationError(
                    f"each {field_name} row must contain rsid/ref/alt/genotype"
                )
            parsed = ParsedVariant(*item)
        if not all(
            isinstance(value, str) and value
            for value in (parsed.rsid, parsed.ref, parsed.alt, parsed.genotype)
        ):
            raise GeneticsValidationError("parsed VCF fields must be non-empty strings")
        materialized.append(
            ParsedVariant(
                rsid=_normalize_rsid(parsed.rsid),
                ref=parsed.ref,
                alt=parsed.alt,
                genotype=parsed.genotype,
            )
        )
    return materialized


def _raw_payload_variants(raw: RawPayload) -> list[ParsedVariant]:
    _validate_raw_shape(raw)
    assert isinstance(raw.payload, dict)
    return _materialize_variants(
        raw.payload["variants"],
        field_name="raw_variants",
    )


def _raw_payload_curated_variants(raw: RawPayload) -> list[ParsedVariant]:
    _validate_raw_shape(raw)
    assert isinstance(raw.payload, dict)
    if raw.payload.get("format_version") != VCF_RAW_FORMAT_VERSION:
        return []
    return _materialize_variants(
        raw.payload["curated_variants"],
        field_name="curated_variants",
    )


def _raw_normalization_variants(raw: RawPayload) -> list[ParsedVariant]:
    """Rebuild every normalized candidate captured by one VCF revision.

    Version 2 stores both the bounded first-50k sample and every curated tail
    hit. Curated evidence may fill a truncated tail but may never contradict an
    overlapping retained row. Legacy payloads keep their historical semantics.
    """

    try:
        retained = _raw_payload_variants(raw)
        curated_items = _raw_payload_curated_variants(raw)
    except GeneticsValidationError as exc:
        raise GeneticsRawProvenanceError(
            "VCF raw payload contains malformed variant evidence"
        ) from exc
    if len(retained) > MAX_RAW_VARIANTS:
        raise GeneticsRawProvenanceError(
            "VCF raw payload exceeds the bounded retained variant limit"
        )
    if raw.payload.get("format_version") == VCF_RAW_FORMAT_VERSION:
        curated_rsids = [item.rsid for item in curated_items]
        if curated_rsids != sorted(set(curated_rsids)):
            raise GeneticsRawProvenanceError(
                "versioned VCF curated evidence must be unique and canonical"
            )
    by_rsid = {item.rsid: item for item in retained}
    for curated in curated_items:
        existing = by_rsid.get(curated.rsid)
        if (
            existing is None
            and raw.payload.get("format_version") == VCF_RAW_FORMAT_VERSION
            and not raw.payload["truncated"]
        ):
            raise GeneticsRawProvenanceError(
                "untruncated curated evidence is absent from retained VCF rows"
            )
        if existing is not None and (
            existing.ref,
            existing.alt,
            existing.genotype,
        ) != (
            curated.ref,
            curated.alt,
            curated.genotype,
        ):
            raise GeneticsRawProvenanceError(
                "curated variant contradicts the retained raw VCF evidence"
            )
        by_rsid[curated.rsid] = curated
    return [by_rsid[rsid] for rsid in sorted(by_rsid)]


def _raw_only_interpreted(raw: RawPayload) -> bool:
    """Return the durable replay policy; unknown legacy imports stay narrow."""

    _validate_raw_shape(raw)
    assert isinstance(raw.payload, dict)
    value = raw.payload.get("only_interpreted")
    # Pre-cutover payloads did not persist this flag. Replaying only marker-bearing
    # rows avoids manufacturing informational facts the original owner may have
    # explicitly excluded; re-upload can opt into the broader catalog later.
    return True if value is None else value


async def _replace_vcf_rows(
    session: AsyncSession,
    *,
    parsed: Sequence[ParsedVariant],
    only_interpreted: bool,
    raw: RawPayload,
    context: conflict_engine.ConflictWriteContext,
) -> tuple[int, int]:
    # Last occurrence wins, then rsID sorting gives every worker the same lock
    # order and prevents a batch from updating one rsID twice.
    by_rsid = {variant.rsid: variant for variant in parsed}
    selected: list[tuple[str, dict]] = []
    for rsid in sorted(by_rsid):
        variant = by_rsid[rsid]
        if rsid not in INTERPRETATIONS:
            continue
        fields = interpret(variant)
        selected.append((rsid, fields))

    imported = 0
    markers = 0
    for rsid, fields in selected:
        row = await _lock_by_rsid(
            session,
            rsid=rsid,
            context=context,
            replacement_raw=raw,
        )
        if row is None and only_interpreted and not fields.get("marker"):
            continue
        if row is None:
            row = GeneticVariant(
                subject_id=context.identity.subject_id,
                actor_user_id=raw.actor_user_id,
                domain=DOMAIN,
                source=Source.VCF_IMPORT.value,
                raw_payload_id=raw.id,
                gene=fields["gene"],
                rsid=rsid,
            )
            session.add(row)
        elif row.source in {Source.MANUAL.value, Source.MCP.value}:
            # A human/MCP fact is an explicit correction, not parser-owned state.
            # A later bulk import must not silently replace its content while
            # retaining human provenance or attach unrelated VCF provenance.
            continue
        else:
            if row.raw_payload_id not in {None, raw.id}:
                current_raw = await _load_raw(
                    session,
                    row.raw_payload_id,
                    for_update=True,
                )
                _validate_raw_shape(current_raw)
                if (current_raw.fetched_at, current_raw.id) > (
                    raw.fetched_at,
                    raw.id,
                ):
                    # A delayed sweep must not roll a normalized fact back to
                    # older evidence. The old raw is still marked processed by
                    # its caller, while the fact and marker remain untouched.
                    continue
            if _variant_is_fully_unowned(row):
                # Preserve the historical null actor while adopting the subject.
                row.subject_id = context.identity.subject_id
                row.raw_payload_id = raw.id
            elif row.source == Source.VCF_IMPORT.value:
                # Parser replacement makes this raw the evidence for the new
                # genotype/marker. Keeping an older link would make the normalized
                # fact contradict its purported source when filenames rotate.
                row.raw_payload_id = raw.id
        row.gene = fields["gene"]
        row.rsid = rsid
        row.genotype = fields.get("genotype")
        row.marker = fields.get("marker")
        row.impact = fields.get("impact")
        row.impact_domain = fields.get("impact_domain")
        row.interpretation = fields.get("interpretation")
        row.action_notes = fields.get("action_notes")
        if not only_interpreted or row.marker:
            imported += 1
            markers += int(bool(row.marker))
    await session.flush()
    return imported, markers


async def ingest_vcf_batch(
    session: AsyncSession,
    *,
    filename: str | None,
    raw_variants: Sequence[ParsedVariant | Sequence[str]],
    curated_variants: Sequence[ParsedVariant | Sequence[str]],
    truncated: bool,
    only_interpreted: bool = False,
    identity: WriteIdentity,
    prepared_conflict_write: conflict_engine.PreparedConflictWrite,
) -> VcfIngestSummary:
    """Persist bounded raw rows first, then normalize the tiny curated batch."""

    if not isinstance(only_interpreted, bool) or not isinstance(truncated, bool):
        raise GeneticsValidationError("VCF batch flags must be booleans")
    raw_items = _materialize_variants(raw_variants, field_name="raw_variants")
    curated_items = _materialize_variants(
        curated_variants,
        field_name="curated_variants",
    )
    if len(raw_items) > MAX_RAW_VARIANTS:
        raise GeneticsValidationError(
            "raw_variants exceeds MAX_RAW_VARIANTS; stream and cap at the boundary"
        )
    if any(item.rsid not in INTERPRETATIONS for item in curated_items):
        raise GeneticsValidationError(
            "curated_variants contains an rsID outside INTERPRETATIONS"
        )
    # A truncated payload may omit a curated hit found after the retained raw
    # prefix. When a curated rsID is present in that prefix, however, its last
    # occurrence must agree with the last normalized occurrence; otherwise the
    # newly linked evidence is affirmatively contradictory.
    raw_last = {item.rsid: item for item in raw_items}
    curated_last = {item.rsid: item for item in curated_items}
    curated_items = [curated_last[rsid] for rsid in sorted(curated_last)]
    for rsid, curated in curated_last.items():
        raw = raw_last.get(rsid)
        if raw is None:
            if truncated:
                continue
            raise GeneticsRawProvenanceError(
                "untruncated curated variants must be present in raw_variants"
            )
        if (raw.ref, raw.alt, raw.genotype) != (
            curated.ref,
            curated.alt,
            curated.genotype,
        ):
            raise GeneticsRawProvenanceError(
                "curated variant contradicts the retained raw VCF evidence"
            )
    if filename is not None and not isinstance(filename, str):
        raise GeneticsValidationError("filename must be a string or None")
    context = _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    assert context is not None
    owner_user_id = await _subject_owner_user_id(session, identity.subject_id)
    if identity.actor_user_id != owner_user_id:
        raise conflict_engine.ConflictPreparedWriteError(
            "initial owned VCF ingestion requires the subject owner actor"
        )
    # Preserve the established header-only behavior: capability/ownership was
    # validated, but no raw or normalized row is manufactured.
    if not raw_items and not curated_items:
        return VcfIngestSummary(raw=None, imported=0, markers=0)
    if not raw_items:
        raise GeneticsValidationError(
            "curated VCF rows must also be represented in the bounded raw batch"
        )
    payload = {
        "format_version": VCF_RAW_FORMAT_VERSION,
        "filename": filename,
        "truncated": truncated,
        "only_interpreted": only_interpreted,
        "variants": [
            [item.rsid, item.ref, item.alt, item.genotype]
            for item in raw_items
        ],
        "curated_variants": [
            [item.rsid, item.ref, item.alt, item.genotype]
            for item in curated_items
        ],
    }

    def validate_locked_existing(candidate: RawPayload) -> None:
        _validate_raw_shape(candidate)
        _raw_normalization_variants(candidate)
        _validate_raw_owner(
            candidate,
            subject_id=identity.subject_id,
            actor_user_id=identity.actor_user_id,
        )

    # A raw payload with an actor but no subject is broken provenance.
    await _reject_partial_legacy_raws(session)
    raw = await raw_payload_service.upsert_owned_raw_payload(
        session,
        identity=identity,
        integration_connection_id=None,
        file_asset_id=None,
        domain=DOMAIN,
        source=Source.VCF_IMPORT.value,
        external_id=_vcf_external_id(payload),
        payload=payload,
        validate_locked_existing=validate_locked_existing,
    )
    _validate_raw_shape(raw)
    _validate_raw_owner(
        raw,
        subject_id=identity.subject_id,
        actor_user_id=identity.actor_user_id,
    )
    imported, markers = await _replace_vcf_rows(
        session,
        parsed=curated_items,
        only_interpreted=only_interpreted,
        raw=raw,
        context=context,
    )
    raw.processed_at = now_local()
    await session.flush()
    return VcfIngestSummary(raw=raw, imported=imported, markers=markers)


async def store_raw_vcf(
    session: AsyncSession,
    *,
    filename: Optional[str],
    variants: Sequence[Sequence[str]],
    truncated: bool = False,
    identity: WriteIdentity,
    prepared_conflict_write: conflict_engine.PreparedConflictWrite,
) -> RawPayload:
    """Retired raw-only adapter. Use ``ingest_vcf_batch``.

    This already refused every scoped caller; what remained underneath was the
    zero-subject arm, which stored the uploaded VCF as a payload belonging to
    nobody. A genome is the most identifying record the application holds, so
    that arm goes rather than gets scoped — ``ingest_vcf_batch`` takes the
    subject and the conflict decision together and is the only way in.
    """

    del session, filename, variants, truncated, identity, prepared_conflict_write
    raise GeneticsValidationError(
        "VCF ingestion requires ingest_vcf_batch with a subject and a "
        "prepared conflict write"
    )


async def delete_variant(
    session: AsyncSession,
    variant_id: int,
    *,
    identity: WriteIdentity,
    prepared_conflict_write: conflict_engine.PreparedConflictWrite,
) -> bool:
    context = _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    if True:
        owner_user_id = await _subject_owner_user_id(session, identity.subject_id)
        if identity.actor_user_id != owner_user_id:
            raise conflict_engine.ConflictPreparedWriteError(
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


async def legacy_unowned_present(session: AsyncSession) -> bool:
    """Mirror of this module's widening in :func:`resolve_variants_scoped`.

    Kept beside it so the two change together: the engine skips its
    sole-subject proof when every probe says no, so a probe that missed a row
    its resolver would still adopt is the one way this goes wrong.
    """

    found = await session.scalar(
        select(GeneticVariant.id)
        .where(GeneticVariant.subject_id.is_(None),
            GeneticVariant.actor_user_id.is_(None),)
        .limit(1)
    )
    return found is not None


async def resolve_variants_scoped(
    session: AsyncSession,
    *,
    scope: conflict_engine.ConflictScope,
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


async def reparse_owned_pending(
    session: AsyncSession,
    *,
    identity: WriteIdentity,
    prepared_conflict_write: conflict_engine.PreparedConflictWrite,
    limit: int = raw_payload_service.REPARSE_BATCH,
    since_days: int = raw_payload_service.REPARSE_WINDOW_DAYS,
) -> int:
    """Replay pending VCF raws, including partially normalized batches.

    Each raw is isolated by a savepoint and is marked processed only after the
    complete bounded raw batch has normalized and flushed successfully.
    """

    outer_context = _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    assert outer_context is not None
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise GeneticsValidationError("limit must be a positive integer")
    if (
        not isinstance(since_days, int)
        or isinstance(since_days, bool)
        or since_days < 0
    ):
        raise GeneticsValidationError("since_days must be a non-negative integer")
    cutoff = now_local() - timedelta(days=since_days)
    await _reject_partial_legacy_raws(session, pending_since=cutoff)

    raw_scope = RawPayload.subject_id == identity.subject_id
    raw_ids = list(
        await session.scalars(
            select(RawPayload.id)
            .where(
                raw_scope,
                RawPayload.domain == DOMAIN,
                RawPayload.source == Source.VCF_IMPORT.value,
                RawPayload.processed_at.is_(None),
                RawPayload.fetched_at >= cutoff,
            )
            .order_by(RawPayload.id)
            .limit(limit)
        )
    )

    # Provenance corruption is a batch boundary failure, not a parser failure to
    # log and silently skip. Validate raw roots and every existing child first.
    owner_user_id = await _subject_owner_user_id(session, identity.subject_id)
    raw_rsid_cache: dict[int, frozenset[str]] = {}
    for raw_id in raw_ids:
        raw = await _load_raw(session, raw_id, for_update=False)
        _validate_raw_shape(raw)
        _validate_raw_owner(
            raw,
            subject_id=identity.subject_id,
            actor_user_id=owner_user_id,
        )
        linked = list(
            await session.scalars(
                select(GeneticVariant)
                .where(GeneticVariant.raw_payload_id == raw_id)
                .order_by(GeneticVariant.id)
                .execution_options(populate_existing=True)
            )
        )
        for row in linked:
            try:
                await _validate_variant_graph(
                    session,
                    row=row,
                    subject_id=identity.subject_id,
                    for_update=False,
                    raw_rsid_cache=raw_rsid_cache,
                )
            except GeneticsServiceError as exc:
                raise GeneticsRawProvenanceError(
                    "pending VCF raw links to foreign or partial normalized provenance"
                ) from exc

    done = 0
    for raw_id in raw_ids:
        try:
            async with session.begin_nested():
                probe = await _load_raw(session, raw_id, for_update=False)
                is_fully_legacy = _raw_is_fully_unowned(probe)
                origin_actor = (
                    None if is_fully_legacy else probe.actor_user_id
                )
                origin_identity = WriteIdentity(identity.subject_id, origin_actor)
                row_context = conflict_engine.ConflictWriteContext(
                    identity=origin_identity,
                    evaluation_date=outer_context.evaluation_date,
                    legacy_bridge=conflict_engine.LegacyConflictBridge.REJECT,
                )
                prepared = await conflict_engine.prepare_scoped_write(
                    session,
                    context=row_context,
                )
                context = conflict_engine.require_prepared_identity(
                    session,
                    prepared=prepared,
                    identity=origin_identity,
                )
                raw = await _load_raw(session, raw_id, for_update=True)
                if raw.processed_at is not None:
                    continue
                _validate_raw_shape(raw)
                _validate_raw_owner(
                    raw,
                    subject_id=identity.subject_id,
                    actor_user_id=owner_user_id,
                )
                await _replace_vcf_rows(
                    session,
                    parsed=_raw_normalization_variants(raw),
                    only_interpreted=_raw_only_interpreted(raw),
                    raw=raw,
                    context=context,
                )
                raw.processed_at = now_local()
                await session.flush()
        except (GeneticsServiceError, conflict_engine.ConflictScopeError):
            raise
        except Exception:
            logger.warning(
                "owned genetics re-parse failed for raw payload %s",
                raw_id,
                exc_info=True,
            )
            continue
        done += 1
    return done
