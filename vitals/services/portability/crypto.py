"""Streaming authenticated encryption for the portability-v2 container."""

from __future__ import annotations

import os
import struct
import tempfile
import unicodedata
from typing import BinaryIO

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id

from vitals.services.portability import contract

DEFAULT_CHUNK_BYTES = 1024 * 1024
MAX_CHUNK_BYTES = 8 * 1024 * 1024
MIN_PASSPHRASE_BYTES = 12
MAX_PASSPHRASE_BYTES = 1024

_INVALID_ARCHIVE = "invalid portability archive"


class PortabilityCryptoError(ValueError):
    """A passphrase, archive, cap or output boundary failed closed."""


def _passphrase_bytes(passphrase: str) -> bytes:
    if type(passphrase) is not str:
        raise PortabilityCryptoError("passphrase must be text")
    encoded = unicodedata.normalize("NFC", passphrase).encode("utf-8")
    if not MIN_PASSPHRASE_BYTES <= len(encoded) <= MAX_PASSPHRASE_BYTES:
        raise PortabilityCryptoError(
            f"passphrase must encode to {MIN_PASSPHRASE_BYTES}-{MAX_PASSPHRASE_BYTES} bytes"
        )
    return encoded


def _derive_key(*, passphrase: bytes, salt: bytes) -> bytearray:
    derived = Argon2id(
        salt=salt,
        length=contract.KEY_BYTES,
        iterations=contract.ARGON2_ITERATIONS,
        lanes=contract.ARGON2_LANES,
        memory_cost=contract.ARGON2_MEMORY_COST_KIB,
    ).derive(passphrase)
    return bytearray(derived)


def _clear(value: bytearray) -> None:
    value[:] = b"\x00" * len(value)


def _chunk_size(value: int) -> int:
    if type(value) is not int or not 1 <= value <= MAX_CHUNK_BYTES:
        raise PortabilityCryptoError("invalid streaming chunk size")
    return value


def _read(source: BinaryIO, size: int) -> bytes:
    chunk = source.read(size)
    if chunk is None:
        raise OSError("archive source returned no data")
    if not isinstance(chunk, bytes | bytearray | memoryview):
        raise TypeError("archive source is not binary")
    return bytes(chunk)


def _read_exact(source: BinaryIO, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = _read(source, remaining)
        if not chunk:
            raise EOFError("truncated archive")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _write(destination: BinaryIO, body: bytes) -> None:
    view = memoryview(body)
    while view:
        written = destination.write(view)
        if written is None:
            raise OSError("archive destination did not report a completed write")
        if type(written) is not int or written <= 0 or written > len(view):
            raise OSError("archive destination refused data")
        view = view[written:]


def _decrypt_pass(
    source: BinaryIO,
    destination: BinaryIO | None,
    *,
    ciphertext_size: int,
    tag: bytes,
    key: bytearray,
    nonce: bytes,
    aad: bytes,
    chunk_size: int,
) -> None:
    """Run one GCM pass, optionally releasing its plaintext to a binary sink."""

    source.seek(0)
    decryptor = Cipher(algorithms.AES(bytes(key)), modes.GCM(nonce)).decryptor()
    decryptor.authenticate_additional_data(aad)
    remaining = ciphertext_size
    while remaining:
        chunk = _read(source, min(chunk_size, remaining))
        if not chunk:
            raise EOFError("truncated ciphertext spool")
        remaining -= len(chunk)
        plaintext = decryptor.update(chunk)
        if destination is not None:
            _write(destination, plaintext)
    final = decryptor.finalize_with_tag(tag)
    if destination is not None:
        _write(destination, final)


def encrypt_stream(
    source: BinaryIO,
    destination: BinaryIO,
    *,
    passphrase: str,
    chunk_size: int = DEFAULT_CHUNK_BYTES,
) -> int:
    """Encrypt a binary source into one authenticated v2 archive.

    The source and destination need not be seekable.  If a cap is crossed the
    destination contains an incomplete archive and must be discarded by the
    caller; it can never authenticate successfully.
    """

    chunk_size = _chunk_size(chunk_size)
    password = _passphrase_bytes(passphrase)
    salt = os.urandom(contract.SALT_BYTES)
    nonce = os.urandom(contract.NONCE_BYTES)
    header = contract.build_header(salt=salt, nonce=nonce)
    prefix = contract.prelude(header)
    aad = prefix + header
    encrypted_size = len(aad) + contract.TAG_BYTES
    if encrypted_size > contract.MAX_ENCRYPTED_BYTES:
        raise PortabilityCryptoError("encrypted archive exceeds the hard limit")

    key = _derive_key(passphrase=password, salt=salt)
    try:
        encryptor = Cipher(algorithms.AES(bytes(key)), modes.GCM(nonce)).encryptor()
        encryptor.authenticate_additional_data(aad)
        _write(destination, aad)
        plaintext_size = 0
        while chunk := _read(source, chunk_size):
            plaintext_size += len(chunk)
            encrypted_size += len(chunk)
            if plaintext_size > contract.MAX_PLAINTEXT_BYTES:
                raise PortabilityCryptoError("plaintext exceeds the hard limit")
            if encrypted_size > contract.MAX_ENCRYPTED_BYTES:
                raise PortabilityCryptoError("encrypted archive exceeds the hard limit")
            _write(destination, encryptor.update(chunk))
        _write(destination, encryptor.finalize())
        _write(destination, encryptor.tag)
        return encrypted_size
    finally:
        _clear(key)


def decrypt_stream(
    source: BinaryIO,
    destination: BinaryIO,
    *,
    passphrase: str,
    chunk_size: int = DEFAULT_CHUNK_BYTES,
) -> int:
    """Authenticate before streaming plaintext to any binary destination.

    A monolithic GCM tag is known only at EOF.  A first pass therefore spools only
    ciphertext into an owner-only temporary file and authenticates it while
    discarding the tentative plaintext.  Only a successful tag check permits the
    second pass to write plaintext.  The KDF runs once and plaintext is never
    internally spooled, so both input and output may be non-seekable.
    """

    chunk_size = _chunk_size(chunk_size)
    password = _passphrase_bytes(passphrase)
    key: bytearray | None = None
    try:
        prefix = _read_exact(source, len(contract.MAGIC) + 4)
        if prefix[: len(contract.MAGIC)] != contract.MAGIC:
            raise ValueError("bad magic")
        header_size = struct.unpack(">I", prefix[len(contract.MAGIC) :])[0]
        if not 1 <= header_size <= contract.MAX_HEADER_BYTES:
            raise ValueError("bad header length")
        header_bytes = _read_exact(source, header_size)
        header = contract.parse_header(header_bytes)
        aad = prefix + header_bytes
        encrypted_size = len(aad)
        with tempfile.TemporaryFile(mode="w+b") as ciphertext_spool:
            os.fchmod(ciphertext_spool.fileno(), 0o600)
            pending = b""
            ciphertext_size = 0
            while chunk := _read(source, chunk_size):
                encrypted_size += len(chunk)
                if encrypted_size > contract.MAX_ENCRYPTED_BYTES:
                    raise ValueError("archive exceeds cap")
                pending += chunk
                if len(pending) <= contract.TAG_BYTES:
                    continue
                ciphertext = pending[: -contract.TAG_BYTES]
                pending = pending[-contract.TAG_BYTES :]
                ciphertext_size += len(ciphertext)
                if ciphertext_size > contract.MAX_PLAINTEXT_BYTES:
                    raise ValueError("plaintext exceeds cap")
                _write(ciphertext_spool, ciphertext)
            if len(pending) != contract.TAG_BYTES:
                raise ValueError("truncated authentication tag")

            key = _derive_key(passphrase=password, salt=header.salt)
            _decrypt_pass(
                ciphertext_spool,
                None,
                ciphertext_size=ciphertext_size,
                tag=pending,
                key=key,
                nonce=header.nonce,
                aad=aad,
                chunk_size=chunk_size,
            )
            _decrypt_pass(
                ciphertext_spool,
                destination,
                ciphertext_size=ciphertext_size,
                tag=pending,
                key=key,
                nonce=header.nonce,
                aad=aad,
                chunk_size=chunk_size,
            )
            return ciphertext_size
    except Exception as exc:
        raise PortabilityCryptoError(_INVALID_ARCHIVE) from exc
    finally:
        if key is not None:
            _clear(key)


__all__ = [
    "DEFAULT_CHUNK_BYTES",
    "MAX_CHUNK_BYTES",
    "MAX_PASSPHRASE_BYTES",
    "MIN_PASSPHRASE_BYTES",
    "PortabilityCryptoError",
    "decrypt_stream",
    "encrypt_stream",
]
