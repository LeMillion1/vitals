"""Which patient this request is about, and why this professional may see them.

One decision runs through this module and everything built on it: **the selected
patient travels in the URL, never in the session.**

A "currently selected patient" held server-side is the obvious design and it has
a failure that cannot be tested away. A professional opens patient A, leaves the
tab, selects patient B in another tab, comes back to the first and submits the
form that is still on screen. With the selection in a cookie, that write lands
on B — silently, with A's data in it. Nothing about the request looks wrong;
there is no error to notice; and the record it corrupts belongs to somebody who
was never involved.

With the selection in the path, the stale tab submits to the patient it was
rendered for. If the professional is still in care for A the write is correct,
and if consent for A was revoked in the meantime the write is refused. Both
outcomes are right, and neither requires the professional to have noticed
anything.

The same reasoning applies to the authorization itself. Nothing here is cached
across requests: the relationship, the consent and the policy decision are
resolved per request, which is what makes a revocation take effect on the next
one rather than on the next login.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.access import AccessContext, PolicyAction, PolicyResourceType, is_allowed
from vitals.enums import CareRelationshipStatus, ProfessionalKind
from vitals.models.identity import HealthSubject, User
from vitals.models.professional import CareRelationship
from vitals.services.access_resolution import (
    AccessResolutionError,
    resolve_access_context,
)
from web.config import SESSION_COOKIE
from web.deps import get_session, require_auth

#: Every failure below answers with this. A subject that does not exist, one the
#: caller has no relationship with, one whose consent has lapsed and one whose
#: consent never covered this are four different facts; told apart, a
#: professional could ask "is this person a patient here?" one id at a time.
_MISSING = HTTPException(status_code=status.HTTP_404_NOT_FOUND)


@dataclass(frozen=True, slots=True)
class CareContext:
    """One professional, one patient, one request, and the reason it is allowed.

    ``basis`` exists to be shown. A professional looking at somebody else's
    medical record should be able to see, without asking anybody, on what
    grounds they are being shown it — and a screen that cannot say why it is
    open is a screen nobody can audit by looking at it.
    """

    access: AccessContext
    subject_id: uuid.UUID
    subject_display_name: str
    relationship_id: uuid.UUID
    kind: ProfessionalKind
    consent_version: int
    #: Short machine-readable reason, for the banner and for logs.
    basis: str

    @property
    def is_owner(self) -> bool:
        """Whether this is the patient looking at their own record."""

        return self.basis == "self"

    @property
    def is_support(self) -> bool:
        """Whether this is platform support, here on an approved grant.

        Not a professional in care, and the screen must not describe them as
        one: ``kind`` carries a placeholder for them because the field is not
        nullable, and a banner reading "(Doctor)" over a support session would
        tell the patient something untrue about who is reading their record.
        """

        return self.basis == "support"

    def may(
        self,
        *,
        resource_key: str,
        action: PolicyAction = PolicyAction.READ,
        resource_type: PolicyResourceType = PolicyResourceType.DOMAIN,
    ) -> bool:
        """Ask the policy about one exact thing, for building a screen.

        Not an authorization: a route still decides for itself. This is what a
        navigation menu asks so it does not offer a link that answers 404.
        """

        from vitals.access import AccessRequest

        return is_allowed(
            self.access,
            AccessRequest(
                subject_id=self.subject_id,
                resource_type=resource_type,
                resource_key=resource_key,
                action=action,
            ),
        )


async def principal_user_id(request: Request, session: AsyncSession) -> uuid.UUID:
    """The account behind this browser session, as an id rather than a name.

    A version 2 session carries the id, which is what a federated installation
    issues. A version 1 session carries only the username, and is resolved here
    — the bridge exists because the two overlap during the cutover, not because
    a name is an identity.
    """

    from web.auth import decode_session

    claims = decode_session(request.cookies.get(SESSION_COOKIE))
    if claims is None:
        raise _MISSING
    if claims.user_id is not None:
        return claims.user_id

    from vitals.services.identity_service import normalize_username

    normalized = normalize_username(claims.username)
    user_id = await session.scalar(
        select(User.id).where(User.normalized_username == normalized.lookup_key)
    )
    if user_id is None:
        raise _MISSING
    return user_id


async def resolve_care_context(
    session: AsyncSession, *, user_id: uuid.UUID, subject_id: uuid.UUID
) -> CareContext:
    """Build the context for one professional looking at one patient.

    Resolved fresh every time. Caching it across requests is what would make a
    revocation take effect "eventually", and eventually is the wrong answer to
    a patient who has just withdrawn their consent.
    """

    try:
        access = await resolve_access_context(
            session, user_id=user_id, subject_id=subject_id
        )
    except AccessResolutionError:
        raise _MISSING from None

    display_name = await session.scalar(
        select(HealthSubject.display_name).where(HealthSubject.id == subject_id)
    )

    if access.subject_owner_user_id == user_id:
        # The patient's own record. There is no relationship to name and there
        # does not need to be — self-ownership is its own basis.
        return CareContext(
            access=access,
            subject_id=subject_id,
            subject_display_name=display_name or "",
            relationship_id=uuid.UUID(int=0),
            kind=ProfessionalKind.DOCTOR,
            consent_version=0,
            basis="self",
        )

    if access.support_grant is not None:
        # Platform support, on a grant this patient approved. The same screens,
        # deliberately: what a support engineer may see is decided by the policy
        # from the grant's exact scopes, and ``may()`` below already asks it —
        # so the record renders the domains that were agreed to and nothing
        # else, and every write affordance is hidden because a read grant
        # ceilings out at read. Building a second, narrower record view would
        # mean two places for "what may be shown" to drift apart.
        return CareContext(
            access=access,
            subject_id=subject_id,
            subject_display_name=display_name or "",
            relationship_id=uuid.UUID(int=0),
            #: A placeholder: the column is not nullable and support is not a
            #: professional kind. ``is_support`` is what the screens read.
            kind=ProfessionalKind.DOCTOR,
            consent_version=0,
            basis="support",
        )

    grant = access.relationship_grant
    if (
        grant is None
        or not grant.active
        or grant.revoked_at is not None
        or access.evaluated_at >= grant.expires_at
    ):
        # No relationship, or one that is paused, or a consent that is paused,
        # lapsed or revoked. All the same answer — see ``_MISSING``.
        raise _MISSING

    relationship_kind = await session.scalar(
        select(CareRelationship.kind).where(
            CareRelationship.id == grant.relationship_id,
            CareRelationship.status == CareRelationshipStatus.ACTIVE.value,
        )
    )
    if relationship_kind is None:
        raise _MISSING

    return CareContext(
        access=access,
        subject_id=subject_id,
        subject_display_name=display_name or "",
        relationship_id=grant.relationship_id,
        kind=ProfessionalKind(relationship_kind),
        consent_version=grant.consent_version,
        basis=f"care:{relationship_kind}",
    )


async def require_care_context(
    subject_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_session),
    _username: str = Depends(require_auth),
) -> CareContext:
    """Path dependency: ``/care/{subject_id}/...`` decides who this is about.

    ``subject_id`` comes from the path and from nowhere else. See the module
    docstring for why that is the whole design rather than a detail of it.

    ``require_auth`` runs first so a stranger gets the login redirect rather
    than a 404 — the uniform-refusal rule is about telling *authenticated*
    callers apart, and answering 404 to somebody with no session at all would
    only hide the login page from them.
    """

    user_id = await principal_user_id(request, db)
    return await resolve_care_context(db, user_id=user_id, subject_id=subject_id)


__all__ = [
    "CareContext",
    "principal_user_id",
    "require_care_context",
    "resolve_care_context",
]
