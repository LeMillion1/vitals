"""Web compatibility wrapper for the neutral runtime-environment editor."""
from __future__ import annotations

# Keep ``os`` visible here: existing focused tests monkeypatch its low-level
# calls to prove the shared writer's fsync and failure semantics through this
# delivery-layer wrapper.
import os
from pathlib import Path

from vitals.runtime_env import read_env_key, write_env_keys


# Local development retains the repository .env fallback. Production always
# sets VITALS_ENV_FILE to the allowlisted file inside its directory bind.
_ENV_PATH = Path(__file__).parent.parent.parent / ".env"


def _find_env_path() -> Path:
    """Return the configured application env path."""

    override = os.getenv("VITALS_ENV_FILE")
    return Path(override) if override else _ENV_PATH


def read_key(key: str) -> str:
    """Return one persisted value, or an empty string when it is absent."""

    return read_env_key(_find_env_path(), key)


def write_keys(updates: dict[str, str]) -> None:
    """Atomically persist *updates* through the shared owner-only writer."""

    write_env_keys(_find_env_path(), updates)
