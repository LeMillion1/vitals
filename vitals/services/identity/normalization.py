"""Canonical username and email representations for identity boundaries."""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from vitals.services.identity.contracts import IdentityValidationError

_MAX_USERNAME_LENGTH = 128
_MAX_EMAIL_LENGTH = 320


@dataclass(frozen=True, slots=True)
class NormalizedUsername:
    """Display spelling plus the unique, case-insensitive lookup key."""

    display: str
    lookup_key: str


@dataclass(frozen=True, slots=True)
class NormalizedEmail:
    """Display spelling plus the shallow mailbox comparison key.

    Email is never an identity key. This representation exists only for
    uniqueness and exact invitation-address matching after an identity provider
    has independently vouched for the claim.
    """

    display: str
    lookup_key: str


def normalize_username(raw: str) -> NormalizedUsername:
    """Return the one canonical username representation used by identity writes."""

    if not isinstance(raw, str):
        raise IdentityValidationError("username must be a string")
    display = unicodedata.normalize("NFKC", raw).strip()
    if not display:
        raise IdentityValidationError("username must not be blank")
    if any(unicodedata.category(char).startswith("C") for char in display):
        raise IdentityValidationError("username must not contain control characters")
    lookup_key = display.casefold()
    if len(display) > _MAX_USERNAME_LENGTH or len(lookup_key) > _MAX_USERNAME_LENGTH:
        raise IdentityValidationError("normalized username is too long")
    return NormalizedUsername(display=display, lookup_key=lookup_key)


def normalize_email(raw: str) -> NormalizedEmail:
    """Normalize an email claim without provider-specific rewriting."""

    if not isinstance(raw, str):
        raise IdentityValidationError("email must be a string")
    display = unicodedata.normalize("NFKC", raw).strip()
    if not display or "@" not in display:
        raise IdentityValidationError("email is not a usable address")
    if any(unicodedata.category(char).startswith("C") for char in display):
        raise IdentityValidationError("email must not contain control characters")
    lookup_key = display.casefold()
    if len(display) > _MAX_EMAIL_LENGTH or len(lookup_key) > _MAX_EMAIL_LENGTH:
        raise IdentityValidationError("normalized email is too long")
    return NormalizedEmail(display=display, lookup_key=lookup_key)


__all__ = ["NormalizedEmail", "NormalizedUsername", "normalize_email", "normalize_username"]
