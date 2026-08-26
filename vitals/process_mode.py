"""Explicit process roles for the transitional split runtime.

Production still starts the historical combined FastAPI + scheduler process.
The other modes are opt-in until Compose is switched in a later change, which
keeps an existing single-user installation working while the two lifecycles are
made independently runnable and testable.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from enum import StrEnum


PROCESS_MODE_ENV = "VITALS_PROCESS_MODE"


class ProcessMode(StrEnum):
    COMBINED = "combined"
    WEB = "web"
    WORKER = "worker"


def load_process_mode(environ: Mapping[str, str] | None = None) -> ProcessMode:
    """Return the explicit process role, preserving ``combined`` as the default."""

    values = os.environ if environ is None else environ
    raw = (values.get(PROCESS_MODE_ENV) or ProcessMode.COMBINED.value).strip().lower()
    try:
        return ProcessMode(raw)
    except ValueError as exc:
        allowed = ", ".join(mode.value for mode in ProcessMode)
        raise RuntimeError(f"{PROCESS_MODE_ENV} must be one of: {allowed}") from exc


__all__ = ["PROCESS_MODE_ENV", "ProcessMode", "load_process_mode"]
