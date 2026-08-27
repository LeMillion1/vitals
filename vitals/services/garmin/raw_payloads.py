"""Raw-first Garmin validation and owned historical reparse."""

from __future__ import annotations

import logging
import uuid
from datetime import date as date_type, timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import Source
from vitals.models.garmin import DOMAIN, GarminActivity, GarminDaily
from vitals.models.raw_payload import RawPayload
from vitals.ownership import WriteIdentity
from vitals.services import raw_payload_service
from vitals.services.conflicts import engine
from vitals.services.garmin.errors import (
    GarminOwnershipValidationError,
    GarminRawPayloadInvariantError,
)
from vitals.services.garmin.normalization import _activity_external_id
from vitals.services.garmin.ownership import (
    _load_owned_garmin_connection,
    _lock_owned_garmin_scope,
    _owned_single_row_candidate,
)
from vitals.services.weight import governance as weight_governance
from vitals.utils.timeutils import now_local

logger = logging.getLogger(__name__)


def _owned_weight_write_context(
    *,
    identity: WriteIdentity,
    on_date: date_type,
) -> engine.ConflictWriteContext:
    return engine.ConflictWriteContext(
        identity=identity,
        evaluation_date=on_date,
        legacy_bridge=engine.LegacyConflictBridge.FULLY_UNOWNED,
    )


def _require_matching_normalized_raw_link(
    row: GarminDaily | GarminActivity,
    *,
    raw_payload_id: int,
) -> None:
    """Never make a historical reparse replace another raw root."""

    if row.raw_payload_id not in {None, raw_payload_id}:
        raise GarminRawPayloadInvariantError(
            "normalized Garmin row already references a different raw payload"
        )


def _validate_owned_raw_row(
    raw_row: RawPayload,
    *,
    identity: WriteIdentity,
    integration_connection_id: uuid.UUID,
    source: str,
    external_id: str,
) -> None:
    expected = {
        "subject_id": identity.subject_id,
        "integration_connection_id": integration_connection_id,
        "domain": DOMAIN,
        "source": source,
        "external_id": external_id,
    }
    for field_name, expected_value in expected.items():
        if getattr(raw_row, field_name) != expected_value:
            raise GarminRawPayloadInvariantError(
                f"raw payload {field_name} does not match owned Garmin context"
            )
    if raw_row.file_asset_id is not None:
        raise GarminRawPayloadInvariantError(
            "Garmin account payload cannot reference a file asset"
        )
    if not isinstance(raw_row.payload, dict):
        raise GarminRawPayloadInvariantError(
            "Garmin raw payload must be a JSON object"
        )


async def _validate_intraday_raw_reference(
    session: AsyncSession,
    *,
    raw_payload_id: int,
    on_date: date_type,
    identity: WriteIdentity,
    integration_connection_id: uuid.UUID,
    source: str,
) -> None:
    if not isinstance(raw_payload_id, int) or isinstance(raw_payload_id, bool):
        raise GarminOwnershipValidationError(
            "raw_payload_id must be an integer or None"
        )
    with session.no_autoflush:
        raw_row = await session.scalar(
            select(RawPayload)
            .where(RawPayload.id == raw_payload_id)
            .with_for_update()
        )
    if raw_row is None:
        raise GarminRawPayloadInvariantError(
            "intraday raw payload does not exist"
        )
    _validate_owned_raw_row(
        raw_row,
        identity=identity,
        integration_connection_id=integration_connection_id,
        source=source,
        external_id=f"daily:{on_date.isoformat()}",
    )


async def _locked_owned_raw_context(
    session: AsyncSession,
    raw_row: RawPayload,
) -> tuple[RawPayload, WriteIdentity, uuid.UUID]:
    """Reload and derive a reparse context from durable raw provenance."""

    if (
        not isinstance(raw_row, RawPayload)
        or not isinstance(raw_row.id, int)
        or isinstance(raw_row.id, bool)
    ):
        raise GarminOwnershipValidationError(
            "raw_row must be a persisted RawPayload"
        )
    with session.no_autoflush:
        preliminary = await session.scalar(
            select(RawPayload)
            .where(RawPayload.id == raw_row.id)
            .execution_options(populate_existing=True)
        )
    if preliminary is None:
        raise GarminRawPayloadInvariantError("raw payload no longer exists")
    if not isinstance(preliminary.subject_id, uuid.UUID):
        raise GarminRawPayloadInvariantError(
            "raw payload has no subject ownership"
        )
    if not isinstance(preliminary.integration_connection_id, uuid.UUID):
        raise GarminRawPayloadInvariantError(
            "raw payload has no Garmin connection ownership"
        )
    scope_identity = WriteIdentity(
        subject_id=preliminary.subject_id,
        actor_user_id=None,
    )
    preliminary_connection_id = preliminary.integration_connection_id
    await _lock_owned_garmin_scope(
        session,
        identity=scope_identity,
        integration_connection_id=preliminary_connection_id,
        allow_retired=True,
    )
    with session.no_autoflush:
        persisted = await session.scalar(
            select(RawPayload)
            .where(RawPayload.id == raw_row.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    if persisted is None:
        raise GarminRawPayloadInvariantError("raw payload no longer exists")
    if (
        persisted.subject_id != scope_identity.subject_id
        or persisted.integration_connection_id != preliminary_connection_id
    ):
        raise GarminRawPayloadInvariantError(
            "raw payload ownership changed during reparse"
        )
    try:
        identity = WriteIdentity(
            subject_id=persisted.subject_id,
            actor_user_id=persisted.actor_user_id,
        )
    except TypeError as exc:
        raise GarminRawPayloadInvariantError(
            "raw payload actor ownership is invalid"
        ) from exc
    return persisted, identity, persisted.integration_connection_id


def _owned_daily_date(raw_row: RawPayload) -> date_type:
    external_id = raw_row.external_id or ""
    if not external_id.startswith("daily:"):
        raise GarminRawPayloadInvariantError(
            "daily reparse requires a daily: external_id"
        )
    value = external_id.removeprefix("daily:")
    try:
        on_date = date_type.fromisoformat(value)
    except ValueError:
        raise GarminRawPayloadInvariantError(
            "daily raw payload has an invalid external date"
        ) from None
    if external_id != f"daily:{on_date.isoformat()}":
        raise GarminRawPayloadInvariantError(
            "daily raw payload external_id is not canonical"
        )
    return on_date


def _owned_hae_date(raw_row: RawPayload) -> date_type:
    external_id = raw_row.external_id or ""
    if not external_id.startswith("hae:"):
        raise GarminRawPayloadInvariantError(
            "Health Auto Export reparse requires a hae: external_id"
        )
    value = external_id.removeprefix("hae:")
    try:
        on_date = date_type.fromisoformat(value)
    except ValueError:
        raise GarminRawPayloadInvariantError(
            "Health Auto Export raw payload has an invalid external date"
        ) from None
    if external_id != f"hae:{on_date.isoformat()}":
        raise GarminRawPayloadInvariantError(
            "Health Auto Export raw external_id is not canonical"
        )
    return on_date


def _owned_activity_id(raw_row: RawPayload) -> str:
    external_id = raw_row.external_id or ""
    if not external_id.startswith("activity:"):
        raise GarminRawPayloadInvariantError(
            "activity reparse requires an activity: external_id"
        )
    value = external_id.removeprefix("activity:")
    if not value or external_id != f"activity:{value}":
        raise GarminRawPayloadInvariantError(
            "activity raw payload external_id is not canonical"
        )
    payload_id = _activity_external_id(raw_row.payload)
    if payload_id != value:
        raise GarminRawPayloadInvariantError(
            "activity payload id does not match raw external_id"
        )
    return value


async def reparse_owned_daily_from_raw(
    session: AsyncSession,
    raw_row: RawPayload,
) -> GarminDaily:
    """Reparse a daily row from its durable owned raw root."""

    from vitals.services.garmin_weight import contracts as garmin_weight_contracts
    from vitals.services.garmin_weight import outbox as garmin_weight_outbox
    import vitals.services.garmin.ingestion as ingestion

    on_date_hint = _owned_daily_date(raw_row)
    if raw_row.subject_id is None:
        raise GarminRawPayloadInvariantError(
            "owned daily reparse requires a subject root"
        )
    preliminary_identity = WriteIdentity(
        raw_row.subject_id,
        raw_row.actor_user_id,
    )
    resolved_export = (
        await garmin_weight_outbox.resolve_optional_legacy_export_context(
            session,
            actor_username=None,
        )
    )
    prepared_weight_write = await weight_governance.prepare_weight_write(
        session,
        context=_owned_weight_write_context(
            identity=preliminary_identity,
            on_date=on_date_hint,
        ),
        garmin_weight_export_context=(
            garmin_weight_contracts.GarminWeightExportContext(
                identity=preliminary_identity,
                integration_connection_id=(
                    resolved_export.integration_connection_id
                ),
                legacy_bridge=resolved_export.legacy_bridge,
            )
            if resolved_export is not None
            else None
        ),
    )
    persisted, identity, connection_id = await _locked_owned_raw_context(
        session,
        raw_row,
    )
    on_date = _owned_daily_date(persisted)
    if identity != preliminary_identity or on_date != on_date_hint:
        raise GarminRawPayloadInvariantError(
            "daily raw ownership changed during prepared reparse"
        )
    if persisted.source != Source.GARMIN_API.value:
        raise GarminRawPayloadInvariantError(
            "daily raw payload must use Garmin API provenance"
        )
    _validate_owned_raw_row(
        persisted,
        identity=identity,
        integration_connection_id=connection_id,
        source=Source.GARMIN_API.value,
        external_id=f"daily:{on_date.isoformat()}",
    )
    candidate = await _owned_single_row_candidate(
        session,
        model=GarminDaily,
        natural_clause=GarminDaily.date == on_date,
        identity=identity,
        integration_connection_id=connection_id,
        key_label=persisted.external_id,
    )
    if candidate is not None:
        _require_matching_normalized_raw_link(
            candidate,
            raw_payload_id=persisted.id,
        )
    return await ingestion._apply_owned_daily_raw(
        session,
        on_date,
        raw_row=persisted,
        identity=identity,
        integration_connection_id=connection_id,
        source=Source.GARMIN_API.value,
        candidate=candidate,
        prepared_weight_write=prepared_weight_write,
    )


async def reparse_owned_health_auto_export_from_raw(
    session: AsyncSession,
    raw_row: RawPayload,
) -> GarminDaily:
    """Reapply one owned HAE partial payload without clobbering other columns."""

    import vitals.services.garmin.ingestion as ingestion

    persisted, identity, connection_id = await _locked_owned_raw_context(
        session,
        raw_row,
    )
    on_date = _owned_hae_date(persisted)
    _validate_owned_raw_row(
        persisted,
        identity=identity,
        integration_connection_id=connection_id,
        source=Source.HEALTH_AUTO_EXPORT.value,
        external_id=f"hae:{on_date.isoformat()}",
    )
    candidate = await _owned_single_row_candidate(
        session,
        model=GarminDaily,
        natural_clause=GarminDaily.date == on_date,
        identity=identity,
        integration_connection_id=connection_id,
        key_label=f"daily:{on_date.isoformat()}",
    )
    if candidate is not None:
        _require_matching_normalized_raw_link(
            candidate,
            raw_payload_id=persisted.id,
        )
    return await ingestion._apply_owned_hae_raw(
        session,
        on_date,
        raw_row=persisted,
        identity=identity,
        integration_connection_id=connection_id,
        candidate=candidate,
    )


async def reparse_owned_activity_from_raw(
    session: AsyncSession,
    raw_row: RawPayload,
) -> GarminActivity:
    """Reparse an activity without re-upserting or rebinding its raw root."""

    import vitals.services.garmin.ingestion as ingestion

    persisted, identity, connection_id = await _locked_owned_raw_context(
        session,
        raw_row,
    )
    _validate_owned_raw_row(
        persisted,
        identity=identity,
        integration_connection_id=connection_id,
        source=Source.GARMIN_API.value,
        external_id=persisted.external_id or "",
    )
    external_id = _owned_activity_id(persisted)
    candidate = await _owned_single_row_candidate(
        session,
        model=GarminActivity,
        natural_clause=GarminActivity.external_id == external_id,
        identity=identity,
        integration_connection_id=connection_id,
        key_label=persisted.external_id,
    )
    if candidate is not None:
        _require_matching_normalized_raw_link(
            candidate,
            raw_payload_id=persisted.id,
        )
    return await ingestion._apply_owned_activity_raw(
        session,
        raw_row=persisted,
        external_id=external_id,
        identity=identity,
        integration_connection_id=connection_id,
        candidate=candidate,
    )


async def reparse_owned_from_raw(
    session: AsyncSession,
    raw_row: RawPayload,
) -> None:
    """Dispatch a durable owned raw root without caller-owned context."""

    external_id = raw_row.external_id or ""
    if external_id.startswith("daily:"):
        await reparse_owned_daily_from_raw(session, raw_row)
    elif external_id.startswith("hae:"):
        await reparse_owned_health_auto_export_from_raw(session, raw_row)
    elif external_id.startswith("activity:"):
        await reparse_owned_activity_from_raw(session, raw_row)
    else:
        raise GarminRawPayloadInvariantError(
            f"unrecognized owned Garmin raw external_id: {external_id!r}"
        )


async def reparse_owned_pending(
    session: AsyncSession,
    *,
    identity: WriteIdentity,
    integration_connection_id: uuid.UUID,
    limit: int = raw_payload_service.REPARSE_BATCH,
    since_days: int = raw_payload_service.REPARSE_WINDOW_DAYS,
) -> int:
    """Sweep one explicit Garmin scope, isolating reparses in savepoints."""

    await _load_owned_garmin_connection(
        session,
        identity=identity,
        integration_connection_id=integration_connection_id,
        allow_retired=True,
    )
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise GarminOwnershipValidationError("limit must be a positive integer")
    if (
        not isinstance(since_days, int)
        or isinstance(since_days, bool)
        or since_days < 0
    ):
        raise GarminOwnershipValidationError(
            "since_days must be a non-negative integer"
        )

    has_normalized = or_(
        select(GarminDaily.id)
        .where(
            GarminDaily.raw_payload_id == RawPayload.id,
            GarminDaily.subject_id == RawPayload.subject_id,
            GarminDaily.integration_connection_id
            == RawPayload.integration_connection_id,
        )
        .exists(),
        select(GarminActivity.id)
        .where(
            GarminActivity.raw_payload_id == RawPayload.id,
            GarminActivity.subject_id == RawPayload.subject_id,
            GarminActivity.integration_connection_id
            == RawPayload.integration_connection_id,
        )
        .exists(),
    )
    cutoff = now_local() - timedelta(days=since_days)
    stmt = (
        select(RawPayload.id)
        .where(
            RawPayload.subject_id == identity.subject_id,
            RawPayload.integration_connection_id == integration_connection_id,
            RawPayload.domain == DOMAIN,
            RawPayload.source.in_(
                [
                    Source.GARMIN_API.value,
                    Source.HEALTH_AUTO_EXPORT.value,
                ]
            ),
            RawPayload.processed_at.is_(None),
            RawPayload.fetched_at >= cutoff,
            ~has_normalized,
        )
        .order_by(RawPayload.id)
        .limit(limit)
    )
    raw_ids = list(await session.scalars(stmt))
    done = 0
    for raw_row_id in raw_ids:
        try:
            async with session.begin_nested():
                raw_row = await session.scalar(
                    select(RawPayload)
                    .where(RawPayload.id == raw_row_id)
                    .execution_options(populate_existing=True)
                )
                if raw_row is None:
                    raise GarminRawPayloadInvariantError(
                        "raw payload disappeared during reparse sweep"
                    )
                if (
                    raw_row.subject_id != identity.subject_id
                    or raw_row.integration_connection_id
                    != integration_connection_id
                ):
                    raise GarminRawPayloadInvariantError(
                        "raw payload changed ownership during reparse sweep"
                    )
                await reparse_owned_from_raw(session, raw_row)
                raw_row.processed_at = now_local()
                await session.flush()
        except Exception:
            logger.warning(
                "owned Garmin re-parse failed for raw payload %s",
                raw_row_id,
                exc_info=True,
            )
            continue
        done += 1
    return done
