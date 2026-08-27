"""Fail-closed public-token attestation and RLS subject binding."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from vitals.enums import UserStatus
from vitals.models.identity import HealthSubject, User
from vitals.models.share import SharedReport
from vitals.ownership_transition import bridges as ownership_bridges
from vitals.persistence.rls import (
    RlsSessionError,
    bind_session_subject,
    bound_subject,
    in_platform_scope,
)
from vitals.services.identity.governance import acquire_identity_governance_lock
from vitals.services.share.ownership import (
    POSTGRES_PUBLIC_AUTHORIZATION_ROUTINE,
    _PublicReportOwnershipError,
)
from vitals.services.share.snapshot import (
    _historical_bridge_state,
    _validate_report_roots,
)
from vitals.utils.timeutils import now_local

logger = logging.getLogger(__name__)

async def _public_subject_owner(
    session: AsyncSession,
    *,
    report_id: int,
    subject_id: uuid.UUID | None,
    created_by_user_id: uuid.UUID | None,
    revoked_by_user_id: uuid.UUID | None,
    revoked_at: datetime | None,
    for_update: bool,
) -> tuple[uuid.UUID, uuid.UUID, Any]:
    """Validate roots selected by an opaque public token, never infer actors."""
    if revoked_by_user_id is not None and revoked_at is None:
        raise _PublicReportOwnershipError(
            "public report has revocation actor without revocation timestamp"
        )
    if subject_id is None:
        if created_by_user_id is not None or revoked_by_user_id is not None:
            raise _PublicReportOwnershipError(
                "public report has partial legacy ownership roots"
            )
        from vitals.services.tenancy.contracts import LegacyOwnershipError
        from vitals.services.tenancy.ownership import resolve_legacy_ownership_context

        try:
            ownership = await resolve_legacy_ownership_context(
                session,
                actor_username=None,
            )
        except LegacyOwnershipError as exc:
            raise _PublicReportOwnershipError(
                "public legacy report requires exactly one active owner"
            ) from exc
        resolved_subject_id = ownership.subject_id
        owner_user_id = ownership.owner_user_id
    else:
        resolved_subject_id = subject_id
        subject_stmt = select(HealthSubject).where(
            HealthSubject.id == resolved_subject_id
        )
        if for_update:
            subject_stmt = subject_stmt.with_for_update().execution_options(
                populate_existing=True
            )
        else:
            subject_stmt = subject_stmt.execution_options(populate_existing=True)
        subject = await session.scalar(subject_stmt)
        if subject is None:
            raise _PublicReportOwnershipError("public report subject is missing")
        owner_user_id = subject.owner_user_id

    if subject_id is None and for_update:
        subject = await session.scalar(
            select(HealthSubject)
            .where(HealthSubject.id == resolved_subject_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if subject is None or subject.owner_user_id != owner_user_id:
            raise _PublicReportOwnershipError(
                "public report subject owner changed during validation"
            )

    owner_stmt = select(User).where(User.id == owner_user_id)
    if for_update:
        owner_stmt = owner_stmt.with_for_update().execution_options(
            populate_existing=True
        )
    else:
        owner_stmt = owner_stmt.execution_options(populate_existing=True)
    owner = await session.scalar(owner_stmt)
    if owner is None or owner.status != UserStatus.ACTIVE.value:
        raise _PublicReportOwnershipError(
            "public report owner is missing or inactive"
        )
    bridge_state = await _historical_bridge_state(
        session,
        subject_id=resolved_subject_id,
        public=True,
    )
    _validate_report_roots(
        report_id=report_id,
        expected_subject_id=resolved_subject_id,
        owner_user_id=owner_user_id,
        subject_id=subject_id,
        created_by_user_id=created_by_user_id,
        revoked_by_user_id=revoked_by_user_id,
        revoked_at=revoked_at,
        bridge_state=bridge_state,
        error_type=_PublicReportOwnershipError,
    )
    return resolved_subject_id, owner_user_id, bridge_state


def _report_is_publicly_live(row: SharedReport) -> bool:
    return bool(
        row.revoked_at is None
        and row.snapshot is not None
        and row.expires_at > now_local()
    )


async def _authorize_and_bind_public_report(
    session: AsyncSession,
    token: str,
) -> tuple[int, uuid.UUID] | None:
    """Turn one exact public bearer into one ordinary subject binding.

    PostgreSQL must cross the initial forced-RLS lookup through the reviewed
    migration-owned routine.  Historical pre-ownership tests may run against a
    PostgreSQL schema with neither RLS nor that later routine; direct lookup is
    allowed only after the catalog proves row security is disabled.  SQLite has
    no RLS and follows the same compatibility path.
    """

    if in_platform_scope(session):
        return None
    await acquire_identity_governance_lock(session)

    dialect_name = session.get_bind().dialect.name
    if dialect_name == "postgresql":
        routine_exists = bool(
            await session.scalar(
                text("SELECT to_regprocedure(:signature) IS NOT NULL"),
                {"signature": POSTGRES_PUBLIC_AUTHORIZATION_ROUTINE},
            )
        )
        if routine_exists:
            attestation = (
                await session.execute(
                    text(
                        "SELECT * FROM "
                        "public.attest_shared_report_token(:token)"
                    ),
                    {"token": token},
                )
            ).mappings().one_or_none()
            if attestation is None:
                return None
            try:
                report_id, subject_id = _validate_public_attestation(attestation)
            except _PublicReportOwnershipError:
                _refresh_cached_public_attestation(
                    session,
                    token=token,
                    attestation=attestation,
                )
                return None
            return await _bind_public_subject(
                session,
                report_id=report_id,
                subject_id=subject_id,
            )
        else:
            row_security_enabled = bool(
                await session.scalar(
                    text(
                        "SELECT relrowsecurity FROM pg_class "
                        "WHERE oid=to_regclass('public.shared_reports')"
                    )
                )
            )
            if row_security_enabled:
                return None

    # SQLite and a historical PostgreSQL schema without row security use this
    # projection-only compatibility path.  Validate every capability property
    # before binding; never materialize the report snapshot while unbound.
    roots = (
        await session.execute(
            select(
                SharedReport.id,
                SharedReport.subject_id,
                SharedReport.created_by_user_id,
                SharedReport.revoked_by_user_id,
                SharedReport.revoked_at,
                SharedReport.expires_at,
                SharedReport.snapshot.is_not(None).label("has_snapshot"),
            ).where(SharedReport.token == token)
        )
    ).one_or_none()
    if roots is None:
        return None
    (
        report_id,
        subject_id,
        created_by_user_id,
        revoked_by_user_id,
        revoked_at,
        expires_at,
        has_snapshot,
    ) = roots
    try:
        subject_id, _owner_user_id, _bridge = await _public_subject_owner(
            session,
            report_id=report_id,
            subject_id=subject_id,
            created_by_user_id=created_by_user_id,
            revoked_by_user_id=revoked_by_user_id,
            revoked_at=revoked_at,
            for_update=True,
        )
    except _PublicReportOwnershipError:
        return None
    if (
        revoked_at is not None
        or has_snapshot is not True
        or not isinstance(expires_at, datetime)
        or expires_at <= now_local()
    ):
        return None
    return await _bind_public_subject(
        session,
        report_id=report_id,
        subject_id=subject_id,
    )


def _validate_public_attestation(attestation: Any) -> tuple[int, uuid.UUID]:
    """Prove a public bearer is safe before granting its subject capability."""

    report_id = attestation["report_id"]
    subject_id = attestation["subject_id"]
    created_by_user_id = attestation["created_by_user_id"]
    revoked_by_user_id = attestation["revoked_by_user_id"]
    revoked_at = attestation["revoked_at"]
    expires_at = attestation["expires_at"]
    has_snapshot = attestation["has_snapshot"]
    owner_user_id = attestation["owner_user_id"]
    owner_status = attestation["owner_status"]
    if (
        not isinstance(report_id, int)
        or isinstance(report_id, bool)
        or report_id <= 0
        or not isinstance(subject_id, uuid.UUID)
        or subject_id.int == 0
        or not isinstance(owner_user_id, uuid.UUID)
        or owner_user_id.int == 0
        or owner_status != UserStatus.ACTIVE.value
        or (
            created_by_user_id is not None
            and not isinstance(created_by_user_id, uuid.UUID)
        )
        or (
            revoked_by_user_id is not None
            and not isinstance(revoked_by_user_id, uuid.UUID)
        )
        or (revoked_at is not None and not isinstance(revoked_at, datetime))
        or not isinstance(expires_at, datetime)
    ):
        raise _PublicReportOwnershipError("public report attestation is malformed")
    checkpoint = ownership_bridges.SharedReportCheckpointAttestation(
        phase_key=attestation["checkpoint_phase_key"],
        subject_id=attestation["checkpoint_subject_id"],
        status=attestation["checkpoint_status"],
        scan_high_watermark_id=attestation[
            "checkpoint_scan_high_watermark_id"
        ],
        snapshot_rows=attestation["checkpoint_snapshot_rows"],
        last_scanned_id=attestation["checkpoint_last_scanned_id"],
        scanned_rows=attestation["checkpoint_scanned_rows"],
        updated_rows=attestation["checkpoint_updated_rows"],
        unchanged_rows=attestation["checkpoint_unchanged_rows"],
        data_checksum_before=attestation["checkpoint_data_checksum_before"],
        data_checksum_after=attestation["checkpoint_data_checksum_after"],
        ownership_checksum_after=attestation[
            "checkpoint_ownership_checksum_after"
        ],
        started_at=attestation["checkpoint_started_at"],
        updated_at=attestation["checkpoint_updated_at"],
        completed_at=attestation["checkpoint_completed_at"],
    )
    try:
        bridge_state = (
            ownership_bridges.shared_report_historical_bridge_state_from_attestation(
                checkpoint,
                subject_id=subject_id,
            )
        )
    except ownership_bridges.SharedReportOwnershipBackfillError as exc:
        raise _PublicReportOwnershipError(
            "public report checkpoint attestation is not authoritative"
        ) from exc
    _validate_report_roots(
        report_id=report_id,
        expected_subject_id=subject_id,
        owner_user_id=owner_user_id,
        subject_id=subject_id,
        created_by_user_id=created_by_user_id,
        revoked_by_user_id=revoked_by_user_id,
        revoked_at=revoked_at,
        bridge_state=bridge_state,
        error_type=_PublicReportOwnershipError,
    )
    if revoked_at is not None or has_snapshot is not True or expires_at <= now_local():
        raise _PublicReportOwnershipError("public report is not live")
    return report_id, subject_id


def _refresh_cached_public_attestation(
    session: AsyncSession,
    *,
    token: str,
    attestation: Any,
) -> None:
    """Apply safe invalidation facts to an already-loaded report instance."""

    report_id = attestation.get("report_id")
    if not isinstance(report_id, int) or isinstance(report_id, bool):
        return
    cached = next(
        (
            instance
            for instance in session.sync_session.identity_map.values()
            if isinstance(instance, SharedReport)
            and instance.id == report_id
            and instance.token == token
        ),
        None,
    )
    if cached is None:
        return
    for attribute in ("revoked_by_user_id", "revoked_at", "expires_at"):
        set_committed_value(cached, attribute, attestation.get(attribute))
    if attestation.get("has_snapshot") is False:
        set_committed_value(cached, "snapshot", None)


async def _bind_public_subject(
    session: AsyncSession,
    *,
    report_id: int,
    subject_id: uuid.UUID,
) -> tuple[int, uuid.UUID] | None:
    """Bind one already-attested subject without permitting scope switching."""

    current_subject = bound_subject(session)
    if current_subject is not None and current_subject != subject_id:
        return None
    try:
        await bind_session_subject(session, subject_id)
    except RlsSessionError:
        return None
    return report_id, subject_id


async def resolve_public(session: AsyncSession, token: str) -> Optional[SharedReport]:
    """The row behind a public token, or ``None``.

    One ``None`` for all four ways a link can fail — unknown, revoked, expired,
    purged — because the visitor must not be able to tell them apart, and a page
    that says "this was revoked" tells them.
    """
    if not token:
        return None
    authorized = await _authorize_and_bind_public_report(session, token)
    if authorized is None:
        _discard_cached_public_snapshot(session, token=token)
        return None
    report_id, authorized_subject_id = authorized
    row = await session.scalar(
        select(SharedReport)
        .where(SharedReport.id == report_id, SharedReport.token == token)
        .execution_options(populate_existing=True)
    )
    if row is None:
        return None
    try:
        resolved_subject_id, _owner_user_id, _bridge = await _public_subject_owner(
            session,
            report_id=row.id,
            subject_id=row.subject_id,
            created_by_user_id=row.created_by_user_id,
            revoked_by_user_id=row.revoked_by_user_id,
            revoked_at=row.revoked_at,
            for_update=False,
        )
    except _PublicReportOwnershipError:
        logger.warning(
            "shared report %s has invalid public ownership roots",
            row.id,
        )
        return None
    if resolved_subject_id != authorized_subject_id:
        return None
    return row if _report_is_publicly_live(row) else None


async def register_open(
    session: AsyncSession,
    token: str,
) -> Optional[SharedReport]:
    """Lock and count one still-live token after password verification."""
    if not token:
        return None
    authorized = await _authorize_and_bind_public_report(session, token)
    if authorized is None:
        _discard_cached_public_snapshot(session, token=token)
        return None
    report_id, authorized_subject_id = authorized
    roots = (
        await session.execute(
            select(
                SharedReport.subject_id,
                SharedReport.created_by_user_id,
                SharedReport.revoked_by_user_id,
                SharedReport.revoked_at,
            ).where(SharedReport.id == report_id, SharedReport.token == token)
        )
    ).one_or_none()
    if roots is None:
        return None
    subject_id, created_by_user_id, revoked_by_user_id, revoked_at = roots
    try:
        resolved_subject_id, _owner_user_id, _bridge = await _public_subject_owner(
            session,
            report_id=report_id,
            subject_id=subject_id,
            created_by_user_id=created_by_user_id,
            revoked_by_user_id=revoked_by_user_id,
            revoked_at=revoked_at,
            for_update=True,
        )
    except _PublicReportOwnershipError:
        logger.warning(
            "shared report %s has invalid open ownership roots",
            report_id,
        )
        return None
    if resolved_subject_id != authorized_subject_id:
        return None
    row = await session.scalar(
        select(SharedReport)
        .where(SharedReport.id == report_id, SharedReport.token == token)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None or (
        row.subject_id != subject_id
        or row.created_by_user_id != created_by_user_id
        or row.revoked_by_user_id != revoked_by_user_id
        or row.revoked_at != revoked_at
    ):
        return None
    if not _report_is_publicly_live(row):
        return None
    row.opened_count = (row.opened_count or 0) + 1
    row.last_opened_at = now_local()
    await session.flush()
    return row


def _discard_cached_public_snapshot(session: AsyncSession, *, token: str) -> None:
    """Fail closed for a report instance retained across public transactions."""

    for instance in session.sync_session.identity_map.values():
        if isinstance(instance, SharedReport) and instance.token == token:
            set_committed_value(instance, "snapshot", None)
