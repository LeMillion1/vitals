"""Push-style authenticated encryption for streaming archive producers."""

from __future__ import annotations

import io

import pytest

from vitals.services.portability import contract, crypto


PASSPHRASE = "correct horse battery staple"


class _PartialWriter:
    def __init__(self) -> None:
        self.body = bytearray()

    def write(self, body: bytes | bytearray | memoryview) -> int:
        accepted = min(len(body), 3)
        self.body.extend(bytes(body[:accepted]))
        return accepted


def _decrypt(body: bytes) -> bytes:
    destination = io.BytesIO()
    crypto.decrypt_stream(
        io.BytesIO(body),
        destination,
        passphrase=PASSPHRASE,
        chunk_size=5,
    )
    return destination.getvalue()


def test_push_writer_encrypts_chunks_without_plaintext_spooling():
    destination = _PartialWriter()

    with crypto.EncryptingWriter(destination, passphrase=PASSPHRASE) as writer:
        assert writer.write(b"streamed ") == 9
        assert writer.write(memoryview(b"medical archive")) == 15
        assert writer.plaintext_size == 24
        assert not writer.closed

    assert writer.closed
    assert writer.encrypted_size == len(destination.body)
    assert _decrypt(bytes(destination.body)) == b"streamed medical archive"


def test_exception_aborts_without_an_authentication_tag():
    destination = io.BytesIO()

    with pytest.raises(RuntimeError, match="archive build failed"):
        with crypto.EncryptingWriter(destination, passphrase=PASSPHRASE) as writer:
            writer.write(b"private prefix")
            raise RuntimeError("archive build failed")

    assert writer.closed
    with pytest.raises(
        crypto.PortabilityCryptoError, match="^invalid portability archive$"
    ):
        _decrypt(destination.getvalue())


def test_cap_failure_permanently_aborts_writer(monkeypatch):
    monkeypatch.setattr(contract, "MAX_PLAINTEXT_BYTES", 5)
    destination = io.BytesIO()
    writer = crypto.EncryptingWriter(destination, passphrase=PASSPHRASE)

    with pytest.raises(crypto.PortabilityCryptoError, match="plaintext exceeds"):
        writer.write(b"123456")
    assert writer.closed
    with pytest.raises(crypto.PortabilityCryptoError, match="closed"):
        writer.write(b"1")
    with pytest.raises(crypto.PortabilityCryptoError, match="closed"):
        writer.finish()


def test_finish_is_idempotent_but_write_after_finish_is_refused():
    destination = io.BytesIO()
    writer = crypto.EncryptingWriter(destination, passphrase=PASSPHRASE)
    writer.write(b"payload")

    first_size = writer.finish()
    assert writer.finish() == first_size
    with pytest.raises(crypto.PortabilityCryptoError, match="closed"):
        writer.write(b"another payload")
    assert _decrypt(destination.getvalue()) == b"payload"

