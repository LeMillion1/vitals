"""Weight-log mutation orchestration and cross-domain workflow ports."""
from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import date as date_type
from typing import Optional, Protocol, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import Domain, Source
from vitals.models.raw_payload import RawPayload
from vitals.models.weight import DOMAIN, WeightLog
from vitals.ownership import WriteIdentity
from vitals.services.conflicts import engine

from . import governance
from .contracts import (
    PreparedGarminWeightExportProtocol,
    PreparedWeightWrite,
    WeightOwnershipError,
    _ORIGIN_ACTOR_UNSET,
)
from .logs import (
    _assert_weight_scope_integrity,
    _get_weight_log_date_in_scope,
    _get_weight_log_for_update,
    _source_priority,
    _validate_new_weight_provenance,
    _validate_persisted_weight_provenance,
    _weight_entity_key,
    _weight_provenance_is_reusable,
    _weight_scope_condition,
    get_active_weight,
)
from .measurements import _recompute_lbm_for_date, _recompute_lbm_for_date_null

_WEIGHT_KG_RANGE = (20.0, 400.0)


class GarminOutboxPort(Protocol):
    """DB-only Garmin outbox commands required by Weight mutations."""

    async def lock_active_weight_change(self, session: AsyncSession) -> None: ...

    async def handle_active_weight_changed_scoped(
        self,
        session: AsyncSession,
        *,
        prepared: PreparedGarminWeightExportProtocol,
    ) -> object: ...

    async def handle_active_weight_deleted_scoped(
        self,
        session: AsyncSession,
        *,
        prepared: PreparedGarminWeightExportProtocol,
        deleted_id: int,
        on_date: date_type,
        deleted_weight_kg: float,
        replacement: WeightLog | None,
    ) -> object: ...


def _garmin_outbox_port() -> GarminOutboxPort:
    """Resolve the adapter lazily so Garmin may depend on Weight commands."""
    from vitals.services.garmin_weight import outbox, reconciliation

    class _GarminOutboxAdapter:
        lock_active_weight_change = staticmethod(outbox.lock_active_weight_change)
        handle_active_weight_changed_scoped = staticmethod(
            reconciliation.handle_active_weight_changed_scoped
        )
        handle_active_weight_deleted_scoped = staticmethod(
            reconciliation.handle_active_weight_deleted_scoped
        )

    return cast(GarminOutboxPort, _GarminOutboxAdapter)


@dataclass(frozen=True, slots=True)
class BodyScanWeightCommand:
    """Normalized BodyScan→Weight projection without importing BodyScan here."""

    on_date: date_type
    weight_kg: float
    integration_connection_id: uuid.UUID | None
    raw_payload_id: int | None
    origin_actor_user_id: uuid.UUID | None
    override: bool = False
    allow_historical_parser_raw: bool = False


def _check_range(
    name: str,
    value: Optional[float],
    bounds: tuple[float, float],
) -> Optional[float]:
    if value is None:
        return None
    low, high = bounds
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(
            f"{name} must be between {low:g} and {high:g} (got {value!r})"
        )
    return value


async def _prepared_weight_write_for_date(
    session: AsyncSession,
    *,
    identity: WriteIdentity,
    prepared: PreparedWeightWrite,
    on_date: date_type,
) -> PreparedWeightWrite:
    """Reissue an already-proven Weight capability for another fact date."""

    context = governance.require_prepared_weight_identity(
        session,
        prepared=prepared,
        identity=identity,
    )
    if context.evaluation_date == on_date:
        return prepared
    return await governance.prepare_weight_write(
        session,
        context=engine.ConflictWriteContext(
            identity=identity,
            evaluation_date=on_date,
            legacy_bridge=context.legacy_bridge,
        ),
        garmin_weight_export_context=(
            prepared.garmin_weight_export.context
            if prepared.garmin_weight_export is not None
            else None
        ),
    )


async def project_body_scan_weight(
    session: AsyncSession,
    *,
    command: BodyScanWeightCommand,
    identity: WriteIdentity,
    prepared_weight_write: PreparedWeightWrite,
) -> WeightLog:
    """Project one normalized BodyScan weight through Weight's write policy."""
    return await log_weight(
        session,
        on_date=command.on_date,
        weight_kg=command.weight_kg,
        source=Source.BODY_SCAN.value,
        override=command.override,
        identity=identity,
        integration_connection_id=command.integration_connection_id,
        raw_payload_id=command.raw_payload_id,
        prepared_weight_write=prepared_weight_write,
        origin_actor_user_id=command.origin_actor_user_id,
        allow_historical_parser_raw=command.allow_historical_parser_raw,
    )


async def log_weight(
    session: AsyncSession,
    *,
    on_date: date_type,
    weight_kg: float,
    source: str = Source.MANUAL.value,
    raw_payload_id: Optional[int] = None,
    note: Optional[str] = None,
    override: bool = False,
    identity: WriteIdentity,
    integration_connection_id: uuid.UUID | None = None,
    prepared_weight_write: PreparedWeightWrite,
    origin_actor_user_id: uuid.UUID | None | object = _ORIGIN_ACTOR_UNSET,
    allow_historical_parser_raw: bool = False,
) -> WeightLog:
    """Record a Weight fact while preserving active/superseded history."""
    _check_range("weight_kg", weight_kg, _WEIGHT_KG_RANGE)
    if not isinstance(allow_historical_parser_raw, bool):
        raise TypeError("allow_historical_parser_raw must be a bool")
    context = governance._require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_weight_write,
    )
    governance.require_evaluation_date(context, on_date)
    await _validate_new_weight_provenance(
        session,
        context=context,
        source=source,
        integration_connection_id=integration_connection_id,
        raw_payload_id=raw_payload_id,
        origin_actor_user_id=origin_actor_user_id,
        allow_historical_parser_raw=allow_historical_parser_raw,
    )

    garmin_outbox = _garmin_outbox_port()
    await garmin_outbox.lock_active_weight_change(session)

    existing = await get_active_weight(
        session,
        on_date,
        subject_id=identity.subject_id,
        for_update=True,
    )
    if (
        existing is not None
        and existing.source == source
        and existing.weight_kg == weight_kg
        and _weight_provenance_is_reusable(
            existing,
            identity=identity,
            integration_connection_id=integration_connection_id,
            raw_payload_id=raw_payload_id,
        )
    ):
        await _adopt_weight_provenance(
            session,
            existing,
            identity=identity,
            integration_connection_id=integration_connection_id,
            raw_payload_id=raw_payload_id,
        )
        await session.flush()
        return existing

    if (
        existing is not None
        and _source_priority(source) < _source_priority(existing.source)
    ):
        duplicate_stmt = select(WeightLog).where(
            WeightLog.date == on_date,
            WeightLog.source == source,
            WeightLog.weight_kg == weight_kg,
        )
        if identity is not None:
            duplicate_scope = _weight_scope_condition(
                subject_id=identity.subject_id,
                evaluation_date=on_date,
            )
            await _assert_weight_scope_integrity(
                session,
                subject_id=identity.subject_id,
                evaluation_date=on_date,
                filters=(
                    WeightLog.date == on_date,
                    WeightLog.source == source,
                    WeightLog.weight_kg == weight_kg,
                ),
            )
            duplicate_stmt = duplicate_stmt.where(duplicate_scope)
        duplicate_rows = list(
            await session.scalars(
                duplicate_stmt.order_by(WeightLog.id.desc())
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        duplicate = next(
            (
                candidate
                for candidate in duplicate_rows
                if _weight_provenance_is_reusable(
                    candidate,
                    identity=identity,
                    integration_connection_id=integration_connection_id,
                    raw_payload_id=raw_payload_id,
                )
            ),
            None,
        )
        if duplicate is not None:
            await _adopt_weight_provenance(
                session,
                duplicate,
                identity=identity,
                integration_connection_id=integration_connection_id,
                raw_payload_id=raw_payload_id,
            )
            await session.flush()
            return duplicate

    insert_as_active = existing is None or (
        _source_priority(source) >= _source_priority(existing.source)
    )
    if insert_as_active:
        assert prepared_weight_write is not None
        await engine.enforce_prepared(
            session,
            prepared=prepared_weight_write.conflict_write,
            domain=Domain.WEIGHT,
            proposed_state={"weight_kg": weight_kg, "source": source},
            override=override,
            entity_ref=f"weight:{on_date.isoformat()}",
            replace_entity_key=(
                _weight_entity_key(existing) if existing is not None else None
            ),
        )

    if existing is not None and insert_as_active:
        existing.superseded = True
        await session.flush()

    row = WeightLog(
        subject_id=identity.subject_id,
        actor_user_id=(
            identity.actor_user_id
            if origin_actor_user_id is _ORIGIN_ACTOR_UNSET and identity is not None
            else (
                None
                if origin_actor_user_id is _ORIGIN_ACTOR_UNSET
                else origin_actor_user_id
            )
        ),
        integration_connection_id=integration_connection_id,
        date=on_date,
        domain=DOMAIN,
        source=source,
        weight_kg=weight_kg,
        raw_payload_id=raw_payload_id,
        note=note,
        superseded=not insert_as_active,
    )
    session.add(row)
    await session.flush()

    active_weight = weight_kg if insert_as_active else (
        existing.weight_kg if existing else None
    )
    if active_weight is not None:
        await _recompute_lbm_for_date(
            session,
            on_date,
            active_weight,
            subject_id=identity.subject_id,
        )
    if insert_as_active and prepared_weight_write.garmin_weight_export is not None:
        await garmin_outbox.handle_active_weight_changed_scoped(
            session,
            prepared=prepared_weight_write.garmin_weight_export,
        )
    return row


async def _adopt_weight_provenance(
    session: AsyncSession,
    row: WeightLog,
    *,
    identity: WriteIdentity | None,
    integration_connection_id: uuid.UUID | None,
    raw_payload_id: int | None,
) -> None:
    """Attach only missing trusted roots without rewriting actor history."""
    if identity is None:
        return
    await _validate_persisted_weight_provenance(
        session,
        row,
        subject_id=identity.subject_id,
    )
    if row.subject_id not in {None, identity.subject_id}:
        raise WeightOwnershipError("weight fact belongs to another subject")
    if row.subject_id is None and (
        row.actor_user_id is not None
        or row.integration_connection_id is not None
    ):
        raise WeightOwnershipError("partial legacy weight roots cannot be adopted")
    if row.integration_connection_id not in {None, integration_connection_id}:
        raise WeightOwnershipError("weight fact belongs to another origin connection")
    if row.raw_payload_id not in {None, raw_payload_id}:
        raise engine.ConflictRawOwnershipError(
            "weight fact references a different raw payload"
        )
    if row.subject_id is None and row.source == Source.GARMIN_API.value:
        raise WeightOwnershipError(
            "legacy Garmin weight requires provider backfill before adoption"
        )
    if row.subject_id is None and row.raw_payload_id is not None:
        raw = await session.scalar(
            select(RawPayload)
            .where(RawPayload.id == row.raw_payload_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if raw is None:
            raise engine.ConflictRawOwnershipError(
                "legacy weight fact references a missing raw payload"
            )
        if raw.subject_id is None:
            if any(
                root is not None
                for root in (
                    raw.actor_user_id,
                    raw.integration_connection_id,
                    raw.file_asset_id,
                )
            ):
                raise engine.ConflictRawOwnershipError(
                    "partial legacy weight raw roots cannot be adopted"
                )
            raw.subject_id = identity.subject_id
        elif raw.subject_id != identity.subject_id:
            raise engine.ConflictRawOwnershipError(
                "weight raw payload belongs to another subject"
            )
    if row.subject_id is None:
        row.subject_id = identity.subject_id
    if row.integration_connection_id is None:
        row.integration_connection_id = integration_connection_id
    if row.raw_payload_id is None:
        row.raw_payload_id = raw_payload_id
    await _validate_persisted_weight_provenance(
        session,
        row,
        subject_id=identity.subject_id,
    )


async def delete_weight_log(
    session: AsyncSession,
    log_id: int,
    *,
    identity: WriteIdentity,
    prepared_weight_write: PreparedWeightWrite,
) -> bool:
    """Delete a fact and safely select any historical active replacement."""
    context = governance._require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_weight_write,
    )

    effective_prepared = prepared_weight_write
    assert identity is not None and prepared_weight_write is not None
    target_date_hint = await _get_weight_log_date_in_scope(
        session,
        log_id,
        subject_id=identity.subject_id,
        evaluation_date=context.evaluation_date,
    )
    if target_date_hint is None:
        return False
    effective_prepared = await _prepared_weight_write_for_date(
        session,
        identity=identity,
        prepared=prepared_weight_write,
        on_date=target_date_hint,
    )
    context = governance.require_prepared_weight_identity(
        session,
        prepared=effective_prepared,
        identity=identity,
    )

    garmin_outbox = _garmin_outbox_port()
    await garmin_outbox.lock_active_weight_change(session)
    row = await _get_weight_log_for_update(
        session,
        log_id,
        subject_id=identity.subject_id,
        evaluation_date=context.evaluation_date,
    )
    if not row:
        return False
    was_active = not row.superseded
    target_date = row.date
    governance.require_evaluation_date(context, target_date)
    deleted_id = row.id
    deleted_weight_kg = row.weight_kg

    next_row = None
    if was_active:
        remaining_stmt = select(WeightLog).where(
            WeightLog.date == target_date,
            WeightLog.id != row.id,
        )
        if identity is not None:
            remaining_scope = _weight_scope_condition(
                subject_id=identity.subject_id,
                evaluation_date=target_date,
            )
            await _assert_weight_scope_integrity(
                session,
                subject_id=identity.subject_id,
                evaluation_date=target_date,
                filters=(
                    WeightLog.date == target_date,
                    WeightLog.id != row.id,
                ),
            )
            remaining_stmt = remaining_stmt.where(remaining_scope)
        remaining = await session.execute(
            remaining_stmt.order_by(WeightLog.id.desc())
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        rows = remaining.scalars().all()
        if identity is not None:
            for candidate in rows:
                await _validate_persisted_weight_provenance(
                    session,
                    candidate,
                    subject_id=identity.subject_id,
                )
        next_row = max(
            rows,
            key=lambda candidate: (
                _source_priority(candidate.source),
                candidate.id,
            ),
            default=None,
        )
        if next_row is not None:
            try:
                await engine.enforce_prepared(
                    session,
                    prepared=effective_prepared.conflict_write,
                    domain=Domain.WEIGHT,
                    proposed_state={
                        "weight_kg": next_row.weight_kg,
                        "source": next_row.source,
                    },
                    override=False,
                    entity_ref=f"weight:{target_date.isoformat()}",
                    replace_entity_key=_weight_entity_key(row),
                )
            except engine.ConflictBlocked:
                next_row = None

    await session.delete(row)
    await session.flush()

    if was_active:
        if next_row is not None:
            next_row.superseded = False
            await session.flush()
            await _recompute_lbm_for_date(
                session,
                target_date,
                next_row.weight_kg,
                subject_id=identity.subject_id,
            )
        else:
            await _recompute_lbm_for_date_null(
                session,
                target_date,
                subject_id=identity.subject_id,
            )

        if effective_prepared.garmin_weight_export is not None:
            await garmin_outbox.handle_active_weight_deleted_scoped(
                session,
                prepared=effective_prepared.garmin_weight_export,
                deleted_id=deleted_id,
                on_date=target_date,
                deleted_weight_kg=deleted_weight_kg,
                replacement=next_row,
            )
    return True


async def update_weight_note(
    session: AsyncSession,
    log_id: int,
    *,
    note: str,
    identity: WriteIdentity,
    prepared_weight_write: PreparedWeightWrite,
) -> WeightLog | None:
    """Update only a scoped Weight note without changing fact provenance."""
    context = governance._require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_weight_write,
    )
    row = await _get_weight_log_for_update(
        session,
        log_id,
        subject_id=identity.subject_id,
        evaluation_date=context.evaluation_date,
    )
    if row is None:
        return None
    row.note = note
    await session.flush()
    return row


async def update_weight_log(
    session: AsyncSession,
    log_id: int,
    *,
    on_date: date_type,
    weight_kg: float,
    note: Optional[str] = None,
    override: bool = False,
    identity: WriteIdentity,
    prepared_weight_write: PreparedWeightWrite,
) -> Optional[WeightLog]:
    """Edit a Weight fact, moving it atomically when its date changes."""
    context = governance._require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_weight_write,
    )
    governance.require_evaluation_date(context, on_date)

    _check_range("weight_kg", weight_kg, _WEIGHT_KG_RANGE)
    garmin_outbox = _garmin_outbox_port()
    await garmin_outbox.lock_active_weight_change(session)
    row = await _get_weight_log_for_update(
        session,
        log_id,
        subject_id=identity.subject_id,
        evaluation_date=on_date,
    )
    if not row:
        return None

    if row.date != on_date:
        moved = await log_weight(
            session,
            on_date=on_date,
            weight_kg=weight_kg,
            source=row.source,
            raw_payload_id=row.raw_payload_id,
            note=note,
            override=override,
            identity=identity,
            integration_connection_id=row.integration_connection_id,
            prepared_weight_write=prepared_weight_write,
            origin_actor_user_id=row.actor_user_id,
        )
        deleted = await delete_weight_log(
            session,
            log_id,
            identity=identity,
            prepared_weight_write=prepared_weight_write,
        )
        if not deleted:  # pragma: no cover - target is locked above
            raise WeightOwnershipError("weight fact disappeared during date move")
        return moved
    if not row.superseded:
        assert prepared_weight_write is not None
        await engine.enforce_prepared(
            session,
            prepared=prepared_weight_write.conflict_write,
            domain=Domain.WEIGHT,
            proposed_state={"weight_kg": weight_kg, "source": row.source},
            override=override,
            entity_ref=f"weight:{on_date.isoformat()}",
            replace_entity_key=_weight_entity_key(row),
        )
    row.weight_kg = weight_kg
    row.note = note
    await session.flush()
    if not row.superseded:
        await _recompute_lbm_for_date(
            session,
            on_date,
            weight_kg,
            subject_id=identity.subject_id,
        )
        if prepared_weight_write.garmin_weight_export is not None:
            await garmin_outbox.handle_active_weight_changed_scoped(
                session,
                prepared=prepared_weight_write.garmin_weight_export,
            )
    return row
