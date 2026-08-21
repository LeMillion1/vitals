"""Helpers for the central ``raw_payloads`` JSONB store.

Every external fetch (Hevy workout, Garmin daily metric, an activity) keeps its
full upstream response here parallel to the normalized rows it produces, so a
later schema/parse change never loses data. Both the Hevy and Garmin services
reconcile against existing rows by ``(domain, source, external_id)`` — the
natural lookup key the table is indexed for — so re-syncing refreshes one raw row
per upstream object instead of piling up duplicates.

:func:`upsert_raw_payload` resets ``processed_at`` to ``None`` whenever it
refreshes an existing row ("re-parse pending") — the promise being that
something later sweeps rows still sitting at ``processed_at IS NULL`` and
re-derives their normalized rows from the raw copy. :func:`sweep_domain` is
that sweep, generic over any domain. ``signals_service.reparse_unparsed`` is
the original, domain-specific version of the same idea (predates this one and
stays special-cased — it piggybacks on the morning brief instead of a
schedule of its own; see its docstring).
"""
from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from typing import Any, Awaitable, Callable, Optional

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import FileAssetStatus, IntegrationConnectionStatus
from vitals.models.identity import HealthSubject
from vitals.models.raw_payload import RawPayload
from vitals.models.tenancy import FileAsset, IntegrationConnection
from vitals.ownership import WriteIdentity
from vitals.utils.timeutils import now_local

logger = logging.getLogger(__name__)

# How far back a sweep looks, and how many rows one pass may cost. Same values
# as signals_service's REPARSE_WINDOW_DAYS/REPARSE_BATCH; kept as separate
# constants because that one predates this and stays domain-specific.
REPARSE_WINDOW_DAYS = 14
REPARSE_BATCH = 20


class RawPayloadServiceError(Exception):
    """Base class for fail-closed owned raw-payload failures."""


class RawPayloadValidationError(RawPayloadServiceError):
    """An ownership input does not use the strict typed contract."""


class RawPayloadReferenceError(RawPayloadServiceError):
    """A connection or file root cannot be used in this subject scope."""

    def __init__(self, field_name: str, reference_id: uuid.UUID, detail: str) -> None:
        self.field_name = field_name
        self.reference_id = reference_id
        super().__init__(f"{field_name} {detail}")


class RawPayloadReferenceNotFoundError(RawPayloadReferenceError):
    """A requested connection or file root does not exist."""


class RawPayloadReferenceOwnershipError(RawPayloadReferenceError):
    """A requested connection or file root belongs to another subject."""


class RawPayloadReferenceLifecycleError(RawPayloadReferenceError):
    """A requested connection or file root cannot authorize ingestion."""


class RawPayloadConflictError(RawPayloadServiceError):
    """A historical ownership reference conflicts with the requested write."""


class RawPayloadAmbiguityError(RawPayloadConflictError):
    """More than one row matches a scoped lookup or legacy adoption path."""


async def upsert_raw_payload(
    session: AsyncSession,
    *,
    domain: str,
    source: str,
    external_id: str,
    payload: Any,
) -> RawPayload:
    """Insert or refresh the raw payload for ``(domain, source, external_id)``.

    Flushes so the returned row has an ``id`` the normalized row can link to.
    Does not commit — the caller owns the transaction.
    """
    result = await session.execute(
        select(RawPayload).where(
            RawPayload.domain == domain,
            RawPayload.source == source,
            RawPayload.external_id == external_id,
        )
    )
    row: Optional[RawPayload] = result.scalars().first()
    if row is None:
        row = RawPayload(
            domain=domain,
            source=source,
            external_id=external_id,
            payload=payload,
            fetched_at=now_local(),
        )
        session.add(row)
    else:
        row.payload = payload
        row.fetched_at = now_local()
        row.processed_at = None  # re-parse pending
    await session.flush()
    return row


def _validate_owned_inputs(
    *,
    identity: WriteIdentity,
    integration_connection_id: uuid.UUID | None,
    file_asset_id: uuid.UUID | None,
) -> None:
    if not isinstance(identity, WriteIdentity):
        raise RawPayloadValidationError("identity must be a WriteIdentity")
    for field_name, value in (
        ("integration_connection_id", integration_connection_id),
        ("file_asset_id", file_asset_id),
    ):
        if value is not None and not isinstance(value, uuid.UUID):
            raise RawPayloadValidationError(f"{field_name} must be a UUID or None")


async def _load_connection_reference(
    session: AsyncSession,
    *,
    connection_id: uuid.UUID,
    subject_id: uuid.UUID,
) -> IntegrationConnection:
    connection = await session.scalar(
        select(IntegrationConnection)
        .where(IntegrationConnection.id == connection_id)
        .with_for_update()
    )
    if connection is None:
        raise RawPayloadReferenceNotFoundError(
            "integration_connection_id",
            connection_id,
            "does not exist",
        )
    if connection.subject_id != subject_id:
        raise RawPayloadReferenceOwnershipError(
            "integration_connection_id",
            connection_id,
            "belongs to another subject",
        )
    known_statuses = {status.value for status in IntegrationConnectionStatus}
    if connection.status not in known_statuses:
        raise RawPayloadReferenceLifecycleError(
            "integration_connection_id",
            connection_id,
            "has an unknown lifecycle state",
        )
    return connection


async def _load_file_reference(
    session: AsyncSession,
    *,
    file_asset_id: uuid.UUID,
    subject_id: uuid.UUID,
) -> FileAsset:
    asset = await session.scalar(
        select(FileAsset).where(FileAsset.id == file_asset_id).with_for_update()
    )
    if asset is None:
        raise RawPayloadReferenceNotFoundError(
            "file_asset_id",
            file_asset_id,
            "does not exist",
        )
    if asset.subject_id != subject_id:
        raise RawPayloadReferenceOwnershipError(
            "file_asset_id",
            file_asset_id,
            "belongs to another subject",
        )
    known_statuses = {status.value for status in FileAssetStatus}
    if asset.status not in known_statuses:
        raise RawPayloadReferenceLifecycleError(
            "file_asset_id",
            file_asset_id,
            "has an unknown lifecycle state",
        )
    return asset


def _require_attachable_connection(connection: IntegrationConnection) -> None:
    if connection.status == IntegrationConnectionStatus.RETIRED.value:
        raise RawPayloadReferenceLifecycleError(
            "integration_connection_id",
            connection.id,
            "is retired and cannot authorize ingestion",
        )


def _require_attachable_file(asset: FileAsset) -> None:
    if asset.status in {
        FileAssetStatus.DELETED.value,
        FileAssetStatus.PURGED.value,
    }:
        raise RawPayloadReferenceLifecycleError(
            "file_asset_id",
            asset.id,
            f"is {asset.status} and cannot authorize ingestion",
        )


async def _exact_scoped_rows(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    integration_connection_id: uuid.UUID | None,
    domain: str,
    source: str,
    external_id: str,
) -> list[RawPayload]:
    connection_scope = (
        RawPayload.integration_connection_id.is_(None)
        if integration_connection_id is None
        else RawPayload.integration_connection_id == integration_connection_id
    )
    return list(
        await session.scalars(
            select(RawPayload)
            .where(
                RawPayload.subject_id == subject_id,
                connection_scope,
                RawPayload.domain == domain,
                RawPayload.source == source,
                RawPayload.external_id == external_id,
            )
            .order_by(RawPayload.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )


async def _legacy_adoption_rows(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    integration_connection_id: uuid.UUID | None,
    domain: str,
    source: str,
    external_id: str,
) -> list[RawPayload]:
    adoption_scopes = [RawPayload.subject_id.is_(None)]
    if integration_connection_id is not None:
        adoption_scopes.append(
            and_(
                RawPayload.subject_id == subject_id,
                RawPayload.integration_connection_id.is_(None),
            )
        )
    return list(
        await session.scalars(
            select(RawPayload)
            .where(
                RawPayload.domain == domain,
                RawPayload.source == source,
                RawPayload.external_id == external_id,
                or_(*adoption_scopes),
            )
            .order_by(RawPayload.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )


async def _require_single_subject_legacy_adoption(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
) -> None:
    """Enforce the registration-disabled compatibility gate.

    This read is deliberately not a concurrency primitive. Second-subject
    creation must remain disabled until unscoped legacy adoption/backfill is
    removed; acquiring the governance lock here after raw/reference row locks
    would introduce the opposite lock order from identity bootstrap.
    """

    subject_ids = list(
        await session.scalars(
            select(HealthSubject.id)
            .order_by(HealthSubject.id)
            .limit(2)
        )
    )
    if subject_ids != [subject_id]:
        raise RawPayloadConflictError(
            "unscoped legacy raw payload cannot be adopted after multi-subject "
            "activation"
        )


async def _validate_existing_file_scope(
    session: AsyncSession,
    *,
    row: RawPayload,
    subject_id: uuid.UUID,
    requested_asset: FileAsset | None,
) -> None:
    if row.file_asset_id is None:
        return
    persisted_asset = (
        requested_asset
        if requested_asset is not None and row.file_asset_id == requested_asset.id
        else await _load_file_reference(
            session,
            file_asset_id=row.file_asset_id,
            subject_id=subject_id,
        )
    )
    _require_attachable_file(persisted_asset)


def _validate_file_compatibility(
    row: RawPayload,
    *,
    requested_file_asset_id: uuid.UUID | None,
) -> bool:
    """Return whether a validated requested file root must be attached."""

    if (
        row.file_asset_id is not None
        and requested_file_asset_id is not None
        and row.file_asset_id != requested_file_asset_id
    ):
        raise RawPayloadConflictError(
            "raw payload already references a different file_asset_id"
        )
    return row.file_asset_id is None and requested_file_asset_id is not None


def _refresh_owned_row(row: RawPayload, payload: Any) -> None:
    row.payload = payload
    row.fetched_at = now_local()
    row.processed_at = None


async def upsert_owned_raw_payload(
    session: AsyncSession,
    *,
    identity: WriteIdentity,
    integration_connection_id: uuid.UUID | None = None,
    file_asset_id: uuid.UUID | None = None,
    domain: str,
    source: str,
    external_id: str,
    payload: Any,
    validate_locked_existing: Callable[[RawPayload], None] | None = None,
) -> RawPayload:
    """Insert, refresh, or safely adopt one subject-scoped raw payload.

    The authoritative lookup key is ``(subject, exact connection/null, domain,
    source, external_id)``. A missing scoped row may adopt one unscoped legacy
    row, or one same-subject/connection-null row when adding a connection. The
    operation never rewrites a historical actor or a non-null connection/file
    reference. Fully scoped rows belonging to other subjects or connections are
    isolated and do not participate in adoption.

    A domain with stricter compatibility rules may pass
    ``validate_locked_existing``. It runs after the matching row is locked but
    before any root, payload, or processing timestamp changes, so rejection is
    fail-closed instead of mutate-then-validate.

    Every selected root and candidate is locked and the function flushes but
    never commits. There is intentionally no concurrent absent-row guarantee
    until the scoped unique constraint is introduced at contract cutover: two
    transactions that both observe no row can still insert duplicates.
    Retired connections and deleted or purged file roots reject every call,
    including refreshes of rows that already reference the closed root.
    Unscoped adoption is a pre-registration compatibility path: concurrent
    second-subject creation remains forbidden until that path and its backfill
    are retired. The subject-count check alone is not phantom-safe.
    """

    _validate_owned_inputs(
        identity=identity,
        integration_connection_id=integration_connection_id,
        file_asset_id=file_asset_id,
    )
    if validate_locked_existing is not None and not callable(
        validate_locked_existing
    ):
        raise RawPayloadValidationError(
            "validate_locked_existing must be callable or None"
        )

    requested_connection = (
        await _load_connection_reference(
            session,
            connection_id=integration_connection_id,
            subject_id=identity.subject_id,
        )
        if integration_connection_id is not None
        else None
    )
    requested_asset = (
        await _load_file_reference(
            session,
            file_asset_id=file_asset_id,
            subject_id=identity.subject_id,
        )
        if file_asset_id is not None
        else None
    )
    if requested_connection is not None:
        _require_attachable_connection(requested_connection)
    if requested_asset is not None:
        _require_attachable_file(requested_asset)

    exact_rows = await _exact_scoped_rows(
        session,
        subject_id=identity.subject_id,
        integration_connection_id=integration_connection_id,
        domain=domain,
        source=source,
        external_id=external_id,
    )
    if len(exact_rows) > 1:
        raise RawPayloadAmbiguityError(
            "multiple raw payloads match the exact subject/connection scope"
        )

    if exact_rows:
        row = exact_rows[0]
        if validate_locked_existing is not None:
            validate_locked_existing(row)
        await _validate_existing_file_scope(
            session,
            row=row,
            subject_id=identity.subject_id,
            requested_asset=requested_asset,
        )
        attach_file = _validate_file_compatibility(
            row,
            requested_file_asset_id=file_asset_id,
        )
        if attach_file:
            row.file_asset_id = file_asset_id
        _refresh_owned_row(row, payload)
        await session.flush()
        return row

    adoption_rows = await _legacy_adoption_rows(
        session,
        subject_id=identity.subject_id,
        integration_connection_id=integration_connection_id,
        domain=domain,
        source=source,
        external_id=external_id,
    )
    if len(adoption_rows) > 1:
        raise RawPayloadAmbiguityError(
            "multiple raw payloads are eligible for legacy ownership adoption"
        )

    if adoption_rows:
        row = adoption_rows[0]
        if validate_locked_existing is not None:
            validate_locked_existing(row)
        if row.subject_id not in {None, identity.subject_id}:
            raise RawPayloadConflictError(
                "legacy raw payload belongs to another subject"
            )
        if row.integration_connection_id is not None:
            if row.integration_connection_id != integration_connection_id:
                raise RawPayloadConflictError(
                    "legacy raw payload references a different "
                    "integration_connection_id"
                )
        if row.subject_id is None:
            await _require_single_subject_legacy_adoption(
                session,
                subject_id=identity.subject_id,
            )
        attach_connection = (
            row.integration_connection_id is None
            and integration_connection_id is not None
        )

        await _validate_existing_file_scope(
            session,
            row=row,
            subject_id=identity.subject_id,
            requested_asset=requested_asset,
        )
        attach_file = _validate_file_compatibility(
            row,
            requested_file_asset_id=file_asset_id,
        )

        if row.subject_id is None:
            row.subject_id = identity.subject_id
        if attach_connection:
            row.integration_connection_id = integration_connection_id
        if attach_file:
            row.file_asset_id = file_asset_id
        _refresh_owned_row(row, payload)
        await session.flush()
        return row

    row = RawPayload(
        subject_id=identity.subject_id,
        actor_user_id=identity.actor_user_id,
        integration_connection_id=integration_connection_id,
        file_asset_id=file_asset_id,
        domain=domain,
        source=source,
        external_id=external_id,
        payload=payload,
        fetched_at=now_local(),
    )
    session.add(row)
    await session.flush()
    return row


async def sweep_domain(
    session: AsyncSession,
    *,
    domain: str,
    reparse: Callable[[AsyncSession, RawPayload], Awaitable[Any]],
    has_normalized: Any,
    limit: int = REPARSE_BATCH,
    since_days: int = REPARSE_WINDOW_DAYS,
) -> int:
    """Generic re-parse sweep, modeled on ``signals_service.reparse_unparsed`` —
    extended here so every domain can reuse the same query instead of each
    re-implementing it.

    Picks up to ``limit`` rows for ``domain`` still at ``processed_at IS NULL``
    (what :func:`upsert_raw_payload` leaves behind whenever it refreshes a row)
    within ``since_days`` of ``fetched_at``, excluding rows that already have a
    normalized child. ``has_normalized`` is a caller-built ``EXISTS`` clause
    correlated to ``RawPayload.id`` (e.g. ``select(Model.id).where(Model.
    raw_payload_id == RawPayload.id).exists()``) — passed in rather than
    hard-coded so this function stays domain-agnostic; it never imports a
    domain's own models. ``reparse`` does the actual re-derivation, reusing
    that domain's existing ingest logic.

    A raising ``reparse`` call is logged and skipped so one bad row can't abort
    the batch; ``processed_at`` is stamped only after a row's ``reparse`` call
    returns without raising. Flushes once at the end of the batch, not per row.
    Does not commit — the caller owns the transaction.

    ponytail: a reparse that raises *after* partially writing (e.g. midway
    through a multi-step ingest) leaves ``processed_at IS NULL`` but may
    already have a partial child row, which would make ``has_normalized`` skip
    it on every later sweep too — stuck rather than retried. Narrow edge case
    (most failures here are validation, which happens before any write); add a
    per-row SAVEPOINT (``session.begin_nested()``) around the ``reparse`` call
    if this shows up for real.
    """
    cutoff = now_local() - timedelta(days=since_days)
    stmt = (
        select(RawPayload)
        .where(
            RawPayload.domain == domain,
            RawPayload.processed_at.is_(None),
            RawPayload.fetched_at >= cutoff,
            ~has_normalized,
        )
        .order_by(RawPayload.id)
        .limit(limit)
    )
    done = 0
    for raw in (await session.execute(stmt)).scalars().all():
        try:
            await reparse(session, raw)
        except Exception:
            logger.warning("re-parse failed for %s raw payload %s", domain, raw.id, exc_info=True)
            continue
        raw.processed_at = now_local()
        done += 1
    if done:
        await session.flush()
    return done


async def sweep_pending_job(session_factory, redis=None) -> None:
    """Nightly sweep for garmin/hevy/labs/body_comp/genetics raw payloads pending a
    normalized row (registered in vitals/scheduler/jobs.py).

    Signals' own reparse instead piggybacks on the morning brief (see
    proactive/inbound.py, called from brief.py) because it has to finish before
    that message goes out. These five domains have no such deadline — they're
    fed by a periodic poll (garmin/hevy) or an owner import/upload
    (labs/body_comp/genetics), not a message that's about to be sent — so one
    shared nightly pass covers all of them instead of separate jobs. Each domain
    commits (and rolls back on failure) on its own so one domain's trouble can't
    lose or block another's completed work.
    """
    from vitals.enums import IntegrationProvider
    from vitals.services import (
        body_scan_service,
        conflict_engine,
        garmin_service,
        genetics_service,
        hevy_service,
        labs_service,
    )
    from vitals.services.legacy_ownership import resolve_legacy_ownership_context
    from vitals.utils.timeutils import today_local

    async with session_factory() as session:
        async def _sweep_owned_garmin() -> int:
            ownership = await resolve_legacy_ownership_context(
                session,
                actor_username=None,
                required_connections=(IntegrationProvider.GARMIN,),
            )
            return await garmin_service.reparse_owned_pending(
                session,
                identity=ownership.system_action(),
                integration_connection_id=ownership.connection_id(
                    IntegrationProvider.GARMIN
                ),
            )

        async def _sweep_owned_hevy() -> int:
            ownership = await resolve_legacy_ownership_context(
                session,
                actor_username=None,
                required_connections=(IntegrationProvider.HEVY,),
            )
            return await hevy_service.reparse_owned_pending(
                session,
                identity=ownership.system_action(),
                integration_connection_id=ownership.connection_id(
                    IntegrationProvider.HEVY
                ),
            )

        async def _sweep_owned_labs() -> int:
            context = await conflict_engine.resolve_legacy_conflict_write_context(
                session,
                actor_username=None,
                evaluation_date=today_local(),
            )
            prepared = await conflict_engine.prepare_scoped_write(
                session,
                context=context,
            )
            return await labs_service.reparse_owned_pending(
                session,
                identity=context.identity,
                prepared_conflict_write=prepared,
                include_legacy_unowned=True,
            )

        async def _sweep_owned_body_comp() -> int:
            context = await conflict_engine.resolve_legacy_conflict_write_context(
                session,
                actor_username=None,
                evaluation_date=today_local(),
            )
            return await body_scan_service.reparse_owned_pending(
                session,
                identity=context.identity,
                include_legacy_unowned=True,
            )

        async def _sweep_owned_genetics() -> int:
            context = await conflict_engine.resolve_legacy_conflict_write_context(
                session,
                actor_username=None,
                evaluation_date=today_local(),
            )
            prepared = await conflict_engine.prepare_scoped_write(
                session,
                context=context,
            )
            return await genetics_service.reparse_owned_pending(
                session,
                identity=context.identity,
                prepared_conflict_write=prepared,
            )

        for name, sweep in (
            ("garmin", _sweep_owned_garmin),
            ("hevy", _sweep_owned_hevy),
            ("labs", _sweep_owned_labs),
            ("body_comp", _sweep_owned_body_comp),
            ("genetics", _sweep_owned_genetics),
        ):
            try:
                await sweep()
                await session.commit()
            except Exception:
                logger.warning("raw payload sweep failed for domain %s", name, exc_info=True)
                await session.rollback()
