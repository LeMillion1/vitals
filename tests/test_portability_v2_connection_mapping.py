"""Portability-v2 connection refs bind only to explicit, usable local roots."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest
from sqlalchemy import inspect as sa_inspect

from vitals.enums import IntegrationConnectionStatus, UserStatus
from vitals.models.identity import HealthSubject, User
from vitals.models.tenancy import IntegrationConnection
from vitals.services.portability import connection_mapping


async def _subject(session, slug: str) -> HealthSubject:
    user = User(
        username=slug,
        normalized_username=slug,
        password_hash="$synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    session.add(user)
    await session.flush()
    subject = HealthSubject(
        owner_user_id=user.id,
        display_name=f"Synthetic {slug}",
        timezone="Asia/Almaty",
    )
    session.add(subject)
    await session.flush()
    return subject


async def _connection(
    session,
    subject_id: uuid.UUID,
    *,
    provider: str,
    connection_type: str,
    status: IntegrationConnectionStatus = IntegrationConnectionStatus.ACTIVE,
    suffix: str,
) -> IntegrationConnection:
    row = IntegrationConnection(
        subject_id=subject_id,
        provider=provider,
        connection_type=connection_type,
        external_account_discriminator=f"synthetic-{suffix}",
        credential_ref=f"private-secret-ref-{suffix}",
        status=status.value,
        retired_at=(
            datetime(2026, 8, 25, tzinfo=timezone.utc)
            if status is IntegrationConnectionStatus.RETIRED
            else None
        ),
    )
    session.add(row)
    await session.flush()
    return row


def _descriptor(
    ref: str, provider: str, connection_type: str
) -> connection_mapping.ArchiveConnectionDescriptor:
    return connection_mapping.ArchiveConnectionDescriptor(
        ref=ref,
        provider=provider,
        connection_type=connection_type,
    )


@pytest.mark.asyncio
async def test_mapping_is_explicit_immutable_canonical_and_credential_free(db_session):
    subject = await _subject(db_session, "mapping-owner")
    hevy = await _connection(
        db_session,
        subject.id,
        provider="hevy",
        connection_type="account",
        suffix="hevy",
    )
    garmin_import = await _connection(
        db_session,
        subject.id,
        provider="garmin",
        connection_type="import",
        status=IntegrationConnectionStatus.LEGACY,
        suffix="garmin-import",
    )
    descriptors = [
        _descriptor("c00000002", "hevy", "account"),
        _descriptor("c00000001", "garmin", "import"),
    ]

    first = await connection_mapping.resolve_connection_mapping(
        db_session,
        target_subject_id=subject.id,
        archive_connections=descriptors,
        connection_ids_by_ref={
            "c00000002": hevy.id,
            "c00000001": garmin_import.id,
        },
    )
    second = await connection_mapping.resolve_connection_mapping(
        db_session,
        target_subject_id=subject.id,
        archive_connections=list(reversed(descriptors)),
        connection_ids_by_ref={
            "c00000001": garmin_import.id,
            "c00000002": hevy.id,
        },
    )

    assert first == second
    assert tuple(first) == ("c00000001", "c00000002")
    assert first["c00000001"] == garmin_import.id
    assert first.bindings == (
        connection_mapping.ConnectionBinding(
            ref="c00000001",
            connection_id=garmin_import.id,
            provider="garmin",
            connection_type="import",
        ),
        connection_mapping.ConnectionBinding(
            ref="c00000002",
            connection_id=hevy.id,
            provider="hevy",
            connection_type="account",
        ),
    )
    canonical = json.dumps(
        {
            "connections": [
                {
                    "connection_id": str(garmin_import.id),
                    "connection_type": "import",
                    "provider": "garmin",
                    "ref": "c00000001",
                },
                {
                    "connection_id": str(hevy.id),
                    "connection_type": "account",
                    "provider": "hevy",
                    "ref": "c00000002",
                },
            ],
            "format": "vitals-portability-connection-map",
            "target_subject_id": str(subject.id),
            "version": 1,
        },
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    assert first.sha256_hex == hashlib.sha256(canonical).hexdigest()
    assert "private-secret-ref" not in repr(first)
    with pytest.raises(FrozenInstanceError):
        first.sha256_hex = "0" * 64


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["missing", "extra", "aliased"])
async def test_mapping_requires_each_ref_exactly_once_and_is_one_to_one(db_session, mode):
    subject = await _subject(db_session, f"mapping-cardinality-{mode}")
    connection = await _connection(
        db_session,
        subject.id,
        provider="hevy",
        connection_type="account",
        suffix=mode,
    )
    descriptors = [
        _descriptor("c00000001", "hevy", "account"),
        _descriptor("c00000002", "garmin", "import"),
    ]
    mapping = {
        "missing": {"c00000001": connection.id},
        "extra": {
            "c00000001": connection.id,
            "c00000002": uuid.uuid4(),
            "c00000003": uuid.uuid4(),
        },
        "aliased": {
            "c00000001": connection.id,
            "c00000002": connection.id,
        },
    }[mode]

    with pytest.raises(connection_mapping.ConnectionMappingError) as raised:
        await connection_mapping.resolve_connection_mapping(
            db_session,
            target_subject_id=subject.id,
            archive_connections=descriptors,
            connection_ids_by_ref=mapping,
        )
    assert raised.value.code == (
        "connection_mapping_not_one_to_one"
        if mode == "aliased"
        else "connection_mapping_incomplete"
    )


@pytest.mark.asyncio
async def test_duplicate_refs_are_refused_but_two_same_kind_accounts_are_supported(
    db_session,
):
    subject = await _subject(db_session, "mapping-duplicate-descriptor")
    connection = await _connection(
        db_session,
        subject.id,
        provider="hevy",
        connection_type="account",
        suffix="duplicate",
    )

    duplicate_refs = [
        _descriptor("c00000001", "hevy", "account"),
        _descriptor("c00000001", "garmin", "import"),
    ]
    with pytest.raises(connection_mapping.ConnectionMappingError) as raised:
        await connection_mapping.resolve_connection_mapping(
            db_session,
            target_subject_id=subject.id,
            archive_connections=duplicate_refs,
            connection_ids_by_ref={"c00000001": connection.id},
        )
    assert raised.value.code == "connection_descriptor_duplicate"

    second = await _connection(
        db_session,
        subject.id,
        provider="hevy",
        connection_type="account",
        suffix="second-same-kind",
    )
    mapping = await connection_mapping.resolve_connection_mapping(
        db_session,
        target_subject_id=subject.id,
        archive_connections=[
            _descriptor("c00000001", "hevy", "account"),
            _descriptor("c00000002", "hevy", "account"),
        ],
        connection_ids_by_ref={
            "c00000001": connection.id,
            "c00000002": second.id,
        },
    )
    assert tuple(mapping.values()) == (connection.id, second.id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected_code"),
    [
        ("missing", "mapped_connection_missing"),
        ("cross_subject", "mapped_connection_cross_subject"),
        ("pending", "mapped_connection_not_usable"),
        ("disabled", "mapped_connection_not_usable"),
        ("retired", "mapped_connection_not_usable"),
        ("provider", "mapped_connection_descriptor_mismatch"),
        ("connection_type", "mapped_connection_descriptor_mismatch"),
    ],
)
async def test_live_connection_must_match_subject_state_and_descriptor(
    db_session, mode, expected_code
):
    subject = await _subject(db_session, f"mapping-target-{mode}")
    other = await _subject(db_session, f"mapping-other-{mode}")
    status = {
        "pending": IntegrationConnectionStatus.PENDING,
        "disabled": IntegrationConnectionStatus.DISABLED,
        "retired": IntegrationConnectionStatus.RETIRED,
    }.get(mode, IntegrationConnectionStatus.ACTIVE)
    connection = await _connection(
        db_session,
        other.id if mode == "cross_subject" else subject.id,
        provider="garmin" if mode in {"provider", "connection_type"} else "hevy",
        connection_type="import" if mode == "connection_type" else "account",
        status=status,
        suffix=mode,
    )
    connection_id = uuid.uuid4() if mode == "missing" else connection.id

    with pytest.raises(connection_mapping.ConnectionMappingError) as raised:
        await connection_mapping.resolve_connection_mapping(
            db_session,
            target_subject_id=subject.id,
            archive_connections=[
                _descriptor(
                    "c00000001",
                    "garmin" if mode == "connection_type" else "hevy",
                    "account",
                )
            ],
            connection_ids_by_ref={"c00000001": connection_id},
        )
    assert raised.value.code == expected_code


@pytest.mark.asyncio
async def test_empty_mapping_is_canonical_and_service_does_not_autoflush(db_session):
    subject = await _subject(db_session, "mapping-empty")
    empty = await connection_mapping.resolve_connection_mapping(
        db_session,
        target_subject_id=subject.id,
        archive_connections=[],
        connection_ids_by_ref={},
    )
    connection = await _connection(
        db_session,
        subject.id,
        provider="hevy",
        connection_type="account",
        suffix="no-autoflush",
    )
    pending_user = User(
        username="must-stay-pending",
        normalized_username="must-stay-pending",
        password_hash="$synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    db_session.add(pending_user)

    result = await connection_mapping.resolve_connection_mapping(
        db_session,
        target_subject_id=subject.id,
        archive_connections=[_descriptor("c00000001", "hevy", "account")],
        connection_ids_by_ref={"c00000001": connection.id},
    )

    assert empty.bindings == ()
    assert dict(empty) == {}
    assert result["c00000001"] == connection.id
    assert sa_inspect(pending_user).pending
    assert pending_user.id is None
