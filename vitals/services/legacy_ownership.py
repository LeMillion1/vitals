"""Fail-closed ownership resolution for unchanged legacy write paths.

This adapter gives single-subject callers an authoritative subject, owner,
optional human actor, and only the explicitly requested integration roots.  It
is intentionally read-only: it does not bootstrap or repair identity/tenancy
state, acquire a mutation lock, inspect configuration, flush, or commit.

Disabled connections remain valid provenance roots.  ``status`` describes
whether new provider activity is enabled, while historical facts must continue
to reference the same non-retired connection.  A retired root is never chosen.
"""
from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import (
    IntegrationConnectionStatus,
    IntegrationProvider,
    UserStatus,
)
from vitals.models.identity import HealthSubject, User
from vitals.models.tenancy import IntegrationConnection
from vitals.access import AccessContext
from vitals.ownership import WriteIdentity
from vitals.services.rls_session import bind_session_subject
from vitals.services.identity_service import IdentityValidationError, normalize_username
from vitals.services.tenancy_bootstrap import LEGACY_CONNECTION_TYPES


class LegacyOwnershipError(Exception):
    """Base class for fail-closed legacy ownership resolution errors."""


class LegacyOwnershipValidationError(LegacyOwnershipError):
    """Resolver input is invalid or does not use the frozen enum contract."""


class LegacySubjectResolutionError(LegacyOwnershipError):
    """There is not exactly one local health subject."""


class NoPersonalRecordError(LegacySubjectResolutionError):
    """The signed-in account keeps no health record of its own.

    A doctor or a trainer is a real account with no personal record, and that is
    not a fault, a missing feature, or a half-finished migration. Every page
    that answers "your weight", "your labs", "your day" has nothing to be about
    for them, and the honest answer names that rather than reporting a limit
    that does not apply — they were being told the page did not support several
    records yet, which sends them looking for a setting that will never exist.
    """


class LegacyOwnerResolutionError(LegacyOwnershipError):
    """The sole subject does not have a resolvable active owner identity."""


class LegacyActorMismatchError(LegacyOwnershipError):
    """A human actor does not match the subject's owner identity."""


class LegacyConnectionResolutionError(LegacyOwnershipError):
    """A requested provider does not have one usable provenance root."""

    def __init__(self, provider: IntegrationProvider, detail: str) -> None:
        self.provider = provider
        super().__init__(f"legacy {provider.value} connection {detail}")


class LegacyConnectionMissingError(LegacyConnectionResolutionError):
    """No connection exists for the provider's frozen legacy type."""


class LegacyConnectionAmbiguousError(LegacyConnectionResolutionError):
    """More than one non-retired connection matches the provider/type pair."""


class LegacyConnectionRetiredError(LegacyConnectionResolutionError):
    """The provider/type pair exists only as retired provenance."""


class LegacyConnectionStateError(LegacyConnectionResolutionError):
    """A matching connection has an unknown persisted lifecycle state."""


class LegacyConnectionNotResolvedError(LegacyConnectionResolutionError):
    """A caller requested an ID that was not part of this resolution."""


@dataclass(frozen=True, slots=True)
class LegacyOwnershipContext:
    """Immutable ownership roots for one legacy operation."""

    subject_id: uuid.UUID
    owner_user_id: uuid.UUID
    actor_user_id: uuid.UUID | None
    connection_ids: Mapping[IntegrationProvider, uuid.UUID]
    #: The policy snapshot this operation may be decided from, when a human is
    #: behind it. ``None`` for a scheduled job or a provider callback: there is
    #: no principal to evaluate, and the job acts as the system rather than as
    #: somebody. Callers that hand data across a boundary — an export, a share,
    #: an external read — ask ``access_resolution.require_access`` against this
    #: rather than inferring permission from having got this far.
    access: AccessContext | None = None

    def __post_init__(self) -> None:
        for field_name in ("subject_id", "owner_user_id"):
            if not isinstance(getattr(self, field_name), uuid.UUID):
                raise LegacyOwnershipValidationError(
                    f"{field_name} must be a UUID"
                )
        if self.actor_user_id is not None and not isinstance(
            self.actor_user_id, uuid.UUID
        ):
            raise LegacyOwnershipValidationError(
                "actor_user_id must be a UUID or None"
            )

        copied: dict[IntegrationProvider, uuid.UUID] = {}
        try:
            items = self.connection_ids.items()
        except AttributeError as exc:
            raise LegacyOwnershipValidationError(
                "connection_ids must be a mapping"
            ) from exc
        for provider, connection_id in items:
            if not isinstance(provider, IntegrationProvider):
                raise LegacyOwnershipValidationError(
                    "connection_ids keys must be IntegrationProvider members"
                )
            if not isinstance(connection_id, uuid.UUID):
                raise LegacyOwnershipValidationError(
                    "connection_ids values must be UUIDs"
                )
            copied[provider] = connection_id
        object.__setattr__(self, "connection_ids", MappingProxyType(copied))

    @property
    def write_identity(self) -> WriteIdentity:
        """Return the domain-service attribution authorized by this lookup."""

        return WriteIdentity(
            subject_id=self.subject_id,
            actor_user_id=self.actor_user_id,
        )

    def owner_action(self) -> WriteIdentity:
        """Attribute a write to the owner at an authenticated human boundary."""

        return WriteIdentity(
            subject_id=self.subject_id,
            actor_user_id=self.owner_user_id,
        )

    def system_action(self) -> WriteIdentity:
        """Attribute a trusted scheduler/system write without a human actor."""

        return WriteIdentity(
            subject_id=self.subject_id,
            actor_user_id=None,
        )

    def connection_id(self, provider: IntegrationProvider) -> uuid.UUID:
        """Return one explicitly resolved connection ID."""

        if not isinstance(provider, IntegrationProvider):
            raise LegacyOwnershipValidationError(
                "provider must be an IntegrationProvider member"
            )
        try:
            return self.connection_ids[provider]
        except KeyError:
            raise LegacyConnectionNotResolvedError(
                provider, "was not requested"
            ) from None


def _validated_required_connections(
    required_connections: Iterable[IntegrationProvider],
) -> tuple[IntegrationProvider, ...]:
    if required_connections is None or isinstance(
        required_connections, (str, bytes)
    ):
        raise LegacyOwnershipValidationError(
            "required_connections must be an iterable of IntegrationProvider members"
        )
    try:
        providers = tuple(required_connections)
    except TypeError as exc:
        raise LegacyOwnershipValidationError(
            "required_connections must be iterable"
        ) from exc

    seen: set[IntegrationProvider] = set()
    for provider in providers:
        if not isinstance(provider, IntegrationProvider):
            raise LegacyOwnershipValidationError(
                "required_connections must contain only IntegrationProvider members"
            )
        if provider not in LEGACY_CONNECTION_TYPES:
            raise LegacyOwnershipValidationError(
                f"provider {provider.value!r} has no legacy connection type"
            )
        if provider in seen:
            raise LegacyOwnershipValidationError(
                f"provider {provider.value!r} was requested more than once"
            )
        seen.add(provider)
    return providers


async def resolve_legacy_ownership_context(
    session: AsyncSession,
    *,
    actor_username: str | None,
    required_connections: Iterable[IntegrationProvider] = (),
) -> LegacyOwnershipContext:
    """Resolve the sole active-owner context without changing database state.

    ``actor_username=None`` represents a trusted system/job boundary and leaves
    ``actor_user_id`` unset.  A supplied username is normalized with the same
    NFKC/strip/casefold rule as identity bootstrap and must name the sole
    subject's owner exactly.  Additive roles are deliberately irrelevant to
    ownership.
    """

    providers = _validated_required_connections(required_connections)
    if actor_username is None:
        actor_lookup_key = None
    else:
        try:
            actor_lookup_key = normalize_username(actor_username).lookup_key
        except IdentityValidationError as exc:
            raise LegacyOwnershipValidationError(str(exc)) from exc

    # Prevent a nominally read-only lookup from autoflushing unrelated pending
    # caller state. Pending identity/tenancy rows are not authoritative roots.
    with session.no_autoflush:
        if actor_lookup_key is not None:
            # A named actor selects *their own* record. This used to ask whether
            # the database held exactly one subject, which is a different
            # question and the wrong one: the check further down already
            # requires the actor to own the subject, so the count was standing
            # in for a relationship it could have asked about directly.
            #
            # It mattered the moment a second person existed. Every page in the
            # app resolves through here, so a doctor taking on one patient lost
            # their own dashboard, their own weight log and their own settings —
            # the count refused, and it refused for everybody rather than for
            # the one request that was genuinely ambiguous.
            #
            # ``uq_health_subjects_owner_user_id`` makes this at most one row,
            # so "the subject they own" is never ambiguous either.
            subject_rows = list(
                await session.execute(
                    select(HealthSubject.id, HealthSubject.owner_user_id)
                    .join(User, User.id == HealthSubject.owner_user_id)
                    .where(User.normalized_username == actor_lookup_key)
                    .limit(2)
                )
            )
            if not subject_rows:
                raise NoPersonalRecordError(
                    "this account keeps no health record of its own"
                )
            if len(subject_rows) != 1:
                raise LegacySubjectResolutionError(
                    "legacy ownership requires the actor to own exactly one "
                    f"health subject; found {len(subject_rows)}"
                )
        else:
            # No actor to select by — a scheduled job or the startup bootstrap.
            # There the sole-subject requirement is the only honest answer:
            # nothing names whose record was meant, and picking one would be
            # inventing the answer.
            subject_rows = list(
                await session.execute(
                    select(HealthSubject.id, HealthSubject.owner_user_id)
                    .order_by(HealthSubject.id)
                    .limit(2)
                )
            )
            if len(subject_rows) != 1:
                count_label = "2 or more" if len(subject_rows) == 2 else str(
                    len(subject_rows)
                )
                raise LegacySubjectResolutionError(
                    "legacy ownership requires exactly one health subject; "
                    f"found {count_label}"
                )
        subject_id, owner_user_id = subject_rows[0]

    # Bind before the first read of a policy-protected table. ``health_subjects``
    # and ``users`` are the roots the boundary is defined *from*, so they carry
    # no policy; ``integration_connections`` below does, and the lookup would
    # come back empty against an unbound session.
    await bind_session_subject(session, subject_id)

    with session.no_autoflush:
        owner_row = (
            await session.execute(
                select(User.id, User.normalized_username, User.status).where(
                    User.id == owner_user_id
                )
            )
        ).one_or_none()
        if owner_row is None:
            raise LegacyOwnerResolutionError(
                "the sole health subject's owner identity does not exist"
            )
        resolved_owner_id, owner_lookup_key, owner_status = owner_row
        if owner_status != UserStatus.ACTIVE.value:
            raise LegacyOwnerResolutionError(
                "the sole health subject's owner identity is not active"
            )

        actor_user_id: uuid.UUID | None = None
        if actor_lookup_key is not None:
            if actor_lookup_key != owner_lookup_key:
                raise LegacyActorMismatchError(
                    "actor username does not match the sole health subject's owner"
                )
            actor_user_id = resolved_owner_id

        known_statuses = {status.value for status in IntegrationConnectionStatus}
        connection_ids: dict[IntegrationProvider, uuid.UUID] = {}
        for provider in providers:
            connection_type = LEGACY_CONNECTION_TYPES[provider]
            rows = list(
                await session.execute(
                    select(IntegrationConnection.id, IntegrationConnection.status)
                    .where(
                        IntegrationConnection.subject_id == subject_id,
                        IntegrationConnection.provider == provider.value,
                        IntegrationConnection.connection_type
                        == connection_type.value,
                    )
                    .order_by(IntegrationConnection.id)
                )
            )
            if not rows:
                raise LegacyConnectionMissingError(
                    provider,
                    f"of type {connection_type.value!r} does not exist",
                )

            unknown_statuses = sorted(
                {status for _connection_id, status in rows} - known_statuses
            )
            if unknown_statuses:
                raise LegacyConnectionStateError(
                    provider,
                    f"has unknown status {unknown_statuses[0]!r}",
                )

            non_retired = [
                row
                for row in rows
                if row.status != IntegrationConnectionStatus.RETIRED.value
            ]
            if not non_retired:
                raise LegacyConnectionRetiredError(
                    provider,
                    f"of type {connection_type.value!r} is retired",
                )
            if len(non_retired) != 1:
                raise LegacyConnectionAmbiguousError(
                    provider,
                    f"of type {connection_type.value!r} has "
                    f"{len(non_retired)} non-retired matches",
                )
            connection_ids[provider] = non_retired[0].id

    access = None
    if actor_user_id is not None:
        # A named actor is a principal, so the operation can be *decided* rather
        # than merely resolved. The sole owner is authorized by self-ownership,
        # which is why nothing changes for this installation; what the snapshot
        # adds is somewhere for a denial to come from once it might.
        from vitals.services.access_resolution import (
            enter_subject_scope,
            resolve_access_context,
        )

        access = await resolve_access_context(
            session, user_id=actor_user_id, subject_id=subject_id
        )
        # This resolver only ever returns the subject its actor owns, so the
        # scope is entered here; a caller reaching for somebody else's record
        # goes through ``require_access`` and enters it only if allowed.
        await enter_subject_scope(session, access)

    return LegacyOwnershipContext(
        subject_id=subject_id,
        owner_user_id=resolved_owner_id,
        actor_user_id=actor_user_id,
        connection_ids=connection_ids,
        access=access,
    )


__all__ = [
    "LegacyActorMismatchError",
    "LegacyConnectionAmbiguousError",
    "LegacyConnectionMissingError",
    "LegacyConnectionNotResolvedError",
    "LegacyConnectionResolutionError",
    "LegacyConnectionRetiredError",
    "LegacyConnectionStateError",
    "LegacyOwnerResolutionError",
    "LegacyOwnershipContext",
    "LegacyOwnershipError",
    "LegacyOwnershipValidationError",
    "LegacySubjectResolutionError",
    "resolve_legacy_ownership_context",
]
