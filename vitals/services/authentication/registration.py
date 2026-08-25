"""Whether this installation may make an account, and by whose decision.

Registration has been closed since the commercial work started, and closed by
absence: ``authentication.federation`` refuses an unrecognised identity because
nothing anywhere could create one. That was the right state and a poor
mechanism — "closed" was a property of there being no code, so opening it later
means writing the decision at the same time as the door, which is how a door
gets opened by accident.

This is the decision, written first and answering **disabled**.

Four modes, from the plan:

``disabled``
    No account is created by anything but an operator running the CLI, which is
    not registration: it is somebody with shell access on the machine.
``invite_only``
    A named person was invited by somebody who already has a record here.
``admin_approved``
    Anybody may ask; an administrator decides.
``open``
    Anybody who can authenticate with the identity provider gets an account.

**Only the first is reachable.** ``VITALS_REGISTRATION_UNLOCKED`` gates the other
three, and it is an environment variable rather than a stored setting on
purpose: the release plan makes opening registration a deployment decision that
comes after a security review, and a mode somebody can flip from a settings page
is not that. Until it is set, ``effective_mode`` answers ``disabled`` no matter
what is stored — so the stored value can be configured, reviewed and tested
ahead of the release that makes it mean anything.

The two middle modes have no implementation yet and say so: they resolve to a
refusal that names itself, rather than falling through to ``open``.
"""
from __future__ import annotations

import os
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.models.scoped_settings import PlatformSetting
from vitals.services.identity_service import acquire_identity_governance_lock

#: The ``platform_settings`` key this module owns.
REGISTRATION_MODE_KEY = "registration_mode"

#: The deployment-level gate. Set to a truthy value to let a stored mode other
#: than ``disabled`` take effect.
REGISTRATION_UNLOCK_ENV = "VITALS_REGISTRATION_UNLOCKED"

_TRUTHY = frozenset({"1", "true", "yes", "on"})


class RegistrationMode(StrEnum):
    DISABLED = "disabled"
    INVITE_ONLY = "invite_only"
    ADMIN_APPROVED = "admin_approved"
    OPEN = "open"


class RegistrationError(Exception):
    """Base class for a fail-closed registration error."""


class RegistrationClosed(RegistrationError):
    """This installation does not create accounts by this route.

    One exception type for every reason — the mode is ``disabled``, the
    deployment gate is off, the mode is one that has no implementation — because
    the difference matters to an operator reading a log and to nobody standing
    at the door. The message carries it; the answer does not.
    """


class RegistrationValidationError(RegistrationError):
    """A stored or supplied mode is not one of the four."""


def _coerce(value: object) -> RegistrationMode:
    if isinstance(value, RegistrationMode):
        return value
    if isinstance(value, str):
        try:
            return RegistrationMode(value.strip().casefold())
        except ValueError as exc:
            raise RegistrationValidationError(
                f"{value!r} is not a registration mode"
            ) from exc
    raise RegistrationValidationError("a registration mode must be a string")


def deployment_is_unlocked() -> bool:
    """Whether this deployment has been cleared to open registration at all."""

    return (os.getenv(REGISTRATION_UNLOCK_ENV) or "").strip().casefold() in _TRUTHY


async def get_stored_mode(session: AsyncSession) -> RegistrationMode:
    """What the installation has configured, before the deployment gate.

    Shown to an administrator so the setting they are looking at is the setting
    they set. ``effective_mode`` is what anything acts on.
    """

    with session.no_autoflush:
        raw = await session.scalar(
            select(PlatformSetting.value).where(
                PlatformSetting.key == REGISTRATION_MODE_KEY
            )
        )
    if raw is None:
        return RegistrationMode.DISABLED
    if isinstance(raw, dict):
        raw = raw.get("mode")
    try:
        return _coerce(raw)
    except RegistrationValidationError:
        # A stored value this build does not understand is not permission to
        # guess. It reads as closed, which is the only safe reading.
        return RegistrationMode.DISABLED


async def effective_mode(session: AsyncSession) -> RegistrationMode:
    """The mode anything may act on."""

    if not deployment_is_unlocked():
        return RegistrationMode.DISABLED
    return await get_stored_mode(session)


async def set_stored_mode(
    session: AsyncSession, mode: RegistrationMode | str
) -> RegistrationMode:
    """Record the mode. Never commits, and never checks who is asking.

    Authorization belongs to the caller — ``require_installation_operator`` at
    the boundary — for the same reason it does everywhere else here: a service
    that authorizes is a service that can be called from somewhere that has
    already authorized differently.
    """

    resolved = _coerce(mode)
    # This is the same fence every account admission takes.  Row-locking only
    # the setting is insufficient: an admission may already have read ``open``
    # and be waiting to create identity rows.  With one lock order, either that
    # admission commits first or the closure wins and its waiter re-reads the
    # now-disabled mode.
    await acquire_identity_governance_lock(session)
    row = await session.scalar(
        select(PlatformSetting)
        .where(PlatformSetting.key == REGISTRATION_MODE_KEY)
        .with_for_update()
    )
    if row is None:
        session.add(
            PlatformSetting(key=REGISTRATION_MODE_KEY, value={"mode": resolved.value})
        )
    else:
        row.value = {"mode": resolved.value}
    await session.flush()
    return resolved


async def require_open_registration(session: AsyncSession) -> RegistrationMode:
    """Raise unless an unrecognised identity may become an account right now.

    The only mode that currently returns is ``open``, and it is unreachable
    without the deployment gate. ``invite_only`` and ``admin_approved`` refuse
    with their own message rather than being quietly treated as ``open``: a
    half-built mode that behaves like the most permissive one is the failure
    this module exists to prevent.
    """

    mode = await effective_mode(session)
    if mode is RegistrationMode.OPEN:
        return mode
    if mode is RegistrationMode.DISABLED:
        if not deployment_is_unlocked():
            raise RegistrationClosed(
                "registration is closed: this deployment has not set "
                f"{REGISTRATION_UNLOCK_ENV}"
            )
        raise RegistrationClosed("registration is closed by installation setting")
    raise RegistrationClosed(
        f"registration mode {mode.value!r} is configured but not implemented; "
        "no account is created"
    )


__all__ = [
    "REGISTRATION_MODE_KEY",
    "REGISTRATION_UNLOCK_ENV",
    "RegistrationClosed",
    "RegistrationError",
    "RegistrationMode",
    "RegistrationValidationError",
    "deployment_is_unlocked",
    "effective_mode",
    "get_stored_mode",
    "require_open_registration",
    "set_stored_mode",
]
