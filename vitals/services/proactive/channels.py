"""Шов 1 — the delivery channel, behind one small protocol.

``Notifier`` is the whole contract: send some text, optionally with tap-buttons,
optionally as a reply to an earlier message; get back the channel's own id for
what was sent (so a later reply can be matched to it) — plus redraw one already
sent. Nothing above this module imports ``httpx``, knows a chat id, or has heard
of Telegram.

    Adding web push / email / anything else: a class with these three methods
    plus one line in :func:`build_notifier`. No caller changes.

``answer_callback`` and ``edit`` are in the protocol on purpose even though only
Telegram has the concept: acknowledging a tap and redrawing a message are
*channel* details, and a channel without taps implements them as no-ops. The
alternative — callers sniffing for the method with ``getattr`` — would leak "is
this Telegram?" back up the stack.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Callable, Optional, Protocol, Sequence, runtime_checkable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.config import Config
from vitals.enums import (
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
)
from vitals.models.tenancy import IntegrationConnection
from vitals.services.tenancy.contracts import (
    LegacyOwnershipContext,
    LegacySubjectResolutionError,
)
from vitals.services.tenancy.ownership import (
    resolve_legacy_ownership_context,
)
from vitals.services.proactive.ownership import ProactiveOwnershipContext

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"
_TIMEOUT_SECONDS = 15.0
# Bot API hard limit. Over it the send is rejected outright, so a brief whose LLM
# tail ran long would simply never arrive — truncating visibly beats silence.
_MAX_TEXT = 4096

# One button = (visible label, callback payload). A flat list renders one button
# per row, which is all the current messages need; grouping into rows can be
# added the day a message actually wants it.
Buttons = Sequence[tuple[str, str]]

LEGACY_TELEGRAM_CREDENTIAL_REF = "legacy_env:telegram"


class NotifierBindingError(RuntimeError):
    """A notifier cannot be proven to target one durable recipient root."""


@dataclass(frozen=True, slots=True)
class DeliveryEndpointBinding:
    """Non-secret identity of one exact outbound transport endpoint."""

    subject_id: uuid.UUID
    recipient_user_id: uuid.UUID
    integration_connection_id: uuid.UUID
    channel: str

    def __post_init__(self) -> None:
        for field_name in (
            "subject_id",
            "recipient_user_id",
            "integration_connection_id",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, uuid.UUID) or value.int == 0:
                raise NotifierBindingError(f"{field_name} must be a non-zero UUID")
        if self.channel != IntegrationProvider.TELEGRAM.value:
            raise NotifierBindingError("durable delivery currently requires Telegram")


def canonicalize_text(text: str) -> str:
    """Return the exact plaintext Telegram will physically receive."""

    if not isinstance(text, str):
        raise TypeError("Telegram text must be a string")
    if len(text) > _MAX_TEXT:
        logger.warning("message of %s chars truncated to %s", len(text), _MAX_TEXT)
        return text[: _MAX_TEXT - 1] + "…"
    return text


_clip = canonicalize_text


def canonicalize_buttons(
    buttons: Optional[Buttons],
) -> tuple[tuple[str, str], ...] | None:
    """Validate and freeze the exact Telegram inline keyboard representation."""

    if buttons is None:
        return None
    if isinstance(buttons, (str, bytes)):
        raise ValueError("Telegram buttons must be a sequence of pairs")
    try:
        rows = tuple(buttons)
    except TypeError as exc:
        raise ValueError("Telegram buttons must be a sequence of pairs") from exc
    if len(rows) > 100:
        raise ValueError("Telegram buttons exceed the supported row count")
    normalized: list[tuple[str, str]] = []
    for row in rows:
        if (
            not isinstance(row, (tuple, list))
            or len(row) != 2
            or not all(isinstance(item, str) for item in row)
        ):
            raise ValueError("each Telegram button must be a pair of strings")
        label, callback_data = row
        if not label.strip() or len(label) > 64:
            raise ValueError("Telegram button label is blank or too long")
        callback_size = len(callback_data.encode("utf-8"))
        if not 1 <= callback_size <= 64:
            raise ValueError("Telegram callback data must be 1-64 UTF-8 bytes")
        normalized.append((label, callback_data))
    return tuple(normalized) or None


def _keyboard(buttons: Optional[Buttons]) -> dict:
    normalized = canonicalize_buttons(buttons)
    return {
        "inline_keyboard": [
            [{"text": label, "callback_data": data}]
            for label, data in normalized or ()
        ]
    }


@runtime_checkable
class Notifier(Protocol):
    channel: str

    async def send(
        self,
        text: str,
        *,
        buttons: Optional[Buttons] = None,
        reply_to: Optional[str] = None,
    ) -> str:
        """Deliver ``text``; return the channel's id for the sent message."""

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        """Acknowledge a button tap (no-op on channels without buttons)."""

    async def edit(
        self,
        message_id: str,
        text: str,
        *,
        buttons: Optional[Buttons] = None,
    ) -> None:
        """Redraw an already-sent message (no-op where the channel can't)."""


@runtime_checkable
class BoundNotifier(Notifier, Protocol):
    """A notifier whose private recipient is bound to exact S/Q/C roots."""

    binding: DeliveryEndpointBinding


BoundNotifierResolver = Callable[
    [DeliveryEndpointBinding, str], Optional[BoundNotifier]
]


def resolve_legacy_bound_notifier(
    binding: DeliveryEndpointBinding,
    credential_ref: str,
    *,
    config: Optional[Config] = None,
) -> Optional[BoundNotifier]:
    """No transport is configured, so no endpoint resolves.

    This is a seam, not a stub. The delivery journal below it is deliberately
    transport-agnostic — ``notification_delivery_intents`` says so in its own
    docstring: *a second delivery channel adds rows here, not a second table* —
    and every caller already handles "no endpoint" as an ordinary answer,
    because a Telegram that was not configured gave the same one.

    Telegram was the first channel and is gone: one bot token and one chat id in
    the environment, which is a single-user shape that a shared installation
    cannot have. Web push replaces it, and replaces it here — this function is
    where a resolver that reads a per-subject subscription belongs.
    """

    del binding, credential_ref, config
    return None


async def build_legacy_bound_notifier(
    session: AsyncSession,
    ownership: ProactiveOwnershipContext,
    *,
    config: Optional[Config] = None,
) -> Optional[BoundNotifier]:
    """Build the exact-one compatibility endpoint used by production callers.

    A global env token/chat pair is safe only while the durable graph resolves
    to exactly one subject, owner-recipient, and Telegram connection.  Future
    multi-recipient transports need a different credential resolver rather than
    attaching UUID metadata to this singleton configuration.
    """

    if not isinstance(ownership, ProactiveOwnershipContext):
        raise TypeError("ownership must be a ProactiveOwnershipContext")
    try:
        resolved = await resolve_legacy_ownership_context(
            session,
            actor_username=None,
            required_connections=(IntegrationProvider.TELEGRAM,),
        )
    except LegacySubjectResolutionError:
        # The environment-backed Telegram recipient is a frozen single-user
        # compatibility transport. Once another subject exists it is no longer
        # an endpoint we can prove belongs to this record, so it becomes the
        # same safe answer as an unconfigured channel: no send capability.
        return None
    expected_connection_id = resolved.connection_id(IntegrationProvider.TELEGRAM)
    if (
        resolved.subject_id != ownership.subject_id
        or resolved.owner_user_id != ownership.recipient_user_id
        or expected_connection_id != ownership.connection_id
    ):
        raise NotifierBindingError(
            "Telegram endpoint does not match the exact-one legacy owner graph"
        )
    row = (
        await session.execute(
            select(
                IntegrationConnection.subject_id,
                IntegrationConnection.provider,
                IntegrationConnection.connection_type,
                IntegrationConnection.status,
                IntegrationConnection.credential_ref,
            ).where(IntegrationConnection.id == ownership.connection_id)
        )
    ).one_or_none()
    if row is None:
        raise NotifierBindingError("Telegram recipient connection does not exist")
    subject_id, provider, connection_type, status, credential_ref = row
    if (
        subject_id != ownership.subject_id
        or provider != IntegrationProvider.TELEGRAM.value
        or connection_type != IntegrationConnectionType.RECIPIENT.value
        or status
        not in {
            IntegrationConnectionStatus.LEGACY.value,
            IntegrationConnectionStatus.ACTIVE.value,
        }
        or credential_ref != LEGACY_TELEGRAM_CREDENTIAL_REF
    ):
        raise NotifierBindingError("Telegram recipient connection is not dispatchable")
    binding = DeliveryEndpointBinding(
        subject_id=ownership.subject_id,
        recipient_user_id=ownership.recipient_user_id,
        integration_connection_id=ownership.connection_id,
        channel=IntegrationProvider.TELEGRAM.value,
    )
    return resolve_legacy_bound_notifier(
        binding,
        credential_ref,
        config=config,
    )


def build_notifier(config: Optional[Config] = None) -> Optional[Notifier]:
    """The factory. ``None`` = no channel configured, which is every call today.

    Callers treat that as "stay quiet", which is how the app behaved before the
    bot existed and how it behaves again now that the bot is gone.
    """

    del config
    logger.debug("no delivery channel configured; proactive messages are dropped")
    return None


def ownership_from_legacy(
    ownership: LegacyOwnershipContext,
) -> ProactiveOwnershipContext:
    """Project the current channel root without leaking its vendor upward."""

    if not isinstance(ownership, LegacyOwnershipContext):
        raise TypeError("ownership must be a LegacyOwnershipContext")
    return ProactiveOwnershipContext(
        subject_id=ownership.subject_id,
        recipient_user_id=ownership.owner_user_id,
        connection_id=ownership.connection_id(IntegrationProvider.TELEGRAM),
        include_legacy_unowned=True,
    )


async def resolve_subject_channel_ownership(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
) -> ProactiveOwnershipContext:
    """Channel roots for a system boundary that names its subject.

    The proactive jobs — the brief, the evening block, the nudges, the reply
    recovery — all arrive here. They used to arrive without naming anybody, which
    means "the sole subject or refuse", so every one of them stopped on a
    two-person installation. The subject is mandatory for the reason given in
    ``resolve_subject_ownership_context``: an omittable scope is the shape this
    codebase keeps out.
    """

    from vitals.services.tenancy.ownership import resolve_subject_ownership_context

    ownership = await resolve_subject_ownership_context(
        session,
        subject_id=subject_id,
        required_connections=(IntegrationProvider.TELEGRAM,),
    )
    return ownership_from_legacy(ownership)


async def resolve_legacy_channel_ownership(
    session: AsyncSession,
    *,
    actor_username: str | None,
) -> ProactiveOwnershipContext:
    """Resolve the single-user channel roots at the channel seam."""

    ownership = await resolve_legacy_ownership_context(
        session,
        actor_username=actor_username,
        required_connections=(IntegrationProvider.TELEGRAM,),
    )
    return ownership_from_legacy(ownership)
