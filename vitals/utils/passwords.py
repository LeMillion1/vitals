"""Password hashing and verification (bcrypt).

Lives in the core because both the web layer (login, shared-report access) and
``share_service`` need it — the core must never import ``web``. No FastAPI
imports here, so it is trivially unit-testable.
"""
from __future__ import annotations

import os

import bcrypt

# bcrypt hashes at most the first 72 bytes; truncate explicitly so hashing and
# verification always agree on the same input.
_BCRYPT_MAX_BYTES = 72
_BCRYPT_ROUNDS = 4 if os.getenv("VITALS_TESTING") == "1" else 12


def _encode(password: str) -> bytes:
    return password.encode("utf-8")[:_BCRYPT_MAX_BYTES]


def hash_password(password: str, *, minimum_rounds: int | None = None) -> str:
    """Hash a password without lowering an explicitly requested bcrypt cost.

    ``minimum_rounds`` is used by the legacy credential bridge when an existing
    deployment already has a stronger hash than this release's default. New
    callers otherwise keep the environment-appropriate default above.
    """

    if minimum_rounds is not None:
        if (
            isinstance(minimum_rounds, bool)
            or not isinstance(minimum_rounds, int)
            or not 4 <= minimum_rounds <= 31
        ):
            raise ValueError("minimum bcrypt rounds must be an integer from 4 to 31")
        rounds = max(_BCRYPT_ROUNDS, minimum_rounds)
    else:
        rounds = _BCRYPT_ROUNDS
    return bcrypt.hashpw(_encode(password), bcrypt.gensalt(rounds=rounds)).decode(
        "utf-8"
    )


def verify_password(password: str, hashed: str | None) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(_encode(password), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# Throwaway hash to equalize login timing when the username doesn't match: we
# still run one bcrypt verification so "wrong user" and "wrong password" take the
# same wall-clock time (no username-enumeration timing oracle).
_DUMMY_HASH = bcrypt.hashpw(b"timing-equalizer", bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)).decode("utf-8")


def verify_password_dummy(password: str) -> bool:
    verify_password(password, _DUMMY_HASH)
    return False
