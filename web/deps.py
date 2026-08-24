"""FastAPI dependencies: DB session, Redis, and the single-user auth guard.

The session factory and Redis client reuse the core's tuned setup and are built
lazily so tests can override them via ``app.dependency_overrides`` without a real
DB/Redis.
"""
from __future__ import annotations

import logging
import os
import threading
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, AsyncIterator, Callable, Optional

from fastapi import Depends, HTTPException, Request, status
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from web.config import SESSION_COOKIE

from vitals.i18n import current_lang
from vitals.models.identity import HealthSubject, User

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# ── DB ───────────────────────────────────────────────────────────────────────
_session_factory: Optional[async_sessionmaker[AsyncSession]] = None
_db_lock = threading.Lock()


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        with _db_lock:
            if _session_factory is None:
                from vitals.config import load_config
                from vitals.database import create_session_factory

                _session_factory = create_session_factory(load_config())
    return _session_factory


async def get_session() -> AsyncIterator[AsyncSession]:
    async with get_session_factory()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# ── Redis ────────────────────────────────────────────────────────────────────
_redis: Optional[Redis] = None
_redis_lock = threading.Lock()


def get_redis_client() -> Redis:
    global _redis
    if _redis is None:
        with _redis_lock:
            if _redis is None:
                url = os.getenv("VITALS_REDIS_URL", "redis://vitals_redis:6379/0")
                _redis = Redis.from_url(url, decode_responses=True)
    return _redis


async def get_redis() -> Redis:
    return get_redis_client()


# ── Auth guard ───────────────────────────────────────────────────────────────
class NotAuthenticated(HTTPException):
    """401 that the app's exception handler turns into a /login redirect for
    HTML GETs (and leaves as JSON 401 for API calls)."""

    def __init__(self) -> None:
        super().__init__(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")


class RecentAuthenticationRequired(HTTPException):
    """A sensitive browser action needs a recent provider/password proof."""

    def __init__(self) -> None:
        super().__init__(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Recent authentication required",
        )


async def require_auth(
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> str:
    """Guard every protected route and revalidate federated sessions."""
    # Lazy import breaks the web.auth ↔ web.deps cycle: auth.py imports the login
    # rate-limiter (web.ratelimit → web.deps), so deps must not import auth at
    # module-load time.
    from web.auth import decode_session, session_issued_at

    token = request.cookies.get(SESSION_COOKIE)
    claims = decode_session(token)
    if claims is None:
        raise NotAuthenticated()

    if claims.user_id is not None:
        from datetime import datetime, timezone

        from vitals.services.session_service import SessionRejected, confirm_session

        authenticated_at = (
            datetime.fromtimestamp(claims.authenticated_at, tz=timezone.utc)
            if claims.authenticated_at is not None
            else None
        )
        try:
            live = await confirm_session(
                db,
                user_id=claims.user_id,
                session_version=claims.session_version or 0,
                authenticated_at=authenticated_at,
            )
        except (SessionRejected, ValueError, OSError, OverflowError):
            raise NotAuthenticated() from None
        request.state.live_session = live
        request.state.session_authenticated_at = live.authenticated_at
        return live.username

    request.state.session_authenticated_at = session_issued_at(token)
    return claims.username


async def require_recent_auth(
    request: Request,
    _username: str = Depends(require_auth),
) -> str:
    """Require authentication performed within the last fifteen minutes."""

    from datetime import datetime, timezone

    authenticated_at = getattr(request.state, "session_authenticated_at", None)
    if authenticated_at is None:
        raise RecentAuthenticationRequired()
    if authenticated_at.tzinfo is None:
        authenticated_at = authenticated_at.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - authenticated_at
    if age.total_seconds() > 900:
        raise RecentAuthenticationRequired()
    return _username


@dataclass(frozen=True, slots=True)
class ChromeScope:
    """Whose app this is, for the parts of the page that are always there.

    The nav rail, the language and the status card belong to the *signed-in
    account*, not to whatever record the page happens to be showing. A doctor
    reading a patient's notes still has their own modules switched on and their
    own language; the patient's settings are the patient's.

    Resolved from the principal rather than through
    ``resolve_legacy_ownership_context``. That resolver is deliberately
    fail-closed on "exactly one subject in the database", which is the right
    answer for a write path and the wrong one here: the chrome would start
    throwing on every request the moment a second person existed, and the page
    would render with defaults and an exception in the log.
    """

    user_id: uuid.UUID
    subject_id: uuid.UUID


async def get_request_chrome_scope(
    request: Request,
    session: AsyncSession,
) -> ChromeScope | None:
    """Resolve and memoize the signed-in account and the record it owns.

    ``None`` for an anonymous request, and for a signed-in account that owns no
    record — a professional who has never been a patient here is a real case,
    and their chrome is the default one rather than an error.
    """

    cache_marker = "_chrome_scope_resolved"
    if getattr(request.state, cache_marker, False):
        return getattr(request.state, "chrome_scope", None)

    scope: ChromeScope | None = None
    signed_in_user_id: uuid.UUID | None = None
    try:
        from web.auth import decode_session

        claims = decode_session(request.cookies.get(SESSION_COOKIE))
        if claims is not None:
            user_id = claims.user_id
            if user_id is None:
                from vitals.services.identity_service import normalize_username

                normalized = normalize_username(claims.username)
                user_id = await session.scalar(
                    select(User.id).where(
                        User.normalized_username == normalized.lookup_key
                    )
                )
            if user_id is not None:
                signed_in_user_id = user_id
                subject_id = await session.scalar(
                    select(HealthSubject.id).where(
                        HealthSubject.owner_user_id == user_id
                    )
                )
                if subject_id is not None:
                    scope = ChromeScope(user_id=user_id, subject_id=subject_id)
    except Exception:
        # The chrome must always render. A failure here means the default nav,
        # never a 500 on a page whose content resolved perfectly well.
        logger.exception("chrome scope resolution failed; using safe defaults")
        scope = None
        signed_in_user_id = None

    request.state.chrome_scope = scope
    # Whether this account keeps a health record of its own. Most doctors and
    # every trainer do not, and every personal section of the product is about
    # one — so the navigation has to know, or it offers a shelf of links that
    # each bounce straight back.
    request.state.has_own_record = scope is not None
    # Whether to offer the roster at all. Asked here because the nav is chrome
    # and must not raise; a link that answers an empty page is worse than no
    # link, and a professional who holds nobody has no roster to visit.
    #
    # Asked about the signed-in account rather than about ``scope``, which is
    # None for anybody who owns no record of their own. That is most doctors and
    # every trainer, so keying this off the scope hid the roster from precisely
    # the people who have one — they signed in and saw no way to reach their
    # patients at all.
    request.state.holds_patients = False
    if signed_in_user_id is not None:
        try:
            from vitals.enums import CareRelationshipStatus
            from vitals.models.professional import CareRelationship

            request.state.holds_patients = bool(
                await session.scalar(
                    select(CareRelationship.id)
                    .where(
                        CareRelationship.professional_user_id
                        == signed_in_user_id,
                        CareRelationship.status
                        != CareRelationshipStatus.ENDED.value,
                    )
                    .limit(1)
                )
            )
        except Exception:
            logger.exception("roster check failed; hiding the link")

    # Whether to offer the support console. Asked about the signed-in account
    # for the same reason the roster is, and it matters more here: a platform
    # superadmin usually keeps no record of their own, so ``/settings`` and
    # ``/more`` both refuse them — and the console's only link lived on
    # ``/settings``. The one account the console exists for could reach it by
    # typing the URL and no other way.
    request.state.is_platform_admin = False
    if signed_in_user_id is not None:
        try:
            from vitals.enums import UserRoleName
            from vitals.models.identity import UserRole

            request.state.is_platform_admin = bool(
                await session.scalar(
                    select(UserRole.id)
                    .where(
                        UserRole.user_id == signed_in_user_id,
                        UserRole.role == UserRoleName.PLATFORM_SUPERADMIN.value,
                    )
                    .limit(1)
                )
            )
        except Exception:
            logger.exception("platform-admin check failed; hiding the link")
    setattr(request.state, cache_marker, True)
    return scope


# ── Dashboard modules ──────────────────────────────────────────────────────────
class ModuleDisabled(HTTPException):
    """Raised when an Optional module's route is hit while the module is off.

    The app's exception handler turns this into a redirect to the dashboard for
    HTML GETs (and a JSON 404 for API calls) — a disabled module behaves as if it
    isn't there."""

    def __init__(self, key: str) -> None:
        super().__init__(status_code=status.HTTP_404_NOT_FOUND, detail=f"Module '{key}' is disabled")
        self.key = key


async def load_enabled_modules(
    request: Request,
    db: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> None:
    """Global dependency: resolve the enabled-module map once per request and stash
    it on ``request.state`` so every template (notably base.html nav) can read it
    without each router passing it through context.

    Fail-safe: any error yields the safe defaults — the chrome must always render.
    """
    from vitals.services import modules_service

    try:
        # The module map is one person's; an anonymous request has no subject
        # to read it for and keeps the safe defaults.
        scope = await get_request_chrome_scope(request, db)
        if scope is None:
            request.state.enabled_modules = dict(modules_service.DEFAULT_STATE)
            return
        request.state.enabled_modules = await modules_service.get_enabled_modules(
            db,
            redis,
            subject_id=scope.subject_id,
        )
    except Exception:
        logger.exception("module-state load failed; using safe defaults")
        request.state.enabled_modules = dict(modules_service.DEFAULT_STATE)


async def load_nav_status(
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> None:
    """Global dependency: today's readout for the nav rail's status card (and the
    phone's "More" screen), stashed on ``request.state``.

    Only for document requests: the rail is chrome, so an MCP call or a JSON API
    read would pay four pointless queries for markup it never renders.

    "Is this a document request" is decided by ruling API clients OUT, not by
    requiring ``text/html`` in. **A boosted navigation sends no Accept header at
    all** — htmx leaves XHR's default alone — so requiring ``text/html`` skipped
    the reads on exactly the requests that re-render the whole rail, and the card
    blinked out of existence on every click and back on every reload. A missing
    or wildcard Accept now counts as a document; only a client that explicitly
    asks for something else (``application/json``, ``text/event-stream``) is
    skipped.

    Fail-safe: any error yields an empty list — the card just doesn't draw.
    """
    request.state.nav_status = []
    accept = request.headers.get("accept", "")
    if request.method != "GET":
        return
    if accept and "text/html" not in accept and "*/*" not in accept:
        return
    from vitals.services import nav_status_service

    try:
        # The card is the signed-in account's own day, so it needs their subject.
        # Resolving it is inside the guard because chrome must never raise: an
        # account that owns no record simply draws no card.
        scope = await get_request_chrome_scope(request, db)
        if scope is None:
            return
        request.state.nav_status = await nav_status_service.rail_stats(
            db,
            getattr(request.state, "enabled_modules", None),
            subject_id=scope.subject_id,
        )
    except Exception:
        logger.exception("nav status load failed; hiding the status card")


async def load_support_banner(
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> None:
    """Global dependency: whether somebody from support can open this record.

    A grant is time-limited, scoped and approved, and none of that is worth much
    if the person whose record it is has to go looking for a settings page to
    find out it is live. The roadmap calls for a persistent banner and this is
    it: on every page, for as long as the access lasts, and gone by itself when
    it lapses.

    Chrome, so it is document-requests-only and fail-safe by exactly the rules
    ``load_nav_status`` documents above — an account with no record of its own
    simply draws no banner, and any error draws none rather than raising.
    """

    request.state.support_banner = None
    accept = request.headers.get("accept", "")
    if request.method != "GET":
        return
    if accept and "text/html" not in accept and "*/*" not in accept:
        return
    try:
        scope = await get_request_chrome_scope(request, db)
        if scope is None:
            return
        from vitals.services import support_access_service
        from vitals.services.access_resolution import resolve_access_context

        context = await resolve_access_context(
            db, user_id=scope.user_id, subject_id=scope.subject_id
        )
        grant = await support_access_service.live_grant_for(db, context=context)
        if grant is not None:
            # Only the two facts the banner shows. A live ORM row on
            # ``request.state`` is one a template could lazy-load from.
            request.state.support_banner = {
                "grant_id": str(grant.id),
                "expires_at": grant.expires_at,
            }
    except Exception:
        logger.exception("support banner load failed; hiding the banner")


async def load_language(
    request: Request,
    db: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> None:
    """Global dependency: resolve the UI language once per request and stash
    it on ``request.state`` + the ``ContextVar`` so both templates and deep
    service code (like ``raise_alert``) can read it without extra arguments.

    Fail-safe: any error yields ``"en"`` — the UI must always render.
    """
    from vitals.services import language_service

    try:
        scope = await get_request_chrome_scope(request, db)
        lang = await language_service.get_language(
            db,
            redis,
            user_id=(scope.user_id if scope is not None else None),
        )
    except Exception:
        logger.exception("language load failed; defaulting to 'en'")
        lang = "en"
    current_lang.set(lang)
    request.state.lang = lang


async def load_subject_timezone(
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> None:
    """Global dependency: read the wall clock as the signed-in person sees it.

    ``VITALS_TIMEZONE`` is the installation's zone, which was also the reader's
    while an installation was one person. It is not any more, and
    ``health_subjects.timezone`` has held the real answer the whole time with
    nothing reading it — so a patient abroad saw the server's "today" on their
    own dashboard, and logged a weigh-in against the wrong date.

    Nothing to restore: the context variable belongs to the task, and the task
    ends with the response. Fail-safe, like every other chrome dependency — an
    error leaves the installation's zone in place rather than failing the page.
    """

    from vitals.utils.timeutils import set_subject_timezone

    try:
        scope = await get_request_chrome_scope(request, db)
        if scope is None:
            return
        zone = await db.scalar(
            select(HealthSubject.timezone).where(
                HealthSubject.id == scope.subject_id
            )
        )
        set_subject_timezone(zone)
    except Exception:
        logger.exception("subject timezone load failed; using the installation's")




def require_module(key: str) -> Callable:
    """Build a dependency that 404s (→ redirect) when module ``key`` is disabled.

    Relies on ``load_enabled_modules`` having populated ``request.state`` first
    (it runs as a global dependency, before route-level ones)."""

    async def _dep(request: Request) -> None:
        enabled = getattr(request.state, "enabled_modules", None) or {}
        if not enabled.get(key, False):
            raise ModuleDisabled(key)

    return _dep
