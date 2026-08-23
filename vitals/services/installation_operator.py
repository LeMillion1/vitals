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

So the question is asked directly, and answered conservatively:

* a platform superadmin is an operator, which is what the role is for;
* while the installation holds exactly one subject, that subject's owner is the
  operator, because on a self-hosted install they are;
* the moment a second subject exists, the second clause stops applying and the
  operations close until somebody holds the role.

The last part is the point. These operations cannot be made safe per-subject —
a full restore wipes portable tables for everybody — so the honest behaviour
when the installation stops being one person's is to refuse, not to guess.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import UserRoleName
from vitals.models.identity import HealthSubject


class NotAnOperator(Exception):
    """This principal may not act for the installation as a whole."""


async def require_installation_operator(
    session: AsyncSession,
    *,
    access,
    operation: str,
) -> None:
    """Authorize one installation-wide operation, or refuse it.

    ``access`` is the resolved :class:`~vitals.access.AccessContext`. It carries
    the principal and its roles, and the subject it selected — which is used
    only to recognise the single-subject owner, never as the scope of the
    operation itself.
    """

    if access is None:
        raise NotAnOperator(f"{operation} needs a principal behind it")

    if UserRoleName.PLATFORM_SUPERADMIN in access.principal.roles:
        return

    subject_count = int(
        await session.scalar(select(func.count()).select_from(HealthSubject)) or 0
    )
    if subject_count == 1 and access.principal.user_id == access.subject_owner_user_id:
        return

    raise NotAnOperator(
        f"{operation} is reserved for an operator of this installation"
    )


__all__ = ["NotAnOperator", "require_installation_operator"]
