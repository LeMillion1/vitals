"""UI language preference — stored in ``app_settings``, cached in Redis.

Mirrors the ``modules_service`` pattern exactly: DB is source of truth, Redis is a
read-through cache with 300 s TTL.  Supported codes: ``"en"`` (default), ``"ru"``.
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.models.app_settings import AppSetting
from vitals.services.scoped_settings_service import (
    ScopedSettingKey,
    SettingScope,
    get_scoped_setting,
    set_scoped_setting,
)

logger = logging.getLogger(__name__)

SETTINGS_KEY = "ui_language"
REDIS_KEY = "settings:ui_language"
REDIS_TTL = 300
SUPPORTED = ("en", "ru")
DEFAULT = "en"


def cache_key(user_id: uuid.UUID | None = None) -> str:
    """Return the legacy or UUID-namespaced cache key."""

    return REDIS_KEY if user_id is None else f"{REDIS_KEY}:{user_id}"


def _sanitize(raw: object) -> str:
    if isinstance(raw, str) and raw in SUPPORTED:
        return raw
    return DEFAULT


async def get_language(
    session: AsyncSession,
    redis: Optional[Redis] = None,
    *,
    user_id: uuid.UUID | None = None,
) -> str:
    redis_key = cache_key(user_id)
    if redis is not None:
        try:
            cached = await redis.get(redis_key)
            if cached:
                return _sanitize(cached)
        except Exception:
            logger.warning("language: Redis read failed; falling through to DB", exc_info=True)

    try:
        if user_id is not None:
            raw = await get_scoped_setting(
                session,
                scope=SettingScope.USER,
                key=ScopedSettingKey.UI_LANGUAGE,
                scope_id=user_id,
                default=DEFAULT,
            )
            lang = _sanitize(raw)
            await prime_cache(redis, lang, user_id=user_id)
            return lang
        row = await session.get(AppSetting, SETTINGS_KEY)
        if row is not None:
            lang = _sanitize(row.value)
            await prime_cache(redis, lang)
            return lang
        logger.debug("language: no app_settings row; using default '%s'", DEFAULT)
    except Exception:
        logger.warning("language: DB read failed; using default", exc_info=True)

    return DEFAULT


async def set_language(
    session: AsyncSession,
    lang: str,
    redis: Optional[Redis] = None,
    *,
    user_id: uuid.UUID | None = None,
) -> str:
    lang = _sanitize(lang)
    if user_id is not None:
        await set_scoped_setting(
            session,
            scope=SettingScope.USER,
            key=ScopedSettingKey.UI_LANGUAGE,
            scope_id=user_id,
            value=lang,
        )
    else:
        row = await session.get(AppSetting, SETTINGS_KEY)
        if row is None:
            session.add(AppSetting(key=SETTINGS_KEY, value=lang))
        else:
            row.value = lang
        await session.flush()
    await prime_cache(redis, lang, user_id=user_id)
    return lang


async def prime_cache(
    redis: Optional[Redis],
    lang: str,
    *,
    user_id: uuid.UUID | None = None,
) -> None:
    if redis is None:
        return
    try:
        await redis.set(cache_key(user_id), lang, ex=REDIS_TTL)
    except Exception:
        logger.warning("language: Redis prime failed", exc_info=True)
