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

from vitals.config import Config, load_config
from vitals.enums import (
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
)
from vitals.models.tenancy import IntegrationConnection
from vitals.services.legacy_ownership import (
    LegacyOwnershipContext,
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


class TelegramNotifier:
    """Bot API over plain ``httpx``. One chat, passed in — not read from config
    here, so the class itself carries nothing single-user about it."""

    channel = "telegram"

    def __init__(self, token: str, chat_id: str, *, base_url: str = TELEGRAM_API):
        try:
            private_recipient_id = int(chat_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("Telegram recipient must be a private user id") from exc
        if private_recipient_id <= 0:
            # Telegram groups/supergroups use negative chat ids. PHI delivery
            # must fail closed even if that id was accidentally configured.
            raise ValueError("Telegram recipient must be a private user id")
        self._token = token
        self._chat_id = str(private_recipient_id)
        self._base_url = base_url.rstrip("/")

    async def _call(self, method: str, payload: dict) -> dict:
        import httpx  # already a dependency; imported here like the other clients

        url = f"{self._base_url}/bot{self._token}/{method}"
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
                resp = await client.post(url, json=payload)
            data = resp.json() if resp.content else {}
            if not isinstance(data, dict):
                raise ValueError
        except Exception:
            # httpx exception strings include the request URL, which embeds the
            # bot token. JSON errors may include provider response fragments.
            raise RuntimeError(f"Telegram {method} transport failed") from None
        if resp.status_code >= 400 or not data.get("ok"):
            # HTTP exception strings and response bodies can include the bot URL,
            # request text, or provider diagnostics.  The durable delivery layer
            # records only an allowlisted outcome code, so keep this exception
            # equally sterile for direct edit/callback paths.
            raise RuntimeError(
                f"Telegram {method} failed ({resp.status_code})"
            ) from None
        return data.get("result") or {}

    async def send(
        self,
        text: str,
        *,
        buttons: Optional[Buttons] = None,
        reply_to: Optional[str] = None,
    ) -> str:
        # No parse_mode: everything sent so far is plain prose, and Markdown/HTML
        # would turn an unescaped '_' or '*' in the owner's own words into a
        # failed send. Formatting can be switched on when a message wants it.
        payload: dict = {"chat_id": self._chat_id, "text": _clip(text)}
        if buttons:
            payload["reply_markup"] = _keyboard(buttons)
        if reply_to:
            payload["reply_to_message_id"] = int(reply_to)
            # The message may have been deleted; a reply that can't attach should
            # still arrive rather than erroring the whole send.
            payload["allow_sending_without_reply"] = True
        result = await self._call("sendMessage", payload)
        return str(result.get("message_id") or "")

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        await self._call(
            "answerCallbackQuery", {"callback_query_id": callback_id, "text": text}
        )

    async def edit(
        self,
        message_id: str,
        text: str,
        *,
        buttons: Optional[Buttons] = None,
    ) -> None:
        # ``reply_markup`` always rides along, empty included: left out entirely,
        # Telegram keeps the previous keyboard — so the question just answered
        # would stay tappable under a line that already says it was answered.
        await self._call(
            "editMessageText",
            {
                "chat_id": self._chat_id,
                "message_id": int(message_id),
                "text": _clip(text),
                "reply_markup": _keyboard(buttons),
            },
        )


class BoundTelegramNotifier(TelegramNotifier):
    """Token-bearing Telegram client issued for one exact recipient graph."""

    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        binding: DeliveryEndpointBinding,
        base_url: str = TELEGRAM_API,
    ) -> None:
        if not isinstance(binding, DeliveryEndpointBinding):
            raise NotifierBindingError("binding must be a DeliveryEndpointBinding")
        super().__init__(token, chat_id, base_url=base_url)
        self._binding = binding

    @property
    def binding(self) -> DeliveryEndpointBinding:
        return self._binding

    def __setattr__(self, name, value) -> None:
        if name in {"binding", "_binding"} and hasattr(self, "_binding"):
            raise AttributeError("BoundTelegramNotifier binding is immutable")
        super().__setattr__(name, value)


BoundNotifierResolver = Callable[
    [DeliveryEndpointBinding, str], Optional[BoundNotifier]
]


def resolve_legacy_bound_notifier(
    binding: DeliveryEndpointBinding,
    credential_ref: str,
    *,
    config: Optional[Config] = None,
) -> Optional[BoundNotifier]:
    """Resolve the reviewed legacy-env Telegram endpoint without DB access.

    The delivery service calls this only after locking and proving exact S/Q/C
    and the connection's resolver handle.  Keeping the resolver synchronous
    mirrors the platform-AI gateway and gives tests a no-network factory seam.
    """

    if not isinstance(binding, DeliveryEndpointBinding):
        raise NotifierBindingError("binding must be a DeliveryEndpointBinding")
    if credential_ref != LEGACY_TELEGRAM_CREDENTIAL_REF:
        raise NotifierBindingError("Telegram credential resolver is not reviewed")
    config = config or load_config()
    if not config.telegram_bot_token or not config.telegram_chat_id:
        return None
    try:
        return BoundTelegramNotifier(
            config.telegram_bot_token,
            config.telegram_chat_id,
            binding=binding,
        )
    except ValueError:
        logger.warning(
            "Telegram delivery disabled: recipient is not a private user"
        )
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
    resolved = await resolve_legacy_ownership_context(
        session,
        actor_username=None,
        required_connections=(IntegrationProvider.TELEGRAM,),
    )
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
    """The factory. ``None`` = no channel configured — callers treat that
    as "stay quiet", which is how the app behaves before the bot exists."""
    config = config or load_config()
    if config.telegram_bot_token and config.telegram_chat_id:
        try:
            return TelegramNotifier(config.telegram_bot_token, config.telegram_chat_id)
        except ValueError:
            logger.warning("Telegram delivery disabled: recipient is not a private user")
            return None
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

    from vitals.services.legacy_ownership import resolve_subject_ownership_context

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
