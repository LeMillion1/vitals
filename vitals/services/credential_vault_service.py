"""Encrypt one subject's provider credential, and refuse rather than guess.

The first encrypted-at-rest store in this codebase, which is worth saying out
loud: everything else here is either one-way (bcrypt, for passwords that only
ever need comparing) or plaintext in ``.env`` (the OpenRouter key, the MCP
client secret — installation-wide values for an installation-wide account). A
Garmin password is neither. It has to come back out to be used, and it belongs
to a person rather than to the installation, so ``.env`` cannot hold it once
there are two people.

**Fernet, over anything hand-rolled.** It is AES-128-CBC with an HMAC over the
ciphertext, from ``cryptography``, which is already in the dependency tree. The
authentication is the part that matters here: a ciphertext somebody has edited
fails to decrypt rather than decrypting to something else, and "the stored
credential is not what we wrote" is a state worth failing on rather than
handing to a login form.

**The key is the installation's.** ``VITALS_CREDENTIAL_KEY`` — a urlsafe base64
32-byte value, which is exactly what ``Fernet.generate_key()`` prints. It is
correctly a ``.env`` value: it belongs to the deployment, not to a patient. Two
consequences, both stated rather than left to be found out:

* **No key, no vault.** Storing raises rather than falling back to plaintext,
  and reading answers "no credential" rather than raising, because a reader's
  honest answer to an unreadable store is that this connection is not
  configured — which every caller already handles.
* **Losing the key costs every stored credential and no health data.** Nothing
  in the lake is encrypted with it. Recovery is somebody re-entering their
  provider password, which is a form, not a restore.

**What is stored.** A JSON object of short strings, and nothing outside it. No
column holds an email, an account id, or a key suffix — the connection's
``external_account_discriminator`` stays opaque for the same reason, and a
readable identifier beside an encrypted secret tells an attacker which rows are
worth attacking.
"""
from __future__ import annotations

import json
import os
import uuid
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.models.credentials import IntegrationCredential

#: The environment variable holding the installation's encryption key.
CREDENTIAL_KEY_ENV = "VITALS_CREDENTIAL_KEY"

#: What ``credential_ref`` says on a connection whose secret lives here.
VAULT_CREDENTIAL_REF = "vault:v1"

#: One key so far. The column exists so a second one is a migration that can
#: read the old rows, rather than a flag day that invalidates them.
CURRENT_KEY_VERSION = 1

#: A credential is a handful of short strings. The cap is not about storage —
#: it is about a caller that hands this a whole session object by mistake.
_MAX_PLAINTEXT_BYTES = 8192


class CredentialVaultError(Exception):
    """Base class for a fail-closed credential vault error."""


class CredentialVaultUnavailable(CredentialVaultError):
    """No usable installation key, so nothing can be encrypted."""


class CredentialVaultValidationError(CredentialVaultError):
    """A caller passed something that is not a credential."""


class CredentialVaultCorrupt(CredentialVaultError):
    """A stored row did not decrypt, or did not decode to an object.

    Deliberately distinct from "there is no credential". A missing row means
    this connection is not configured, which is ordinary. A row that fails its
    authentication tag means the stored bytes are not the ones written, and
    that is worth failing on rather than treating as absence — absence would
    quietly re-prompt somebody whose credential is still there and unreadable
    because the key changed.
    """


def _fernet(key: str | None = None):
    from cryptography.fernet import Fernet, InvalidToken  # noqa: F401

    raw = key if key is not None else os.getenv(CREDENTIAL_KEY_ENV, "")
    raw = (raw or "").strip()
    if not raw:
        raise CredentialVaultUnavailable(
            f"{CREDENTIAL_KEY_ENV} is not set, so provider credentials cannot "
            "be stored. Generate one with "
            "`python -c \"from cryptography.fernet import Fernet; "
            'print(Fernet.generate_key().decode())"`.'
        )
    try:
        return Fernet(raw.encode("ascii"))
    except (ValueError, TypeError) as exc:
        raise CredentialVaultUnavailable(
            f"{CREDENTIAL_KEY_ENV} is not a valid Fernet key"
        ) from exc


def is_available() -> bool:
    """Whether this installation can store a credential at all.

    Asked by the settings page so it can say so on the card, rather than
    accepting a password and failing on save.
    """

    try:
        _fernet()
    except CredentialVaultUnavailable:
        return False
    return True


def _require_uuid(value: Any, *, field: str) -> uuid.UUID:
    if not isinstance(value, uuid.UUID) or value.int == 0:
        raise CredentialVaultValidationError(f"{field} must be a non-zero UUID")
    return value


def _encode(secret: Mapping[str, Any]) -> bytes:
    if not isinstance(secret, Mapping) or not secret:
        raise CredentialVaultValidationError(
            "a credential must be a non-empty mapping"
        )
    for key, value in secret.items():
        if not isinstance(key, str) or not key:
            raise CredentialVaultValidationError("credential keys must be strings")
        if not isinstance(value, str):
            raise CredentialVaultValidationError(
                f"credential field {key!r} must be a string"
            )
    payload = json.dumps(dict(secret), separators=(",", ":")).encode("utf-8")
    if len(payload) > _MAX_PLAINTEXT_BYTES:
        raise CredentialVaultValidationError("credential is implausibly large")
    return payload


async def store(
    session: AsyncSession,
    *,
    integration_connection_id: uuid.UUID,
    subject_id: uuid.UUID,
    secret: Mapping[str, Any],
) -> None:
    """Replace this connection's credential. Never commits.

    The subject is required and is not looked up from the connection on
    purpose: the composite foreign key checks the two agree, so a caller that
    names the wrong one is refused by the database rather than silently writing
    a credential against somebody else's connection.
    """

    integration_connection_id = _require_uuid(
        integration_connection_id, field="integration_connection_id"
    )
    subject_id = _require_uuid(subject_id, field="subject_id")
    ciphertext = _fernet().encrypt(_encode(secret))

    row = await session.scalar(
        select(IntegrationCredential)
        .where(
            IntegrationCredential.integration_connection_id
            == integration_connection_id
        )
        .with_for_update()
    )
    if row is None:
        session.add(
            IntegrationCredential(
                integration_connection_id=integration_connection_id,
                subject_id=subject_id,
                key_version=CURRENT_KEY_VERSION,
                ciphertext=ciphertext,
            )
        )
    else:
        if row.subject_id != subject_id:
            raise CredentialVaultValidationError(
                "this connection's credential belongs to another subject"
            )
        row.key_version = CURRENT_KEY_VERSION
        row.ciphertext = ciphertext
    await session.flush()


async def load(
    session: AsyncSession,
    *,
    integration_connection_id: uuid.UUID,
) -> dict[str, str] | None:
    """This connection's credential, or ``None`` if it has none.

    ``None`` for a missing row and for an installation with no key: both mean
    "nothing usable here", which callers already read as not configured.
    A row that exists and will not decrypt raises, because that is a different
    fact and one worth an operator seeing.
    """

    integration_connection_id = _require_uuid(
        integration_connection_id, field="integration_connection_id"
    )
    with session.no_autoflush:
        row = await session.scalar(
            select(IntegrationCredential).where(
                IntegrationCredential.integration_connection_id
                == integration_connection_id
            )
        )
    if row is None:
        return None
    try:
        cipher = _fernet()
    except CredentialVaultUnavailable:
        return None

    from cryptography.fernet import InvalidToken

    try:
        payload = cipher.decrypt(bytes(row.ciphertext))
    except InvalidToken as exc:
        raise CredentialVaultCorrupt(
            "a stored provider credential did not decrypt with the current "
            f"{CREDENTIAL_KEY_ENV}"
        ) from exc
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CredentialVaultCorrupt(
            "a stored provider credential did not decode as an object"
        ) from exc
    if not isinstance(decoded, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in decoded.items()
    ):
        raise CredentialVaultCorrupt(
            "a stored provider credential is not an object of strings"
        )
    return decoded


async def clear(
    session: AsyncSession,
    *,
    integration_connection_id: uuid.UUID,
) -> bool:
    """Forget this connection's credential. Never commits.

    Returns whether there was one, so a caller can tell "disconnected" from
    "was never connected" without reading the row twice.
    """

    integration_connection_id = _require_uuid(
        integration_connection_id, field="integration_connection_id"
    )
    row = await session.scalar(
        select(IntegrationCredential)
        .where(
            IntegrationCredential.integration_connection_id
            == integration_connection_id
        )
        .with_for_update()
    )
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    return True


__all__ = [
    "CREDENTIAL_KEY_ENV",
    "CURRENT_KEY_VERSION",
    "CredentialVaultCorrupt",
    "CredentialVaultError",
    "CredentialVaultUnavailable",
    "CredentialVaultValidationError",
    "VAULT_CREDENTIAL_REF",
    "clear",
    "is_available",
    "load",
    "store",
]
