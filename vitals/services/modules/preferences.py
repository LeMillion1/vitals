"""Subject-scoped module preferences and read-through cache.

Storage is one ``subject_settings`` row per health subject. The reviewed scoped
store retains an exact-one-subject fallback to the legacy ``app_settings`` key.
Redis is UUID-namespaced and the database remains the source of truth.

Manifest — **Zero Silent Errors**: every fallback path is *logged*, never
swallowed. ``get_enabled_modules`` NEVER raises: on a broken/empty/corrupt config
it returns the safe default (Core → True, Optional → False) so the UI degrades to
"core only" instead of 500-ing.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Optional

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.services.settings.contracts import ScopedSettingKey, SettingScope
from vitals.services.settings.scoped_store import (
    get_scoped_setting,
    update_scoped_setting,
)

from .registry import CORE_KEYS, DEFAULT_STATE, MODULE_REGISTRY, OPTIONAL_KEYS

logger = logging.getLogger(__name__)

# The legacy app_settings key the scoped read still falls back to.
SETTINGS_KEY = "enabled_modules"
# Version the key because releases before v2 could populate a subject cache
# from an unbound PostgreSQL session.  FORCE RLS made that look like an empty
# setting, so the cache held the safe defaults even when the scoped row said
# otherwise.  Old entries must not survive the read-boundary fix.
REDIS_KEY = "settings:enabled_modules:v2"    # cache key
REDIS_TTL = 300                           # seconds


def cache_key(subject_id: uuid.UUID) -> str:
    """Return the UUID-namespaced cache key.

    One cache entry per person: a shared key would serve one subject's module
    state to the next request from another.
    """

    return f"{REDIS_KEY}:{subject_id}"


class ModuleToggleError(ValueError):
    """Raised when a caller tries to toggle a non-existent or Core (locked)
    module. The router maps this to HTTP 400."""


def _sanitize(raw: Any) -> dict[str, bool]:
    """Project arbitrary stored data onto the registry.

    Core keys are forced True; Optional keys take ``bool(raw[key])`` when present
    else their default (False); unknown keys are dropped. Resilient to schema
    drift and to a non-dict ``raw`` (returns clean defaults).
    """
    state = dict(DEFAULT_STATE)
    if isinstance(raw, dict):
        for key in OPTIONAL_KEYS:
            if key in raw:
                state[key] = bool(raw[key])
    for key in CORE_KEYS:
        state[key] = True
    return state


async def get_enabled_modules(
    session: AsyncSession,
    redis: Optional[Redis] = None,
    *,
    subject_id: uuid.UUID,
) -> dict[str, bool]:
    """Resolve one subject's enabled-module map. Never raises — falls back to
    safe defaults.

    Order: Redis cache → their scoped setting → ``DEFAULT_STATE``. The scoped
    read still falls back to the legacy ``app_settings`` row on its own, so a
    pre-backfill installation keeps its modules without a bridge here.
    """
    # 1) Redis read-through cache.
    cached = await get_cached_enabled_modules(redis, subject_id=subject_id)
    if cached is not None:
        return cached

    # 2) Database (source of truth).
    try:
        raw = await get_scoped_setting(
            session,
            scope=SettingScope.SUBJECT,
            key=ScopedSettingKey.ENABLED_MODULES,
            scope_id=subject_id,
            default=dict(DEFAULT_STATE),
        )
        if isinstance(raw, dict):
            state = _sanitize(raw)
            if _session_can_populate_subject_cache(session, subject_id=subject_id):
                await prime_cache(redis, state, subject_id=subject_id)
            return state
        logger.warning(
            "modules: subject setting is not an object (%s); using defaults",
            type(raw).__name__,
        )
        return dict(DEFAULT_STATE)
    except Exception:
        logger.warning(
            "modules: DB read failed; using safe defaults", exc_info=True
        )

    # 3) Safe fallback.
    return dict(DEFAULT_STATE)


def _session_can_populate_subject_cache(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
) -> bool:
    """Whether this session could have observed the subject's protected row.

    SQLite has no row security, so its ordinary application-scoped reads are
    authoritative.  On PostgreSQL, only the exact subject binding (or an
    explicit platform worker scope) may turn a DB result into shared cache
    state.  An unbound runtime session sees no ``subject_settings`` rows under
    FORCE RLS; caching that absence would hide the durable preference.
    """

    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        return True

    from vitals.persistence.rls import bound_subject, in_platform_scope

    return bound_subject(session) == subject_id or in_platform_scope(session)


async def get_cached_enabled_modules(
    redis: Optional[Redis],
    *,
    subject_id: uuid.UUID,
) -> dict[str, bool] | None:
    """Return a validated subject cache entry, or ``None`` on miss/failure."""

    if redis is None:
        return None
    try:
        cached = await redis.get(cache_key(subject_id))
        if not cached:
            return None
        parsed = json.loads(cached)
        if not isinstance(parsed, dict):
            logger.warning(
                "modules: Redis value is not an object (%s); falling through to DB",
                type(parsed).__name__,
            )
            return None
        return _sanitize(parsed)
    except Exception:
        logger.warning(
            "modules: Redis read failed; falling through to DB", exc_info=True
        )
        return None


async def set_module_enabled(
    session: AsyncSession,
    *,
    key: str,
    enabled: bool,
    subject_id: uuid.UUID,
) -> dict[str, bool]:
    """Toggle an Optional module. Flushes (caller commits). Returns the new state.

    Raises ``ModuleToggleError`` for unknown keys or Core (locked) modules.
    """
    if key not in MODULE_REGISTRY:
        raise ModuleToggleError(f"unknown module '{key}'")
    if key in CORE_KEYS:
        raise ModuleToggleError(f"module '{key}' is core and cannot be disabled")

    def _toggle(raw: Any) -> dict[str, bool]:
        return {**_sanitize(raw), key: bool(enabled)}

    updated = await update_scoped_setting(
        session,
        scope=SettingScope.SUBJECT,
        key=ScopedSettingKey.ENABLED_MODULES,
        scope_id=subject_id,
        default=dict(DEFAULT_STATE),
        update=_toggle,
    )
    return _sanitize(updated)


async def prime_cache(
    redis: Optional[Redis],
    state: dict[str, bool],
    *,
    subject_id: uuid.UUID,
) -> None:
    """Write-through the resolved state into Redis. Best-effort (logged on fail)."""
    if redis is None:
        return
    try:
        await redis.set(
            cache_key(subject_id),
            json.dumps(_sanitize(state)),
            ex=REDIS_TTL,
        )
    except Exception:
        logger.warning("modules: Redis prime failed", exc_info=True)


__all__ = [
    "ModuleToggleError",
    "REDIS_KEY",
    "REDIS_TTL",
    "SETTINGS_KEY",
    "cache_key",
    "get_cached_enabled_modules",
    "get_enabled_modules",
    "prime_cache",
    "set_module_enabled",
]
