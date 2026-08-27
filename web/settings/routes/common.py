"""Shared presentation helpers for settings delivery routes."""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import TypeVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import status
from fastapi.responses import RedirectResponse

_T = TypeVar("_T", bound=Callable)


def compatibility_override(name: str, default: _T) -> _T:
    """Honor explicit seams historically patched on ``web.routers.settings``."""

    aggregate = sys.modules.get("web.routers.settings")
    if aggregate is None:
        return default
    return getattr(aggregate, name, default)


def blank_if_none(value) -> str:
    """Render an unset profile field as an empty input value."""

    if value is None:
        return ""
    return number(value)


def number(value) -> str:
    """Render integral floats without a redundant decimal suffix."""

    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def is_known_timezone(zone: str) -> bool:
    """Return whether ``zone`` names an installed IANA timezone."""

    try:
        ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return False
    return True


def redirect(suffix: str = "") -> RedirectResponse:
    return RedirectResponse(
        url=f"/settings{suffix}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
