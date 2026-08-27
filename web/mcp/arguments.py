"""Pure argument parsing shared by Vitals MCP tool adapters."""

from __future__ import annotations

from datetime import date as date_type
from datetime import time as time_type
from typing import Optional


class McpArgumentError(ValueError):
    """A reviewed argument error whose message contains no supplied value."""


def _parse_date(value: Optional[str], default=None, *, field: str):
    """Parse a ``YYYY-MM-DD`` argument, or return ``default`` when omitted."""

    if value is None:
        return default
    try:
        return date_type.fromisoformat(value)
    except (ValueError, TypeError):
        raise McpArgumentError(f"{field} must be a YYYY-MM-DD date") from None


def _parse_time(value: Optional[str], *, field: str):
    """Parse an ``HH:MM`` argument, or return ``None`` when omitted."""

    if value is None:
        return None
    try:
        return time_type.fromisoformat(value)
    except (ValueError, TypeError):
        raise McpArgumentError(f"{field} must be an HH:MM time") from None
