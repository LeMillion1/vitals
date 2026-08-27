"""Idempotent reparsing of owned pending VCF raw payloads."""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import Source
from vitals.models.genetics import DOMAIN, GeneticVariant
from vitals.models.raw_payload import RawPayload
from vitals.ownership import WriteIdentity
from vitals.services import raw_payload_service
from vitals.services.conflicts import engine
from vitals.utils.timeutils import now_local

from vitals.services.genetics.contracts import (
    GeneticsRawProvenanceError,
    GeneticsServiceError,
    GeneticsValidationError,
)
from vitals.services.genetics.validation import (
    _load_raw,
    _raw_is_fully_unowned,
    _raw_normalization_variants,
    _raw_only_interpreted,
    _reject_partial_legacy_raws,
    _require_scoped_prepared_write,
    _subject_owner_user_id,
    _validate_raw_owner,
    _validate_raw_shape,
    _validate_variant_graph,
)
from vitals.services.genetics.vcf_ingestion import _replace_vcf_rows

logger = logging.getLogger(__name__)


async def reparse_owned_pending(
    session: AsyncSession,
    *,
    identity: WriteIdentity,
    prepared_conflict_write: engine.PreparedConflictWrite,
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
    if not isinstance(since_days, int) or isinstance(since_days, bool) or since_days < 0:
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
                origin_actor = None if is_fully_legacy else probe.actor_user_id
                origin_identity = WriteIdentity(identity.subject_id, origin_actor)
                row_context = engine.ConflictWriteContext(
                    identity=origin_identity,
                    evaluation_date=outer_context.evaluation_date,
                    legacy_bridge=engine.LegacyConflictBridge.REJECT,
                )
                prepared = await engine.prepare_scoped_write(
                    session,
                    context=row_context,
                )
                context = engine.require_prepared_identity(
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
        except (GeneticsServiceError, engine.ConflictScopeError):
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
