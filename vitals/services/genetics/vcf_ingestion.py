"""Raw-first VCF persistence and normalized replacement."""

from __future__ import annotations

from typing import Optional, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import Source
from vitals.models.genetics import DOMAIN, GeneticVariant
from vitals.models.raw_payload import RawPayload
from vitals.ownership import WriteIdentity
from vitals.services import raw_payload_service
from vitals.services.conflicts import engine
from vitals.services.genetics.vcf import INTERPRETATIONS, ParsedVariant, interpret
from vitals.utils.timeutils import now_local

from vitals.services.genetics.contracts import (
    MAX_RAW_VARIANTS,
    VCF_RAW_FORMAT_VERSION,
    GeneticsRawProvenanceError,
    GeneticsValidationError,
    VcfIngestSummary,
)
from vitals.services.genetics.validation import (
    _load_raw,
    _materialize_variants,
    _raw_normalization_variants,
    _reject_partial_legacy_raws,
    _require_scoped_prepared_write,
    _subject_owner_user_id,
    _validate_raw_owner,
    _validate_raw_shape,
    _vcf_external_id,
    _variant_is_fully_unowned,
)
from vitals.services.genetics.writes import _lock_by_rsid


async def _replace_vcf_rows(
    session: AsyncSession,
    *,
    parsed: Sequence[ParsedVariant],
    only_interpreted: bool,
    raw: RawPayload,
    context: engine.ConflictWriteContext,
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
    prepared_conflict_write: engine.PreparedConflictWrite,
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
        raise GeneticsValidationError("curated_variants contains an rsID outside INTERPRETATIONS")
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
        raise engine.ConflictPreparedWriteError(
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
        "variants": [[item.rsid, item.ref, item.alt, item.genotype] for item in raw_items],
        "curated_variants": [
            [item.rsid, item.ref, item.alt, item.genotype] for item in curated_items
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
    prepared_conflict_write: engine.PreparedConflictWrite,
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
        "VCF ingestion requires ingest_vcf_batch with a subject and a prepared conflict write"
    )
