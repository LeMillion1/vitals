"""Ownership graph, scoped capability, validation, and advisory locking."""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date as date_type

from sqlalchemy import and_, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import (
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
    UserStatus,
)
from vitals.models.garmin import GarminWeightExport
from vitals.models.identity import HealthSubject, User
from vitals.models.tenancy import IntegrationConnection
from vitals.models.weight import WeightLog
from vitals.services.conflicts import engine
from vitals.services.garmin_weight.contracts import (
    GarminWeightExportConnectionInactiveError,
    GarminWeightExportContext,
    GarminWeightExportLegacyBridgeError,
    GarminWeightExportOwnershipError,
    GarminWeightExportPreparedError,
    PreparedGarminWeightExport,
    _require_prepared_export,
)
from vitals.services.identity.governance import acquire_identity_governance_lock

_DB_OPERATION_LOCK_ID = 0x564954414C535747
_SCOPED_EXPORT: ContextVar[PreparedGarminWeightExport | None] = ContextVar(
    "garmin_weight_scoped_export", default=None
)


async def prepare_scoped_export(
    session: AsyncSession,
    *,
    context: GarminWeightExportContext,
    historical: bool = False,
) -> PreparedGarminWeightExport:
    """Prepare one scoped outbox transaction in the canonical global order."""

    if not isinstance(context, GarminWeightExportContext):
        raise TypeError("context must be a GarminWeightExportContext")
    if not isinstance(historical, bool):
        raise TypeError("historical must be a boolean")
    await acquire_identity_governance_lock(session)
    await _acquire_operation_lock(session)
    with session.no_autoflush:
        if (
            context.legacy_bridge is engine.LegacyConflictBridge.FULLY_UNOWNED
            and await legacy_unowned_outbox_present(session)
        ):
            subject_ids = list(
                await session.scalars(select(HealthSubject.id).order_by(HealthSubject.id).limit(2))
            )
            if subject_ids != [context.identity.subject_id]:
                raise GarminWeightExportLegacyBridgeError(
                    "fully-unowned outbox bridge requires exactly one subject"
                )
        subject = await session.scalar(
            select(HealthSubject)
            .where(HealthSubject.id == context.identity.subject_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if subject is None:
            raise GarminWeightExportOwnershipError("health subject does not exist")
        user_ids = {subject.owner_user_id}
        if context.identity.actor_user_id is not None:
            user_ids.add(context.identity.actor_user_id)
        users = {
            row.id: row
            for row in await session.scalars(
                select(User)
                .where(User.id.in_(tuple(user_ids)))
                .order_by(User.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        }
        owner = users.get(subject.owner_user_id)
        if owner is None or owner.status != UserStatus.ACTIVE.value:
            raise GarminWeightExportOwnershipError(
                "Garmin Weight export requires an active subject owner"
            )
        actor_id = context.identity.actor_user_id
        if actor_id is not None:
            actor = users.get(actor_id)
            if actor is None or actor.status != UserStatus.ACTIVE.value:
                raise GarminWeightExportOwnershipError("Garmin Weight export actor is not active")
            if actor_id != subject.owner_user_id:
                raise GarminWeightExportOwnershipError(
                    "Garmin Weight export actor must own the subject"
                )
        connection = await session.scalar(
            select(IntegrationConnection)
            .where(IntegrationConnection.id == context.integration_connection_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            connection is None
            or connection.subject_id != context.identity.subject_id
            or connection.provider != IntegrationProvider.GARMIN.value
            or connection.connection_type != IntegrationConnectionType.ACCOUNT.value
        ):
            raise GarminWeightExportOwnershipError(
                "outbox connection is not this subject's Garmin account"
            )
        allowed = {
            IntegrationConnectionStatus.LEGACY.value,
            IntegrationConnectionStatus.ACTIVE.value,
        }
        if historical:
            allowed.update(
                {
                    IntegrationConnectionStatus.DISABLED.value,
                    IntegrationConnectionStatus.RETIRED.value,
                }
            )
        if connection.status not in allowed:
            raise GarminWeightExportConnectionInactiveError(
                "Garmin account lifecycle cannot authorize this operation"
            )
    return PreparedGarminWeightExport._issue(
        session=session,
        context=context,
        historical=historical,
    )


async def resolve_legacy_export_context(
    session: AsyncSession,
    *,
    actor_username: str | None,
) -> GarminWeightExportContext:
    """Resolve the registration-off S+A+Garmin-C graph under governance."""

    from vitals.services.tenancy.ownership import resolve_legacy_ownership_context

    await acquire_identity_governance_lock(session)
    ownership = await resolve_legacy_ownership_context(
        session,
        actor_username=actor_username,
        required_connections=(IntegrationProvider.GARMIN,),
    )
    identity = ownership.system_action() if actor_username is None else ownership.owner_action()
    return GarminWeightExportContext(
        identity=identity,
        integration_connection_id=ownership.connection_id(IntegrationProvider.GARMIN),
        legacy_bridge=engine.LegacyConflictBridge.FULLY_UNOWNED,
    )


async def resolve_scoped_export_context(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
) -> GarminWeightExportContext:
    """The same graph, for a subject a trusted boundary has named.

    ``resolve_legacy_export_context`` above asks "the sole subject, or refuse",
    which is right for a direct call that named nobody and was the reason this
    job stopped entirely on a two-person installation. A scheduled run is in a
    position to say whose weight it is exporting, once per configured account.
    """

    from vitals.services.tenancy.ownership import resolve_subject_ownership_context

    await acquire_identity_governance_lock(session)
    ownership = await resolve_subject_ownership_context(
        session,
        subject_id=subject_id,
        required_connections=(IntegrationProvider.GARMIN,),
    )
    return GarminWeightExportContext(
        identity=ownership.system_action(),
        integration_connection_id=ownership.connection_id(IntegrationProvider.GARMIN),
        legacy_bridge=engine.LegacyConflictBridge.FULLY_UNOWNED,
    )


async def resolve_optional_legacy_export_context(
    session: AsyncSession,
    *,
    actor_username: str | None,
) -> GarminWeightExportContext | None:
    """Resolve the optional Garmin destination without weakening owner proof.

    Local Weight and body-scan writes must remain available when no usable
    Garmin account exists. Only provider-connection resolution failures are
    converted to ``None``; subject and actor ambiguity still fail closed.
    """

    from vitals.services.tenancy.contracts import LegacyConnectionResolutionError

    try:
        return await resolve_legacy_export_context(
            session,
            actor_username=actor_username,
        )
    except LegacyConnectionResolutionError:
        return None


@contextmanager
def _activate_scoped_export(prepared: PreparedGarminWeightExport):
    token = _SCOPED_EXPORT.set(prepared)
    try:
        yield
    finally:
        _SCOPED_EXPORT.reset(token)


def _scoped_outbox_query(
    on_date: date_type,
    *,
    context: "GarminWeightExportContext | None",
):
    """Scope the outbox lookup by destination account.

    One queued export per account per date: two Garmin accounts legitimately
    hold an intent for the same day.  A row that has not been adopted yet
    carries no connection and is still the one this context is claiming.
    Without a context there is no scope to apply, which is the legacy bridge
    the scoped-read cutover removes.
    """

    query = select(GarminWeightExport).where(GarminWeightExport.date == on_date)
    if context is None:
        return query
    return query.where(
        or_(
            GarminWeightExport.integration_connection_id == context.integration_connection_id,
            GarminWeightExport.integration_connection_id.is_(None),
        )
    )


def _active_export_context() -> GarminWeightExportContext | None:
    prepared = _SCOPED_EXPORT.get()
    return prepared.context if prepared is not None else None


async def _reprepare_active_export(
    session: AsyncSession,
    *,
    historical: bool,
) -> PreparedGarminWeightExport | None:
    """Reissue the active immutable context after an internal commit."""

    current = _SCOPED_EXPORT.get()
    if current is None:
        return None
    prepared = await prepare_scoped_export(
        session,
        context=current.context,
        historical=historical,
    )
    _SCOPED_EXPORT.set(prepared)
    return prepared


def _outbox_exact_scope(context: GarminWeightExportContext):
    return and_(
        GarminWeightExport.subject_id == context.identity.subject_id,
        GarminWeightExport.integration_connection_id == context.integration_connection_id,
    )


def _outbox_legacy_scope():
    return and_(
        GarminWeightExport.subject_id.is_(None),
        GarminWeightExport.integration_connection_id.is_(None),
        GarminWeightExport.requested_by_user_id.is_(None),
    )


async def legacy_unowned_outbox_present(session: AsyncSession) -> bool:
    """Whether any outbox row is still waiting for the ownership backfill.

    Mirror of :func:`_outbox_legacy_scope`, kept beside it. This is what the
    fully-unowned bridge is for, and it is a different question from how many
    people the installation holds: only if there is a row nobody owns does it
    matter that there is more than one person to give it to.

    ``scripts/backfill_garmin_weight_export_subject_ownership.py`` is what
    empties this set, run while the installation is still one person.
    """

    with session.no_autoflush:
        found = await session.scalar(
            select(GarminWeightExport.id).where(_outbox_legacy_scope()).limit(1)
        )
    return found is not None


def _outbox_visible_scope(context: GarminWeightExportContext):
    exact = _outbox_exact_scope(context)
    if context.legacy_bridge is engine.LegacyConflictBridge.FULLY_UNOWNED:
        return or_(exact, _outbox_legacy_scope())
    return exact


async def _validate_requested_by(
    session: AsyncSession,
    row: GarminWeightExport,
    context: GarminWeightExportContext,
) -> None:
    if row.requested_by_user_id is None:
        return
    owner_id = await session.scalar(
        select(HealthSubject.owner_user_id).where(HealthSubject.id == context.identity.subject_id)
    )
    if row.requested_by_user_id != owner_id:
        raise GarminWeightExportOwnershipError("outbox requester does not own the prepared subject")


async def _validate_linked_weight(
    session: AsyncSession,
    row: GarminWeightExport,
    context: GarminWeightExportContext,
) -> WeightLog | None:
    if row.weight_log_id is None:
        return None
    from vitals.services.weight import logs as weight_logs

    weight = await session.scalar(
        select(WeightLog)
        .where(WeightLog.id == row.weight_log_id)
        .execution_options(populate_existing=True)
    )
    if weight is None:
        # ON DELETE SET NULL is authoritative after flush. A dangling reference
        # means constraints were bypassed or this transaction has stale state.
        raise GarminWeightExportOwnershipError("outbox references a missing Weight fact")
    await weight_logs._validate_persisted_weight_provenance(
        session,
        weight,
        subject_id=context.identity.subject_id,
    )
    if weight.date != row.date:
        raise GarminWeightExportOwnershipError(
            "outbox and linked Weight fact belong to different dates"
        )
    return weight


async def _validate_scoped_outbox_row(
    session: AsyncSession,
    row: GarminWeightExport,
    context: GarminWeightExportContext,
    *,
    adopt_legacy: bool,
) -> None:
    exact = (
        row.subject_id == context.identity.subject_id
        and row.integration_connection_id == context.integration_connection_id
    )
    legacy = (
        row.subject_id is None
        and row.integration_connection_id is None
        and row.requested_by_user_id is None
    )
    if not exact:
        if not (legacy and context.legacy_bridge is engine.LegacyConflictBridge.FULLY_UNOWNED):
            raise GarminWeightExportOwnershipError("outbox has partial or foreign ownership roots")
    await _validate_requested_by(session, row, context)
    await _validate_linked_weight(session, row, context)
    if legacy and adopt_legacy:
        row.subject_id = context.identity.subject_id
        row.integration_connection_id = context.integration_connection_id
        # Unknown historical request attribution remains unknown. Never stamp
        # the actor who merely caused compatibility adoption.
        await session.flush()


async def _scoped_rows(
    session: AsyncSession,
    *,
    filters: tuple = (),
    for_update: bool = False,
) -> list[GarminWeightExport]:
    prepared = _SCOPED_EXPORT.get()
    if prepared is None:
        raise GarminWeightExportPreparedError("no scoped outbox capability is active")
    context = _require_prepared_export(session, prepared)
    await _assert_outbox_scope_integrity(session, context, filters=filters)
    stmt = select(GarminWeightExport).where(
        *filters,
        _outbox_visible_scope(context),
    )
    if for_update:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    rows = list(await session.scalars(stmt))
    for row in rows:
        await _validate_scoped_outbox_row(
            session,
            row,
            context,
            adopt_legacy=for_update,
        )
    return rows


async def _assert_outbox_scope_integrity(
    session: AsyncSession,
    context: GarminWeightExportContext,
    *,
    filters: tuple = (),
) -> None:
    invalid = await session.scalar(
        select(GarminWeightExport.id)
        .where(
            *filters,
            or_(
                GarminWeightExport.subject_id == context.identity.subject_id,
                GarminWeightExport.subject_id.is_(None),
                GarminWeightExport.integration_connection_id == context.integration_connection_id,
            ),
            _outbox_visible_scope(context).is_not(True),
        )
        .limit(1)
    )
    if invalid is not None:
        raise GarminWeightExportOwnershipError("outbox has partial or conflicting ownership roots")


async def _acquire_operation_lock(session: AsyncSession) -> None:
    """Serialize every production outbox transition for the transaction lifetime."""
    if session.get_bind().dialect.name == "postgresql":
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": _DB_OPERATION_LOCK_ID},
        )


async def lock_active_weight_change(session: AsyncSession) -> None:
    """Take the outbox lock before a local delete can touch its FK.

    PostgreSQL applies ``ON DELETE SET NULL`` while the weight row is flushed,
    which locks the related outbox row.  Taking the advisory lock first keeps the
    same lock order as the exporter and avoids an outbox-row/advisory deadlock.
    This helper is deliberately local-only and never talks to Garmin.
    """
    await _acquire_operation_lock(session)
