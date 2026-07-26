"""Telegram webhook — the one door the outside world knocks on.

Layered, because each layer stops a different thing:

  * the **path** itself is a random secret segment (``/tg/<path>``), so the
    endpoint isn't discoverable by crawling;
  * the ``X-Telegram-Bot-Api-Secret-Token`` **header** proves the caller is the
    Telegram that our ``setWebhook`` call configured;
  * both are compared with ``compare_digest`` — a plain ``==`` on a secret leaks
    its prefix through timing;
  * a rate limit sits in front (fail-open on Redis, as everywhere else);
  * and anything that clears all that but comes from **another chat** gets a
    plain ``200`` and the bin. Not a 403: a wrong answer tells a prober they
    found something, and Telegram would retry a non-200 for hours anyway.

Unconfigured (no secret in ``.env``) fails closed with 401 — never wide open.
Optionally narrow the path to Telegram's own subnets at the reverse proxy; that
is infrastructure, not code.
"""
from __future__ import annotations

import logging
from secrets import compare_digest
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.config import load_config
from vitals.services.proactive import channels, inbound
from web.deps import get_session
from web.ratelimit import login_rate_limit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tg", tags=["telegram"], include_in_schema=False)

# Telegram sends one update per request and retries failures; a real burst is a
# few a second at most. Generous enough never to drop a genuine update, tight
# enough that a leaked URL can't be hammered.
_RATE_LIMIT = login_rate_limit(bucket="telegram", limit=60, window=60)


async def get_notifier() -> Optional[channels.Notifier]:
    """The reply channel for this request. A dependency so tests can swap it for
    a fake without patching module internals."""
    return channels.build_notifier()


@router.post("/{secret_path}", dependencies=[Depends(_RATE_LIMIT)])
async def telegram_webhook(
    secret_path: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    notifier: Optional[channels.Notifier] = Depends(get_notifier),
) -> dict:
    config = load_config()
    expected_path = config.telegram_webhook_path
    expected_secret = config.telegram_webhook_secret
    if not expected_path or not expected_secret:
        logger.warning("telegram webhook hit while unconfigured")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")

    header = request.headers.get("x-telegram-bot-api-secret-token", "")
    # Compared as bytes: compare_digest rejects non-ASCII str outright, and the
    # path segment is whatever a prober chose to send. Both checks always run —
    # short-circuiting on the path would reveal which half was wrong by timing.
    path_ok = compare_digest(secret_path.encode(), expected_path.encode())
    secret_ok = compare_digest(header.encode(), expected_secret.encode())
    if not (path_ok and secret_ok):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")

    try:
        update = await request.json()
    except Exception:
        logger.info("telegram webhook: unparseable body, discarded")
        return {"ok": True}
    if not isinstance(update, dict):
        return {"ok": True}

    chat_id = inbound.chat_id_of(update)
    if not chat_id or not config.telegram_chat_id or chat_id != config.telegram_chat_id:
        logger.info("telegram webhook: update from chat %s discarded", chat_id)
        return {"ok": True}

    try:
        await inbound.handle_update(session, update, notifier=notifier)
    except Exception:
        # Anything that escapes here would be a 500, and Telegram retries a 500
        # for hours — each retry another model call on the same message. The text
        # itself is already committed to the lake, so the re-parse sweep picks up
        # whatever this run failed to finish. The rollback is by hand because
        # ``get_session`` commits on a clean return, and committing a transaction
        # that has already failed raises on the way out.
        logger.exception("telegram update failed; acknowledged anyway")
        await session.rollback()
    return {"ok": True}
