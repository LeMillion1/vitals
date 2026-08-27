"""Shared, side-effect-free settings form primitives."""

SECRET_SENTINEL = "••••••••"


def is_secret_sentinel(value: str) -> bool:
    """Return whether a submitted secret is the masked keep-current marker."""

    return value.strip() == SECRET_SENTINEL


__all__ = ["SECRET_SENTINEL", "is_secret_sentinel"]
