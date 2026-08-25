from __future__ import annotations

import io
import json
import struct

import pytest

from vitals.services.portability import contract, crypto

PASSPHRASE = "correct horse battery staple"


class NonSeekableReader:
    def __init__(self, body: bytes, *, maximum_read: int = 7) -> None:
        self._stream = io.BytesIO(body)
        self._maximum_read = maximum_read

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = self._maximum_read
        return self._stream.read(min(size, self._maximum_read))


class NonSeekableWriter:
    def __init__(self) -> None:
        self.body = bytearray()

    def write(self, body: bytes) -> int:
        self.body.extend(body)
        return len(body)


class PartialWriter(NonSeekableWriter):
    def write(self, body: bytes) -> int:
        return super().write(body[:3])


class NoneWriter:
    def write(self, _body: bytes) -> None:
        return None


def _encrypt(body: bytes, *, passphrase: str = PASSPHRASE, chunk_size: int = 11) -> bytes:
    destination = io.BytesIO()
    written = crypto.encrypt_stream(
        NonSeekableReader(body),
        destination,
        passphrase=passphrase,
        chunk_size=chunk_size,
    )
    assert written == len(destination.getvalue())
    return destination.getvalue()


def _decrypt(body: bytes, *, passphrase: str = PASSPHRASE, chunk_size: int = 13) -> bytes:
    destination = io.BytesIO()
    read = crypto.decrypt_stream(
        NonSeekableReader(body),
        destination,
        passphrase=passphrase,
        chunk_size=chunk_size,
    )
    assert read == len(destination.getvalue())
    return destination.getvalue()


def _parts(archive: bytes) -> tuple[bytes, dict[str, object], bytes, bytes]:
    header_length = struct.unpack(">I", archive[len(contract.MAGIC) : len(contract.MAGIC) + 4])[0]
    header_start = len(contract.MAGIC) + 4
    header_end = header_start + header_length
    return (
        archive[:header_start],
        json.loads(archive[header_start:header_end]),
        archive[header_start:header_end],
        archive[header_end:],
    )


@pytest.mark.parametrize("body", [b"", b"a", bytes(range(256)) * 19])
def test_round_trip_streams_chunks_without_seekable_io(body: bytes):
    encrypted_destination = NonSeekableWriter()
    written = crypto.encrypt_stream(
        NonSeekableReader(body, maximum_read=3),
        encrypted_destination,
        passphrase=PASSPHRASE,
        chunk_size=5,
    )
    assert written == len(encrypted_destination.body)

    output = io.BytesIO()
    read = crypto.decrypt_stream(
        NonSeekableReader(bytes(encrypted_destination.body), maximum_read=2),
        output,
        passphrase=PASSPHRASE,
        chunk_size=7,
    )
    assert read == len(body)
    assert output.getvalue() == body


def test_passphrase_is_normalized_to_nfc():
    decomposed = "long-passphrase-cafe\u0301"
    composed = "long-passphrase-caf\u00e9"
    assert _decrypt(_encrypt(b"medical archive", passphrase=decomposed), passphrase=composed) == b"medical archive"


@pytest.mark.parametrize("passphrase", ["short", "x" * 1025])
def test_passphrase_byte_limits_are_enforced(passphrase: str):
    with pytest.raises(crypto.PortabilityCryptoError, match="passphrase"):
        _encrypt(b"payload", passphrase=passphrase)


def test_wrong_password_is_indistinguishable_and_rolls_back_output():
    archive = _encrypt(b"private medical payload")
    output = NonSeekableWriter()
    output.body.extend(b"prefix")
    with pytest.raises(crypto.PortabilityCryptoError, match="^invalid portability archive$"):
        crypto.decrypt_stream(io.BytesIO(archive), output, passphrase="another valid password")
    assert output.body == b"prefix"


@pytest.mark.parametrize("target", ["header", "ciphertext", "tag"])
def test_tampering_fails_uniformly_and_releases_no_plaintext(target: str):
    archive = bytearray(_encrypt(b"private medical payload" * 3))
    prefix, _header, header_bytes, encrypted = _parts(bytes(archive))
    if target == "header":
        offset = len(prefix) + header_bytes.index(b"argon2id")
    elif target == "ciphertext":
        assert len(encrypted) > contract.TAG_BYTES
        offset = len(prefix) + len(header_bytes)
    else:
        offset = len(archive) - 1
    archive[offset] ^= 1

    output = io.BytesIO()
    with pytest.raises(crypto.PortabilityCryptoError, match="^invalid portability archive$"):
        crypto.decrypt_stream(io.BytesIO(archive), output, passphrase=PASSPHRASE)
    assert output.getvalue() == b""


@pytest.mark.parametrize("removed", [1, contract.TAG_BYTES, contract.TAG_BYTES + 1, 100])
def test_truncation_fails_uniformly(removed: int):
    archive = _encrypt(b"private medical payload" * 10)
    with pytest.raises(crypto.PortabilityCryptoError, match="^invalid portability archive$"):
        _decrypt(archive[:-removed])


def test_unknown_major_kdf_and_cost_are_rejected_before_argon2(monkeypatch):
    archive = _encrypt(b"payload")
    prefix, header, _header_bytes, encrypted = _parts(archive)

    def must_not_run(**_kwargs):
        raise AssertionError("untrusted parameters reached Argon2id")

    monkeypatch.setattr(crypto, "_derive_key", must_not_run)
    for key, value in (
        ("format_major", 3),
        ("kdf", "scrypt"),
        ("kdf_memory_cost_kib", contract.ARGON2_MEMORY_COST_KIB * 1024),
    ):
        changed = dict(header)
        changed[key] = value
        changed_header = contract.canonical_json(changed)
        changed_prefix = contract.MAGIC + struct.pack(">I", len(changed_header))
        forged = changed_prefix + changed_header + encrypted
        with pytest.raises(crypto.PortabilityCryptoError, match="^invalid portability archive$"):
            _decrypt(forged)


def test_oversized_header_is_rejected_without_reading_or_kdf(monkeypatch):
    forged = contract.MAGIC + struct.pack(">I", contract.MAX_HEADER_BYTES + 1)

    def must_not_run(**_kwargs):
        raise AssertionError("oversized header reached Argon2id")

    monkeypatch.setattr(crypto, "_derive_key", must_not_run)
    with pytest.raises(crypto.PortabilityCryptoError, match="^invalid portability archive$"):
        _decrypt(forged)


def test_plaintext_and_encrypted_caps_fail_closed(monkeypatch):
    monkeypatch.setattr(contract, "MAX_PLAINTEXT_BYTES", 5)
    with pytest.raises(crypto.PortabilityCryptoError, match="plaintext exceeds"):
        _encrypt(b"123456")

    monkeypatch.setattr(contract, "MAX_PLAINTEXT_BYTES", 2 * 1024 * 1024 * 1024)
    archive = _encrypt(b"123456")
    header_end = len(contract.MAGIC) + 4 + len(_parts(archive)[2])
    monkeypatch.setattr(contract, "MAX_ENCRYPTED_BYTES", header_end + contract.TAG_BYTES + 5)
    with pytest.raises(crypto.PortabilityCryptoError, match="^invalid portability archive$"):
        _decrypt(archive)


def test_decryption_streams_to_a_non_seekable_output_after_authentication():
    archive = _encrypt(b"payload")
    output = NonSeekableWriter()
    assert crypto.decrypt_stream(
        NonSeekableReader(archive), output, passphrase=PASSPHRASE
    ) == len(b"payload")
    assert output.body == b"payload"


def test_partial_writes_are_completed_for_both_directions():
    encrypted = PartialWriter()
    expected = b"payload split across partial writes"
    size = crypto.encrypt_stream(
        NonSeekableReader(expected), encrypted, passphrase=PASSPHRASE, chunk_size=5
    )
    assert size == len(encrypted.body)

    decrypted = PartialWriter()
    assert crypto.decrypt_stream(
        NonSeekableReader(bytes(encrypted.body)),
        decrypted,
        passphrase=PASSPHRASE,
        chunk_size=4,
    ) == len(expected)
    assert decrypted.body == expected


def test_write_returning_none_is_never_treated_as_success():
    with pytest.raises(OSError, match="did not report"):
        crypto.encrypt_stream(io.BytesIO(b"payload"), NoneWriter(), passphrase=PASSPHRASE)

    archive = _encrypt(b"payload")
    with pytest.raises(crypto.PortabilityCryptoError, match="^invalid portability archive$"):
        crypto.decrypt_stream(io.BytesIO(archive), NoneWriter(), passphrase=PASSPHRASE)


def test_decryption_derives_the_argon2_key_once(monkeypatch):
    archive = _encrypt(b"payload")
    original = crypto._derive_key
    calls = 0

    def counted(**kwargs):
        nonlocal calls
        calls += 1
        return original(**kwargs)

    monkeypatch.setattr(crypto, "_derive_key", counted)
    assert _decrypt(archive) == b"payload"
    assert calls == 1


def test_header_encoding_is_deterministic_but_each_archive_uses_fresh_randomness():
    salt = bytes(range(contract.SALT_BYTES))
    nonce = bytes(range(contract.NONCE_BYTES))
    first = contract.build_header(salt=salt, nonce=nonce)
    assert first == contract.build_header(salt=salt, nonce=nonce)
    assert contract.parse_header(first) == contract.ArchiveHeader(salt=salt, nonce=nonce)

    archive_one = _encrypt(b"same payload")
    archive_two = _encrypt(b"same payload")
    _, header_one, bytes_one, _ = _parts(archive_one)
    _, header_two, bytes_two, _ = _parts(archive_two)
    assert bytes_one == contract.canonical_json(header_one)
    assert bytes_two == contract.canonical_json(header_two)
    assert header_one["salt"] != header_two["salt"]
    assert header_one["nonce"] != header_two["nonce"]
    assert archive_one != archive_two


def test_noncanonical_or_duplicate_header_is_rejected():
    salt = bytes(range(contract.SALT_BYTES))
    nonce = bytes(range(contract.NONCE_BYTES))
    header = contract.build_header(salt=salt, nonce=nonce)
    parsed = json.loads(header)
    noncanonical = json.dumps(parsed, sort_keys=False, indent=1).encode()
    duplicate = header[:-1] + b',"salt":"duplicate"}'
    with pytest.raises(contract.ContractError):
        contract.parse_header(noncanonical)
    with pytest.raises(contract.ContractError):
        contract.parse_header(duplicate)
