"""Strict wire contract for encrypted portability-v2 archives."""

from __future__ import annotations

import base64
import binascii
import json
import struct
from dataclasses import dataclass
from typing import Any

MAGIC = b"VITALSP2"
FORMAT_MAJOR = 2
FORMAT_MINOR = 0
TAG_BYTES = 16
NONCE_BYTES = 12
SALT_BYTES = 32
KEY_BYTES = 32

# One reviewed Argon2id profile.  An archive may not choose its own cost: doing
# so would let an unauthenticated header turn import into a memory/CPU DoS.
KDF_NAME = "argon2id"
ARGON2_ITERATIONS = 3
ARGON2_LANES = 4
ARGON2_MEMORY_COST_KIB = 64 * 1024

MAX_HEADER_BYTES = 4096
MAX_PLAINTEXT_BYTES = 2 * 1024 * 1024 * 1024
MAX_ENCRYPTED_BYTES = MAX_PLAINTEXT_BYTES + len(MAGIC) + 4 + MAX_HEADER_BYTES + TAG_BYTES

_HEADER_KEYS = frozenset(
    {
        "format_major",
        "format_minor",
        "kdf",
        "kdf_iterations",
        "kdf_lanes",
        "kdf_memory_cost_kib",
        "kdf_output_bytes",
        "nonce",
        "salt",
    }
)


class ContractError(ValueError):
    """The untrusted archive header does not match the fixed v2 contract."""


@dataclass(frozen=True, slots=True)
class ArchiveHeader:
    """Validated binary values needed to derive and use the archive key."""

    salt: bytes
    nonce: bytes


def canonical_json(value: dict[str, Any]) -> bytes:
    """Return the one accepted JSON representation for an authenticated header."""

    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def _encoded_token(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decoded_token(value: Any, *, length: int) -> bytes:
    if type(value) is not str or not value:
        raise ContractError("invalid binary header field")
    try:
        padding = "=" * (-len(value) % 4)
        decoded = base64.b64decode(value + padding, altchars=b"-_", validate=True)
    except (ValueError, UnicodeEncodeError, binascii.Error) as exc:
        raise ContractError("invalid binary header field") from exc
    if len(decoded) != length or _encoded_token(decoded) != value:
        raise ContractError("invalid binary header field")
    return decoded


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError("duplicate header field")
        result[key] = value
    return result


def build_header(*, salt: bytes, nonce: bytes) -> bytes:
    """Build the canonical fixed-profile header for fresh random inputs."""

    if type(salt) is not bytes or len(salt) != SALT_BYTES:
        raise ContractError("invalid salt")
    if type(nonce) is not bytes or len(nonce) != NONCE_BYTES:
        raise ContractError("invalid nonce")
    encoded = canonical_json(
        {
            "format_major": FORMAT_MAJOR,
            "format_minor": FORMAT_MINOR,
            "kdf": KDF_NAME,
            "kdf_iterations": ARGON2_ITERATIONS,
            "kdf_lanes": ARGON2_LANES,
            "kdf_memory_cost_kib": ARGON2_MEMORY_COST_KIB,
            "kdf_output_bytes": KEY_BYTES,
            "nonce": _encoded_token(nonce),
            "salt": _encoded_token(salt),
        }
    )
    if len(encoded) > MAX_HEADER_BYTES:  # pragma: no cover - fixed fields are tiny
        raise ContractError("header exceeds the fixed limit")
    return encoded


def parse_header(encoded: bytes) -> ArchiveHeader:
    """Validate exact keys, values and canonical representation before KDF work."""

    if type(encoded) is not bytes or not 1 <= len(encoded) <= MAX_HEADER_BYTES:
        raise ContractError("invalid header length")
    try:
        value = json.loads(encoded, object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, ContractError) as exc:
        raise ContractError("invalid header JSON") from exc
    if type(value) is not dict or frozenset(value) != _HEADER_KEYS:
        raise ContractError("invalid header fields")
    expected_integers = {
        "format_major": FORMAT_MAJOR,
        "format_minor": FORMAT_MINOR,
        "kdf_iterations": ARGON2_ITERATIONS,
        "kdf_lanes": ARGON2_LANES,
        "kdf_memory_cost_kib": ARGON2_MEMORY_COST_KIB,
        "kdf_output_bytes": KEY_BYTES,
    }
    if any(type(value[key]) is not int or value[key] != expected for key, expected in expected_integers.items()):
        raise ContractError("unsupported header parameters")
    if type(value["kdf"]) is not str or value["kdf"] != KDF_NAME:
        raise ContractError("unsupported KDF")
    if canonical_json(value) != encoded:
        raise ContractError("header is not canonical JSON")
    return ArchiveHeader(
        salt=_decoded_token(value["salt"], length=SALT_BYTES),
        nonce=_decoded_token(value["nonce"], length=NONCE_BYTES),
    )


def prelude(header: bytes) -> bytes:
    """Prefix an already canonical header with magic and its big-endian length."""

    if type(header) is not bytes or not 1 <= len(header) <= MAX_HEADER_BYTES:
        raise ContractError("invalid header length")
    return MAGIC + struct.pack(">I", len(header))


__all__ = [
    "ARGON2_ITERATIONS",
    "ARGON2_LANES",
    "ARGON2_MEMORY_COST_KIB",
    "ArchiveHeader",
    "ContractError",
    "FORMAT_MAJOR",
    "FORMAT_MINOR",
    "KDF_NAME",
    "KEY_BYTES",
    "MAGIC",
    "MAX_ENCRYPTED_BYTES",
    "MAX_HEADER_BYTES",
    "MAX_PLAINTEXT_BYTES",
    "NONCE_BYTES",
    "SALT_BYTES",
    "TAG_BYTES",
    "build_header",
    "canonical_json",
    "parse_header",
    "prelude",
]
