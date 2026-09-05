"""Ownership-scoped reads and canonical lock acquisition for preferences."""
from __future__ import annotations

import uuid

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from vitals.enums import IntegrationConnectionType, IntegrationProvider, UserStatus
from vitals.models.identity import HealthSubject, User
from vitals.models.scoped_settings import IntegrationConnectionSetting, SubjectSetting
from vitals.models.tenancy import IntegrationConnection
from vitals.services.identity.contracts import IdentityValidationError
from vitals.services.identity.governance import acquire_identity_governance_lock
from vitals.services.identity.normalization import normalize_username
from vitals.services.proactive.preferences.codec import (
    _decode_bundle,
    _decode_delivery,
    _decode_garmin,
    _decode_subject,
    _stored_or_default,
)
from vitals.services.proactive.preferences.contracts import (
    GARMIN_POLICY_KEY,
    SUBJECT_POLICY_KEY,
    TELEGRAM_DELIVERY_POLICY_KEY,
    LegacyProactivePreferencesBridgeClosedError,
    ProactivePreferencesBundle,
    ProactivePreferencesNotConfiguredError,
    ProactivePreferencesScope,
    ProactivePreferencesScopeError,
    ProactivePreferencesUnavailableError,
    ProactivePreferencesValidationError,
    SubjectProactivePolicy,
    GarminProactivePolicy,
    DeliveryPolicy,
    _LIVE_CONNECTION_STATUSES,
    _NON_RETIRED_CONNECTION_STATUSES,
)

def _required_actor_lookup_key(actor_username: str) -> str:
    try:
        return normalize_username(actor_username).lookup_key
    except IdentityValidationError as exc:
        raise ProactivePreferencesValidationError(str(exc)) from exc


async def resolve_legacy_preferences_scope(
    session: AsyncSession,
    *,
    actor_username: str | None,
) -> ProactivePreferencesScope:
    """Resolve the exact-one subject and both connection partitions."""

    from vitals.services.tenancy.ownership import resolve_legacy_ownership_context

    ownership = await resolve_legacy_ownership_context(
        session,
        actor_username=actor_username,
        required_connections=(
            IntegrationProvider.TELEGRAM,
            IntegrationProvider.GARMIN,
        ),
    )
    return ProactivePreferencesScope(
        subject_id=ownership.subject_id,
        recipient_user_id=ownership.owner_user_id,
        telegram_connection_id=ownership.connection_id(
            IntegrationProvider.TELEGRAM
        ),
        garmin_connection_id=ownership.connection_id(IntegrationProvider.GARMIN),
        include_legacy=True,
    )


async def _validate_scope_roots(
    session: AsyncSession,
    scope: ProactivePreferencesScope,
    *,
    for_update: bool,
    actor_lookup_key: str | None = None,
    require_live_telegram: bool = False,
) -> None:
    if not isinstance(scope, ProactivePreferencesScope):
        raise ProactivePreferencesValidationError(
            "scope must be a ProactivePreferencesScope"
        )

    subject_query = select(HealthSubject.owner_user_id).where(
        HealthSubject.id == scope.subject_id
    )
    if for_update:
        subject_query = subject_query.with_for_update()
    owner_user_id = await session.scalar(subject_query)
    if owner_user_id != scope.recipient_user_id:
        raise ProactivePreferencesScopeError(
            "proactive preference recipient is not the subject owner"
        )

    owner_query = select(User.status, User.normalized_username).where(
        User.id == scope.recipient_user_id
    )
    if for_update:
        owner_query = owner_query.with_for_update()
    owner_row = (await session.execute(owner_query)).one_or_none()
    if owner_row is None or owner_row.status != UserStatus.ACTIVE.value:
        raise ProactivePreferencesScopeError(
            "proactive preference recipient is not active"
        )
    if (
        actor_lookup_key is not None
        and owner_row.normalized_username != actor_lookup_key
    ):
        raise ProactivePreferencesScopeError(
            "proactive preference actor is not the subject owner"
        )

    connection_query = (
        select(IntegrationConnection)
        .where(
            IntegrationConnection.id.in_(
                (scope.telegram_connection_id, scope.garmin_connection_id)
            )
        )
        .order_by(IntegrationConnection.id)
    )
    if for_update:
        connection_query = connection_query.with_for_update().execution_options(
            populate_existing=True
        )
    connections = {
        row.id: row for row in await session.scalars(connection_query)
    }
    if set(connections) != {
        scope.telegram_connection_id,
        scope.garmin_connection_id,
    }:
        raise ProactivePreferencesScopeError(
            "proactive preference connection roots are missing"
        )

    telegram = connections[scope.telegram_connection_id]
    garmin = connections[scope.garmin_connection_id]
    if (
        telegram.subject_id != scope.subject_id
        or telegram.provider != IntegrationProvider.TELEGRAM.value
        or telegram.connection_type
        != IntegrationConnectionType.RECIPIENT.value
        or telegram.status
        not in (
            _LIVE_CONNECTION_STATUSES
            if require_live_telegram
            else _NON_RETIRED_CONNECTION_STATUSES
        )
    ):
        raise ProactivePreferencesScopeError(
            "Telegram preference connection does not match the subject"
        )
    if (
        garmin.subject_id != scope.subject_id
        or garmin.provider != IntegrationProvider.GARMIN.value
        or garmin.connection_type != IntegrationConnectionType.ACCOUNT.value
        or garmin.status not in _NON_RETIRED_CONNECTION_STATUSES
    ):
        raise ProactivePreferencesScopeError(
            "Garmin preference connection does not match the subject"
        )


async def _setting_rows(
    session: AsyncSession,
    scope: ProactivePreferencesScope,
    *,
    for_update: bool,
) -> tuple[
    SubjectSetting | None,
    IntegrationConnectionSetting | None,
    IntegrationConnectionSetting | None,
]:
    subject_query = select(SubjectSetting).where(
        SubjectSetting.subject_id == scope.subject_id,
        SubjectSetting.key == SUBJECT_POLICY_KEY,
    )
    delivery_query = select(IntegrationConnectionSetting).where(
        IntegrationConnectionSetting.integration_connection_id
        == scope.telegram_connection_id,
        IntegrationConnectionSetting.key == TELEGRAM_DELIVERY_POLICY_KEY,
    )
    garmin_query = select(IntegrationConnectionSetting).where(
        IntegrationConnectionSetting.integration_connection_id
        == scope.garmin_connection_id,
        IntegrationConnectionSetting.key == GARMIN_POLICY_KEY,
    )
    if for_update:
        subject_query = subject_query.with_for_update().execution_options(
            populate_existing=True
        )
        delivery_query = delivery_query.with_for_update().execution_options(
            populate_existing=True
        )
        garmin_query = garmin_query.with_for_update().execution_options(
            populate_existing=True
        )
    return (
        await session.scalar(subject_query),
        await session.scalar(delivery_query),
        await session.scalar(garmin_query),
    )


def _require_complete_rows(
    rows: tuple[
        SubjectSetting | None,
        IntegrationConnectionSetting | None,
        IntegrationConnectionSetting | None,
    ],
) -> tuple[
    SubjectSetting,
    IntegrationConnectionSetting,
    IntegrationConnectionSetting,
]:
    if any(row is None for row in rows):
        raise ProactivePreferencesUnavailableError(
            "scoped proactive preferences are missing or partial"
        )
    subject, delivery, garmin = rows
    assert subject is not None and delivery is not None and garmin is not None
    return subject, delivery, garmin


async def get_preferences_bundle(
    session: AsyncSession,
    *,
    scope: ProactivePreferencesScope,
    actor_username: str,
) -> ProactivePreferencesBundle:
    """Load one actor-authorized, statement-consistent scoped snapshot.

    One joined statement is intentional: under PostgreSQL ``READ COMMITTED`` it
    observes the roots and all three policy partitions from one MVCC snapshot.
    Splitting this read into independent selects could combine values from two
    concurrent settings saves. Legacy/default state is never consulted.
    """

    if not isinstance(scope, ProactivePreferencesScope):
        raise ProactivePreferencesValidationError(
            "scope must be a ProactivePreferencesScope"
        )
    actor_lookup_key = _required_actor_lookup_key(actor_username)
    telegram_connection = aliased(IntegrationConnection)
    garmin_connection = aliased(IntegrationConnection)
    delivery_setting = aliased(IntegrationConnectionSetting)
    garmin_setting = aliased(IntegrationConnectionSetting)
    statement = (
        select(
            SubjectSetting.value,
            delivery_setting.value,
            garmin_setting.value,
        )
        .select_from(HealthSubject)
        .join(User, User.id == HealthSubject.owner_user_id)
        .outerjoin(
            SubjectSetting,
            and_(
                SubjectSetting.subject_id == HealthSubject.id,
                SubjectSetting.key == SUBJECT_POLICY_KEY,
            ),
        )
        .join(
            telegram_connection,
            telegram_connection.id == scope.telegram_connection_id,
        )
        .outerjoin(
            delivery_setting,
            and_(
                delivery_setting.integration_connection_id
                == telegram_connection.id,
                delivery_setting.key == TELEGRAM_DELIVERY_POLICY_KEY,
            ),
        )
        .join(
            garmin_connection,
            garmin_connection.id == scope.garmin_connection_id,
        )
        .outerjoin(
            garmin_setting,
            and_(
                garmin_setting.integration_connection_id
                == garmin_connection.id,
                garmin_setting.key == GARMIN_POLICY_KEY,
            ),
        )
        .where(
            HealthSubject.id == scope.subject_id,
            HealthSubject.owner_user_id == scope.recipient_user_id,
            User.id == scope.recipient_user_id,
            User.normalized_username == actor_lookup_key,
            User.status == UserStatus.ACTIVE.value,
            telegram_connection.subject_id == scope.subject_id,
            telegram_connection.provider == IntegrationProvider.TELEGRAM.value,
            telegram_connection.connection_type
            == IntegrationConnectionType.RECIPIENT.value,
            telegram_connection.status.in_(_NON_RETIRED_CONNECTION_STATUSES),
            garmin_connection.subject_id == scope.subject_id,
            garmin_connection.provider == IntegrationProvider.GARMIN.value,
            garmin_connection.connection_type
            == IntegrationConnectionType.ACCOUNT.value,
            garmin_connection.status.in_(_NON_RETIRED_CONNECTION_STATUSES),
        )
    )
    with session.no_autoflush:
        row = (await session.execute(statement)).one_or_none()
    if row is None:
        raise ProactivePreferencesScopeError(
            "proactive preference actor or resource graph is out of scope"
        )
    return _decode_bundle(*_stored_or_default(row[0], row[1], row[2]))


async def get_exact_one_preferences_bundle(
    session: AsyncSession,
    *,
    scope: ProactivePreferencesScope,
) -> ProactivePreferencesBundle:
    """Strict actorless startup/job read while the exact-one bridge is open.

    This compatibility API is deliberately separate from the human read API.
    It serializes subject cardinality under identity governance, locks the
    canonical S/Q/C roots, and then locks all three scoped setting rows. It
    fails closed as soon as the database contains another health subject.
    """

    if not isinstance(scope, ProactivePreferencesScope) or not scope.include_legacy:
        raise ProactivePreferencesValidationError(
            "actorless preference reads require an exact-one legacy scope"
        )
    bridge_open = await _lock_write_roots(
        session,
        scope,
        actor_lookup_key=None,
    )
    if not bridge_open:
        raise LegacyProactivePreferencesBridgeClosedError(
            "legacy proactive preference bridge is closed"
        )
    subject, delivery, garmin = _require_complete_rows(
        await _setting_rows(session, scope, for_update=True)
    )
    return _decode_bundle(subject.value, delivery.value, garmin.value)


async def get_subject_policy(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
) -> SubjectProactivePolicy:
    if not isinstance(subject_id, uuid.UUID) or subject_id.int == 0:
        raise ProactivePreferencesValidationError(
            "subject_id must be a non-zero UUID"
        )
    with session.no_autoflush:
        row = await session.scalar(
            select(SubjectSetting)
            .join(HealthSubject, HealthSubject.id == SubjectSetting.subject_id)
            .where(
                SubjectSetting.subject_id == subject_id,
                SubjectSetting.key == SUBJECT_POLICY_KEY,
            )
        )
    if row is None:
        # No rows at all is the deliberate pre-opt-in state. A missing subject
        # partition beside either connection partition is a torn bundle and
        # must keep failing loudly rather than being mistaken for onboarding.
        partial_connection_row = await session.scalar(
            select(IntegrationConnectionSetting.key)
            .join(
                IntegrationConnection,
                IntegrationConnection.id
                == IntegrationConnectionSetting.integration_connection_id,
            )
            .where(
                IntegrationConnection.subject_id == subject_id,
                IntegrationConnectionSetting.key.in_(
                    (TELEGRAM_DELIVERY_POLICY_KEY, GARMIN_POLICY_KEY)
                ),
            )
            .limit(1)
        )
        if partial_connection_row is not None:
            raise ProactivePreferencesUnavailableError(
                "scoped proactive preference split is partial"
            )
        raise ProactivePreferencesNotConfiguredError(
            "subject proactive preference row is missing"
        )
    return _decode_subject(row.value)


async def get_garmin_policy(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    integration_connection_id: uuid.UUID,
) -> GarminProactivePolicy:
    for field, value in (
        ("subject_id", subject_id),
        ("integration_connection_id", integration_connection_id),
    ):
        if not isinstance(value, uuid.UUID) or value.int == 0:
            raise ProactivePreferencesValidationError(
                f"{field} must be a non-zero UUID"
            )
    with session.no_autoflush:
        row = await session.scalar(
            select(IntegrationConnectionSetting)
            .join(
                IntegrationConnection,
                IntegrationConnection.id
                == IntegrationConnectionSetting.integration_connection_id,
            )
            .where(
                IntegrationConnectionSetting.integration_connection_id
                == integration_connection_id,
                IntegrationConnectionSetting.key == GARMIN_POLICY_KEY,
                IntegrationConnection.subject_id == subject_id,
                IntegrationConnection.provider == IntegrationProvider.GARMIN.value,
                IntegrationConnection.connection_type
                == IntegrationConnectionType.ACCOUNT.value,
                IntegrationConnection.status.in_(_NON_RETIRED_CONNECTION_STATUSES),
            )
        )
    if row is None:
        raise ProactivePreferencesUnavailableError(
            "Garmin proactive preference row is missing or out of scope"
        )
    return _decode_garmin(row.value)


async def get_locked_delivery_policy(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    recipient_user_id: uuid.UUID,
    integration_connection_id: uuid.UUID,
) -> DeliveryPolicy:
    """Read strict Telegram policy after canonical roots are already locked.

    The caller must hold governance -> S -> Q -> Telegram-C locks. This function
    performs no ``FOR UPDATE``, advisory lock, legacy lookup, or default
    projection. It rechecks the exact graph in the current transaction and reads
    only the connection-scoped policy row.
    """

    for field, value in (
        ("subject_id", subject_id),
        ("recipient_user_id", recipient_user_id),
        ("integration_connection_id", integration_connection_id),
    ):
        if not isinstance(value, uuid.UUID) or value.int == 0:
            raise ProactivePreferencesValidationError(
                f"{field} must be a non-zero UUID"
            )
    with session.no_autoflush:
        raw = await session.scalar(
            select(IntegrationConnectionSetting.value)
            .join(
                IntegrationConnection,
                IntegrationConnection.id
                == IntegrationConnectionSetting.integration_connection_id,
            )
            .join(
                HealthSubject,
                HealthSubject.id == IntegrationConnection.subject_id,
            )
            .join(User, User.id == HealthSubject.owner_user_id)
            .where(
                IntegrationConnectionSetting.integration_connection_id
                == integration_connection_id,
                IntegrationConnectionSetting.key == TELEGRAM_DELIVERY_POLICY_KEY,
                IntegrationConnection.subject_id == subject_id,
                IntegrationConnection.provider == IntegrationProvider.TELEGRAM.value,
                IntegrationConnection.connection_type
                == IntegrationConnectionType.RECIPIENT.value,
                IntegrationConnection.status.in_(_LIVE_CONNECTION_STATUSES),
                HealthSubject.owner_user_id == recipient_user_id,
                User.status == UserStatus.ACTIVE.value,
            )
        )
    if raw is None:
        raise ProactivePreferencesUnavailableError(
            "Telegram delivery policy is missing or out of scope"
        )
    return _decode_delivery(raw)


async def governs_the_process_schedule(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
) -> bool:
    """Whether this subject may govern process-wide provider cadences.

    Garmin polling and weight-export cadences are stored per subject but still
    produce one process-wide scheduler trigger.  While an installation is one
    person those are the same schedule.  With two, rebuilding the registry from
    either person's Save would quietly move provider work for everybody.

    Daily Brief is not part of this compatibility decision: its minutely
    dispatcher reads each active subject's own time at the tick. Startup already
    keeps shared-install provider defaults rather than guessing one person's
    cadence; this gives the save path the same answer.
    """

    if not isinstance(subject_id, uuid.UUID) or subject_id.int == 0:
        raise ProactivePreferencesValidationError(
            "subject_id must be a non-zero UUID"
        )
    subject_ids = list(
        await session.scalars(
            select(HealthSubject.id).order_by(HealthSubject.id).limit(2)
        )
    )
    return subject_ids == [subject_id]

async def _lock_write_roots(
    session: AsyncSession,
    scope: ProactivePreferencesScope,
    *,
    actor_lookup_key: str | None,
) -> bool:
    """Lock canonical roots and report whether the legacy mirror still applies.

    It used to refuse here, for every caller, the moment a second subject
    existed — and the caller that meets that first is a person clicking Save on
    their own notification settings, whose scoped rows are unambiguous and whose
    write has nothing to do with the mirror. What stops meaning anything with
    two people is the shared ``app_settings`` key, not the subject-scoped row;
    the same distinction ``scoped_settings_service`` already draws.

    So the cardinality is reported rather than enforced. The two callers that
    genuinely need a sole subject — the startup adoption of the legacy row and
    the actorless startup read — refuse on a ``False`` themselves, where the
    refusal names what it is actually about.
    """

    await acquire_identity_governance_lock(session)
    await _validate_scope_roots(
        session,
        scope,
        for_update=True,
        actor_lookup_key=actor_lookup_key,
    )
    subject_ids = list(
        await session.scalars(
            select(HealthSubject.id).order_by(HealthSubject.id).limit(2)
        )
    )
    exact_one = subject_ids == [scope.subject_id]
    return exact_one and scope.include_legacy
