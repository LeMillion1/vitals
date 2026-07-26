"""Minimal Redis fixed-window rate limiter (dependency factory).

Single-user app, so this guards against a runaway client / accidental loop rather
than abuse: an INCR-with-EXPIRE counter per (bucket, user) window.

**Fail-open**: if Redis is unreachable the request is allowed (and the failure is
logged) — a rate limiter must never take the app down. The over-limit case raises
HTTP 429, which FastAPI renders as JSON.
"""
from __future__ import annotations

import logging
from typing import Callable

from fastapi import Depends, HTTPException, Request, status

from vitals.i18n import t
from redis.asyncio import Redis

from web.deps import get_redis, require_auth

logger = logging.getLogger(__name__)


def rate_limit(bucket: str, *, limit: int, window: int) -> Callable:
    """Build a dependency enforcing ``limit`` requests per ``window`` seconds.

    Keyed by the authenticated username (so it composes with ``require_auth``).
    """

    async def _dep(
        redis: Redis = Depends(get_redis),
        username: str = Depends(require_auth),
    ) -> None:
        key = f"ratelimit:{bucket}:{username}"
        try:
            count = await redis.incr(key)
            if count == 1:
                await redis.expire(key, window)
        except Exception:
            logger.warning(
                "rate-limit backend unavailable for %s; allowing request",
                bucket,
                exc_info=True,
            )
            return
        if count > limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=t("common.rate_limit"),
            )

    return _dep


def login_rate_limit(*, limit: int, window: int, bucket: str = "login") -> Callable:
    """Build a dependency throttling repeated requests **by client IP**.

    Unlike :func:`rate_limit`, this must NOT depend on ``require_auth`` — the whole
    point is to guard endpoints where there is no username yet: ``/login``, where
    password-guessing would otherwise be completely unbounded, and the Telegram
    webhook, which authenticates with its own secret. ``bucket`` keeps those
    counters apart so webhook traffic can't exhaust the login allowance. Keyed by
    the caller's IP. Fail-open like ``rate_limit`` — a missing Redis must never
    lock the owner out of their own app.
    """

    async def _dep(
        request: Request,
        redis: Redis = Depends(get_redis),
    ) -> None:
        ip = request.client.host if request.client else "unknown"
        key = f"ratelimit:{bucket}:{ip}"
        try:
            count = await redis.incr(key)
            if count == 1:
                await redis.expire(key, window)
        except Exception:
            logger.warning(
                "login rate-limit backend unavailable; allowing request",
                exc_info=True,
            )
            return
        if count > limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=t("common.rate_limit"),
            )

    return _dep
