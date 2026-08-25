"""The export operation joins graph, archive and crypto without plaintext."""

from __future__ import annotations

import io

from vitals.operations.portability.export_v2 import export_subject_encrypted
from vitals.services.portability.archive_reader import (
    open_validated_encrypted_archive,
)
from vitals.services.portability.record_decoder import decode_validated_record
from vitals.services.portability.resources import ResourceLocations
from vitals.services.portability.schema import PORTABILITY_SCHEMA_DIGEST


_PASSPHRASE = "synthetic correct horse battery staple"


async def test_subject_export_is_immediately_readable_and_flush_free(
    db_session,
    legacy_owner_roots,
    tmp_path,
):
    destination = io.BytesIO()
    locations = ResourceLocations(
        static_dir=str(tmp_path / "static"),
        private_root=str(tmp_path / "private"),
    )

    result = await export_subject_encrypted(
        db_session,
        subject_id=legacy_owner_roots.subject_id,
        passphrase=_PASSPHRASE,
        destination=destination,
        locations=locations,
    )

    assert result.archive_id.int != 0
    assert result.schema_digest == PORTABILITY_SCHEMA_DIGEST
    assert result.encrypted_bytes == len(destination.getvalue())
    assert result.plaintext_bytes > 0
    with open_validated_encrypted_archive(
        io.BytesIO(destination.getvalue()),
        passphrase=_PASSPHRASE,
    ) as archive:
        decoded = decode_validated_record(archive)
        assert archive.archive_id == result.archive_id
        assert archive.record_ref == result.record_ref
        assert archive.plaintext_bytes == result.plaintext_bytes
        assert decoded.row_count == result.row_count
        assert len(decoded.tables) == result.table_count
        assert len(decoded.connections) == result.connection_count
        assert len(decoded.resources) == result.resource_count


async def test_export_does_not_commit_the_callers_transaction(
    db_session,
    legacy_owner_roots,
    tmp_path,
    monkeypatch,
):
    async def forbidden_commit():
        raise AssertionError("export must not commit")

    monkeypatch.setattr(db_session, "commit", forbidden_commit)
    await export_subject_encrypted(
        db_session,
        subject_id=legacy_owner_roots.subject_id,
        passphrase=_PASSPHRASE,
        destination=io.BytesIO(),
        locations=ResourceLocations(
            static_dir=str(tmp_path / "static"),
            private_root=str(tmp_path / "private"),
        ),
    )
