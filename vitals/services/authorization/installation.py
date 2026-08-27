"""Who may act for the installation rather than for a person in it.

Most authorization here answers "may this principal do X to subject S", and
``vitals.access.is_allowed`` is built for exactly that shape. A few operations
have no S to put in the question. Replacing the database, restoring a backup
over it, restarting the process — none of them are about somebody's health
record, and asking a subject-scoped policy about them produces an answer to a
different question.

Passing the owner's own subject in as S is the tempting shortcut, and it is
wrong in a way that only shows up later: self-ownership authorizes everything on
one's own subject, so the check reads as a check while being unconditionally
true for every account in the installation. The second person to get an account
would inherit the restore button.

So the question is asked directly and has one answer: an active platform
superadmin is an operator.  The commercial bootstrap grants that role to the
historical installation owner explicitly; owning the only health subject is no
longer an implicit control-plane capability.  This keeps the authorization
stable when the second subject arrives and makes the UI and backend describe the
same role boundary.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import UserRoleName, UserStatus
from vitals.models.identity import User, UserRole


class NotAnOperator(Exception):
    """This principal may not act for the installation as a whole."""


async def require_installation_operator_user(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    operation: str,
) -> None:
    """Authorize an account without pretending it must own a health record.

    Platform administrators are installation operators even when they keep no
    personal record.  Subject ownership is deliberately irrelevant: restart,
    full restore, and installation export affect the whole service.
    """

    rows = (
        await session.execute(
            select(User.status, UserRole.role)
            .outerjoin(UserRole, UserRole.user_id == User.id)
            .where(User.id == user_id)
        )
    ).all()
    if not rows or rows[0].status != UserStatus.ACTIVE.value:
        raise NotAnOperator(f"{operation} needs an active principal behind it")
    if any(row.role == UserRoleName.PLATFORM_SUPERADMIN.value for row in rows):
        return

    raise NotAnOperator(
        f"{operation} is reserved for an operator of this installation"
    )


async def require_installation_operator(
    session: AsyncSession,
    *,
    access,
    operation: str,
) -> None:
    """Authorize one installation-wide operation, or refuse it.

    ``access`` is the resolved :class:`~vitals.access.AccessContext`. It carries
    the principal and roles; its selected health subject is intentionally not
    consulted for a control-plane decision.
    """

    if access is None:
        raise NotAnOperator(f"{operation} needs a principal behind it")

    if UserRoleName.PLATFORM_SUPERADMIN in access.principal.roles:
        return

    raise NotAnOperator(
        f"{operation} is reserved for an operator of this installation"
    )


__all__ = [
    "NotAnOperator",
    "require_installation_operator",
    "require_installation_operator_user",
]
