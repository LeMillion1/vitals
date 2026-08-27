"""Ownership, provenance, and VCF shape validation."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence as SequenceABC
from datetime import datetime
from typing import Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import Source
from vitals.models.genetics import DOMAIN, GeneticVariant
from vitals.models.identity import HealthSubject
from vitals.models.raw_payload import RawPayload
from vitals.ownership import WriteIdentity
from vitals.services.conflicts import engine
from vitals.services.genetics.vcf import ParsedVariant

from vitals.services.genetics.contracts import (
    MAX_LIST_LIMIT,
    MAX_RAW_VARIANTS,
    VCF_RAW_FORMAT_VERSION,
    GeneticsOwnershipError,
    GeneticsRawProvenanceError,
    GeneticsValidationError,
)


def _require_scoped_prepared_write(
    session: AsyncSession,
    *,
    identity: WriteIdentity | None,
    prepared: engine.PreparedConflictWrite | None,
) -> engine.ConflictWriteContext | None:
    if identity is None and prepared is None:
        return None
    if identity is None or prepared is None:
        raise engine.ConflictPreparedWriteError(
            "scoped genetics writes require identity and a prepared conflict write"
        )
    return engine.require_prepared_identity(
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
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > MAX_LIST_LIMIT:
        raise GeneticsValidationError(f"limit must be an integer between 1 and {MAX_LIST_LIMIT}")


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
    if await session.scalar(stmt.order_by(RawPayload.id).limit(1).with_for_update()) is not None:
        raise GeneticsRawProvenanceError("VCF raw payload has partial legacy S/A/C/F provenance")


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
        raise GeneticsRawProvenanceError("raw_payload_id must identify a persisted VCF raw payload")
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
        raise GeneticsRawProvenanceError("genetics facts require a genetics/vcf_import raw payload")
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
    if format_version == VCF_RAW_FORMAT_VERSION and "only_interpreted" not in raw.payload:
        raise GeneticsRawProvenanceError("versioned VCF raw payload has no replay policy")
    only_interpreted = raw.payload.get("only_interpreted")
    if only_interpreted is not None and not isinstance(only_interpreted, bool):
        raise GeneticsRawProvenanceError("VCF raw only_interpreted policy must be boolean")
    if format_version == VCF_RAW_FORMAT_VERSION and "truncated" not in raw.payload:
        raise GeneticsRawProvenanceError("versioned VCF raw payload has no truncation policy")
    truncated = raw.payload.get("truncated", False)
    if not isinstance(truncated, bool):
        raise GeneticsRawProvenanceError("VCF raw truncation flag must be boolean")
    expected_external_id = _vcf_external_id(raw.payload)
    legacy_external_id = (filename or "vcf")[:128]
    valid_external_ids = (
        {expected_external_id}
        if format_version == VCF_RAW_FORMAT_VERSION
        else {legacy_external_id, expected_external_id}
    )
    if raw.external_id not in valid_external_ids:
        raise GeneticsRawProvenanceError("VCF raw revision does not match its external id")


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
    raw_rsids = raw_rsid_cache.get(raw.id) if raw_rsid_cache is not None else None
    if raw_rsids is None:
        raw_rsids = frozenset(
            _normalize_rsid(item.rsid) for item in _raw_normalization_variants(raw)
        )
        if raw_rsid_cache is not None:
            raw_rsid_cache[raw.id] = raw_rsids
    strict_membership = raw.payload.get(
        "format_version"
    ) == VCF_RAW_FORMAT_VERSION or not raw.payload.get("truncated", False)
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
            raise GeneticsOwnershipError("genetic variant actor is foreign to its subject owner")
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
        row.subject_id == subject_id and row.actor_user_id == owner_user_id and raw_exact_owner
    )
    bridged_graph = (
        row_is_legacy
        and (raw_exact_owner or raw_exact_historical_null or _raw_is_fully_unowned(raw))
    ) or (
        row.subject_id == subject_id
        and row.actor_user_id in {None, owner_user_id}
        and (_raw_is_fully_unowned(raw) or raw_exact_historical_null or raw_exact_owner)
    )
    if not exact_graph and not bridged_graph:
        raise GeneticsRawProvenanceError("VCF fact/raw graph has foreign or partial S/A provenance")


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
