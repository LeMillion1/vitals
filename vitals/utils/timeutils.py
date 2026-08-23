"""Local-time helpers — the ONLY sanctioned source of "now"/"today".

Ported in spirit from Boxly's ``bot/utils/timeutils``: business logic must read
the wall clock through these helpers, never ``datetime.now()``/``utcnow()``
directly, so a container running in UTC can't skew "today" / date windows.

The timezone comes from ``VITALS_TIMEZONE`` (default Europe/Chisinau) and is
resolved lazily and cached. Health-calendar helpers return **naive** values
(tzinfo stripped) on purpose so they stay directly comparable with the naive
``Date``/``DateTime`` columns used across the schema. ``now_utc()`` is the
explicit aware exception for external-provider billing boundaries. The
container also pins ``TZ`` as defence in depth.

``set_timezone()`` exists for tests (``freezegun`` covers the clock; this covers
the zone) — production reads the env once.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime, timezone
from typing import Iterator, Optional
from zoneinfo import ZoneInfo

DEFAULT_TIMEZONE = "Europe/Chisinau"

_tz_name: str | None = None
_tz: ZoneInfo | None = None

#: The zone of the person the current work is about, when there is one.
#:
#: ``VITALS_TIMEZONE`` is the installation's, which was the same thing while an
#: installation was one person. It is not any more: ``health_subjects.timezone``
#: has always been the real answer, and a scheduled job fanned out over ten
#: people was closing every one of their days on the operator's clock. For
#: ``nutrition_day_end`` that is the whole point of the job — it exists to run
#: once the day's totals are final — and for somebody four hours east it was
#: finalising a day still in progress.
_subject_zone: ContextVar[ZoneInfo | None] = ContextVar(
    "vitals_subject_zone", default=None
)


def _resolved_zone(name: str | None) -> ZoneInfo | None:
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except Exception:
        # A subject row with an unusable zone must not take the work down; the
        # installation's zone is a worse answer than theirs and a much better
        # one than no answer at all.
        return None


def set_subject_timezone(name: str | None) -> None:
    """Bind a subject's zone for the rest of the current task.

    For a web request, where the task ends with the response and there is
    nothing to restore — the same shape as ``current_lang``. Work that has to
    hand the zone back, such as a scheduler fanning one job over ten people,
    wants :func:`subject_timezone` instead.
    """

    _subject_zone.set(_resolved_zone(name))


@contextmanager
def subject_timezone(name: str | None) -> Iterator[None]:
    """Read the wall clock as one subject sees it, for the length of a block.

    A context variable rather than a parameter because "the local day" is read
    deep inside services that have no business taking a timezone argument — it
    is ambient context, and it already was. What changes is how narrow the
    ambient is: the process, or the person the work is about.

    ``None`` leaves the installation's zone in place, which is right for work
    that is not about anybody in particular.
    """

    zone = _resolved_zone(name)
    if zone is None:
        yield
        return
    token = _subject_zone.set(zone)
    try:
        yield
    finally:
        _subject_zone.reset(token)


def _zone() -> ZoneInfo:
    global _tz_name, _tz
    subject = _subject_zone.get()
    if subject is not None:
        return subject
    configured = os.getenv("VITALS_TIMEZONE", DEFAULT_TIMEZONE) or DEFAULT_TIMEZONE
    if _tz is None or configured != _tz_name:
        _tz_name = configured
        _tz = ZoneInfo(configured)
    return _tz


def set_timezone(name: str) -> None:
    """Override the active zone (tests). Clears the cache so the next call to
    :func:`now_local` reflects it."""
    global _tz_name, _tz
    os.environ["VITALS_TIMEZONE"] = name
    _tz_name = None
    _tz = None


def now_local() -> datetime:
    """Current local wall-clock time as a naive datetime."""
    return datetime.now(_zone()).replace(tzinfo=None)


def now_utc() -> datetime:
    """Current aware UTC time for provider/billing boundaries.

    Health-day semantics continue to use :func:`now_local`; this helper exists
    for external-provider periods that are explicitly defined in UTC.
    """

    return datetime.now(timezone.utc)


def today_local() -> date:
    """Current local calendar date."""
    return now_local().date()


def to_local_naive(dt: Optional[datetime]) -> Optional[datetime]:
    """Convert a datetime to a **naive local** datetime (matching the schema's
    naive columns). A tz-aware value is converted into the configured zone; a
    naive value is assumed to already be UTC (how Garmin/Hevy timestamps arrive).
    ``None`` passes through."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_zone()).replace(tzinfo=None)
