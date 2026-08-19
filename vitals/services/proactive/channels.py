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
from typing import Optional, Protocol, Sequence, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession

from vitals.config import Config, load_config
from vitals.enums import IntegrationProvider
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


def _clip(text: str) -> str:
    if len(text) > _MAX_TEXT:
        logger.warning("message of %s chars truncated to %s", len(text), _MAX_TEXT)
        return text[: _MAX_TEXT - 1] + "…"
    return text


def _keyboard(buttons: Optional[Buttons]) -> dict:
    return {
        "inline_keyboard": [
            [{"text": label, "callback_data": data}] for label, data in buttons or ()
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
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            resp = await client.post(url, json=payload)
        data = resp.json() if resp.content else {}
        if resp.status_code >= 400 or not data.get("ok"):
            raise RuntimeError(
                f"Telegram {method} failed ({resp.status_code}): "
                f"{data.get('description') or resp.text[:200]}"
            )
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
