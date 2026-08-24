"""Resolve a real :class:`~vitals.access.AccessContext` and enforce it.

PR-02 built the policy vocabulary — principals, grants, scopes, and
:func:`~vitals.access.is_allowed` — and nothing has ever called it. Every scoped
path instead goes through ``resolve_legacy_ownership_context``, which answers a
narrower question: *is this the sole owner of the sole subject?* That was exactly
right while one subject existed, and it is the reason a second one is refused
outright rather than merely kept apart.

This module asks the question the policy engine was written for: *may this
principal reach that subject, for this action?* Self-ownership answers yes on its
own, which is why nothing changes for the installation as it stands. What changes
is the shape of the refusal: a second subject stops being an error about the
database's cardinality and becomes an ordinary denial about somebody else's data.

Relationship and support grants are read here too, so a professional or a
support engineer with a live, exactly-scoped grant is authorized by the same
evaluation rather than by a second code path.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.access import (
    AccessContext,
    AccessRequest,
    PolicyAction,
    PolicyResourceType,
    Principal,
    is_allowed,
)
from vitals.enums import UserRoleName, UserStatus
from vitals.models.identity import HealthSubject, User, UserRole
from vitals.services.rls_session import bind_session_subject


class AccessResolutionError(RuntimeError):
    """The principal or subject for this operation could not be established."""


class PrincipalNotFoundError(AccessResolutionError):
    """No active user matches the supplied identity."""


class SubjectNotFoundError(AccessResolutionError):
    """The requested health subject does not exist."""


class NoAccessibleSubjectError(AccessResolutionError):
    """The principal owns no health subject and none was named."""


class AccessDeniedError(AccessResolutionError):
    """The policy engine refused this exact resource and action."""


async def _load_principal(session: AsyncSession, user_id: uuid.UUID) -> Principal:
    with session.no_autoflush:
        row = (
            await session.execute(
                select(User.id, User.status, User.session_version).where(
                    User.id == user_id
                )
            )
        ).one_or_none()
    if row is None:
        raise PrincipalNotFoundError(f"user {user_id} does not exist")
    resolved_id, status, session_version = row
    if status != UserStatus.ACTIVE.value:
        raise PrincipalNotFoundError(f"user {user_id} is not active")
    with session.no_autoflush:
        roles = frozenset(
            UserRoleName(value)
            for value in await session.scalars(
                select(UserRole.role).where(UserRole.user_id == resolved_id)
            )
        )
    return Principal(
        user_id=resolved_id,
        roles=roles,
        session_version=session_version or 1,
    )


async def resolve_access_context(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    subject_id: uuid.UUID | None,
    evaluated_at: datetime | None = None,
) -> AccessContext:
    """Build the immutable snapshot one policy evaluation is decided from.

    ``subject_id`` is mandatory and has exactly two readings: an id names the
    person whose data is being reached for, and ``None`` means the subject this
    principal owns. Note what neither reading is — it never falls back to "the
    only subject in the database". Reaching somebody else's record is a
    decision, and a decision has to be made by naming them.

    The parameter has no default on purpose. A default would let a caller arrive
    here without having thought about whose data it wants, which is the shape
    every scoped service in this codebase has just finished removing.

    Building a context authorizes nothing. :func:`require_access` is what decides,
    and it is deny-by-default for every subject the principal does not own.
    """

    if not isinstance(user_id, uuid.UUID) or user_id.int == 0:
        raise AccessResolutionError("user_id must be a non-zero UUID")
    principal = await _load_principal(session, user_id)

    with session.no_autoflush:
        if subject_id is None:
            # ``uq_health_subjects_owner_user_id`` makes this at most one row,
            # so "the subject they own" is never ambiguous.
            resolved_subject_id = await session.scalar(
                select(HealthSubject.id).where(
                    HealthSubject.owner_user_id == principal.user_id
                )
            )
            if resolved_subject_id is None:
                raise NoAccessibleSubjectError(
                    f"user {principal.user_id} owns no health subject"
                )
            owner_user_id = principal.user_id
        else:
            if not isinstance(subject_id, uuid.UUID) or subject_id.int == 0:
                raise AccessResolutionError("subject_id must be a non-zero UUID")
            owner_user_id = await session.scalar(
                select(HealthSubject.owner_user_id).where(
                    HealthSubject.id == subject_id
                )
            )
            if owner_user_id is None:
                raise SubjectNotFoundError(
                    f"health subject {subject_id} does not exist"
                )
            resolved_subject_id = subject_id

    decided_at = evaluated_at or datetime.now(timezone.utc)

    # Only for somebody else's record. The owner's own access needs no
    # relationship, and looking one up for them would be a query per request
    # that can only ever answer "no" — and, worse, a place for a stray row to
    # start meaning something about a person who already has full access.
    relationship_grant = None
    support_grant = None
    if owner_user_id != principal.user_id:
        from vitals.services.care.relationships import load_relationship_grant

        with session.no_autoflush:
            relationship_grant = await load_relationship_grant(
                session,
                subject_id=resolved_subject_id,
                professional_user_id=principal.user_id,
                evaluated_at=decided_at,
            )

        # Only for an actual superadmin, and only for somebody else's record.
        # This module's docstring has promised support grants were read here
        # since it was written, and until now they were not: the policy engine
        # understood them, nothing ever handed it one, and an approved grant
        # authorized exactly nothing. Gated on the role so an ordinary
        # professional's request does not pay for a query that can only ever
        # answer "no".
        if UserRoleName.PLATFORM_SUPERADMIN in principal.roles:
            from vitals.services.support_access_service import load_support_grant

            with session.no_autoflush:
                support_grant = await load_support_grant(
                    session,
                    subject_id=resolved_subject_id,
                    admin_user_id=principal.user_id,
                    evaluated_at=decided_at,
                )

    return AccessContext(
        principal=principal,
        subject_id=resolved_subject_id,
        subject_owner_user_id=owner_user_id,
        evaluated_at=decided_at,
        relationship_grant=relationship_grant,
        support_grant=support_grant,
    )


async def enter_subject_scope(session: AsyncSession, context: AccessContext) -> None:
    """Bind the database boundary to the subject this operation may proceed for.

    Deliberately separate from resolution. Building a context is a question —
    *may this principal reach that record?* — and a question about somebody
    else's data must be answerable without first entering their scope. Binding
    is the answer being acted on, so it belongs after :func:`require_access`,
    not before it.
    """

    await bind_session_subject(session, context.subject_id)


def require_access(
    context: AccessContext,
    *,
    resource_type: PolicyResourceType,
    resource_key: str,
    action: PolicyAction,
) -> None:
    """Raise unless the policy engine authorizes this exact resource and action.

    The refusal deliberately says nothing about whether the subject exists or
    what it holds: a denial and a miss look the same from outside, so probing
    with somebody else's id learns nothing.
    """

    request = AccessRequest(
        subject_id=context.subject_id,
        resource_type=resource_type,
        resource_key=resource_key,
        action=action,
    )
    if not is_allowed(context, request):
        raise AccessDeniedError(
            f"{action.value} on {resource_type.value} is not authorized "
            "for this principal and subject"
        )


__all__ = [
    "AccessDeniedError",
    "enter_subject_scope",
    "AccessResolutionError",
    "NoAccessibleSubjectError",
    "PrincipalNotFoundError",
    "SubjectNotFoundError",
    "require_access",
    "resolve_access_context",
]
