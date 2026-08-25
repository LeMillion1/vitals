"""Idempotent, flush-only portability-v2 import receipt service."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import FrozenInstanceError, replace

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vitals.enums import UserStatus
from vitals.models.identity import HealthSubject, User
from vitals.models.portability import PortabilityImportReceipt
from vitals.services.portability.receipts import (
    ImportReceiptRequest,
    ReceiptServiceError,
    find_import_receipt,
    record_completed_import,
)


_SHA_A = "a" * 64
_SHA_B = "b" * 64
_SHA_C = "c" * 64


async def _roots(db_session, suffix: str):
    owner = User(
        username=f"receipt-owner-{suffix}",
        normalized_username=f"receipt-owner-{suffix}",
        password_hash="$synthetic-receipt-owner",
        status=UserStatus.ACTIVE.value,
    )
    actor = User(
        username=f"receipt-actor-{suffix}",
        normalized_username=f"receipt-actor-{suffix}",
        password_hash="$synthetic-receipt-actor",
        status=UserStatus.ACTIVE.value,
    )
    db_session.add_all((owner, actor))
    await db_session.flush()
    subject = HealthSubject(
        owner_user_id=owner.id,
        display_name="Synthetic receipt subject",
        timezone="Asia/Almaty",
    )
    db_session.add(subject)
    await db_session.flush()
    return subject, actor


def _request(subject, actor, **changes) -> ImportReceiptRequest:
    values = {
        "subject_id": subject.id,
        "actor_user_id": actor.id,
        "operation_id": uuid.uuid4(),
        "archive_id": uuid.uuid4(),
        "manifest_digest": _SHA_A,
        "record_ref": "record_A-19",
        "record_digest": _SHA_B,
        "mapping_digest": _SHA_C,
        "row_count": 12,
        "resource_count": 3,
    }
    values.update(changes)
    return ImportReceiptRequest(**values)


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"subject_id": "not-a-uuid"}, "receipt_uuid_invalid"),
        ({"actor_user_id": None}, "receipt_uuid_invalid"),
        ({"operation_id": "operation"}, "receipt_uuid_invalid"),
        ({"archive_id": 1}, "receipt_uuid_invalid"),
        ({"archive_id": uuid.UUID(int=0)}, "receipt_uuid_invalid"),
        ({"manifest_digest": "a" * 63}, "receipt_digest_invalid"),
        ({"record_digest": "B" * 64}, "receipt_digest_invalid"),
        ({"mapping_digest": "g" * 64}, "receipt_digest_invalid"),
        ({"record_ref": ""}, "receipt_record_ref_invalid"),
        ({"record_ref": "records/subject"}, "receipt_record_ref_invalid"),
        ({"record_ref": "r" * 129}, "receipt_record_ref_invalid"),
        ({"row_count": -1}, "receipt_count_invalid"),
        ({"row_count": True}, "receipt_count_invalid"),
        ({"resource_count": 2**63}, "receipt_count_invalid"),
        ({"mode": "merge"}, "receipt_mode_invalid"),
    ],
)
async def test_request_rejects_invalid_control_metadata(
    db_session,
    changes,
    code,
):
    subject, actor = await _roots(db_session, str(abs(hash(str(changes)))))
    with pytest.raises(ReceiptServiceError) as raised:
        _request(subject, actor, **changes)
    assert raised.value.code == code


async def test_request_and_result_are_immutable_and_phi_free(db_session):
    subject, actor = await _roots(db_session, "immutable")
    request = _request(subject, actor)
    result = await record_completed_import(db_session, request)

    with pytest.raises(FrozenInstanceError):
        request.row_count = 99
    with pytest.raises(FrozenInstanceError):
        result.replayed = True
    assert set(request.__dataclass_fields__) == {
        "subject_id",
        "actor_user_id",
        "operation_id",
        "archive_id",
        "manifest_digest",
        "record_ref",
        "record_digest",
        "mapping_digest",
        "row_count",
        "resource_count",
        "mode",
    }
    assert set(result.__dataclass_fields__) == {
        "request",
        "receipt_id",
        "completed_at",
        "replayed",
    }


async def test_insert_is_flush_only_and_outer_rollback_removes_receipt(db_session):
    subject, actor = await _roots(db_session, "rollback")
    request = _request(subject, actor)

    created = await record_completed_import(db_session, request)

    assert created.created is True
    assert created.replayed is False
    assert await db_session.scalar(select(func.count()).select_from(PortabilityImportReceipt)) == 1
    await db_session.rollback()
    assert await db_session.scalar(select(func.count()).select_from(PortabilityImportReceipt)) == 0


async def test_exact_request_returns_replay_and_lookup_uses_subject_operation(
    db_session,
):
    first_subject, first_actor = await _roots(db_session, "first")
    second_subject, second_actor = await _roots(db_session, "second")
    operation_id = uuid.uuid4()
    first_request = _request(
        first_subject,
        first_actor,
        operation_id=operation_id,
    )
    second_request = _request(
        second_subject,
        second_actor,
        operation_id=operation_id,
    )
    first = await record_completed_import(db_session, first_request)
    second = await record_completed_import(db_session, second_request)

    replay = await record_completed_import(db_session, first_request)
    found_first = await find_import_receipt(
        db_session,
        subject_id=first_subject.id,
        operation_id=operation_id,
    )
    found_second = await find_import_receipt(
        db_session,
        subject_id=second_subject.id,
        operation_id=operation_id,
    )

    assert replay.replayed is True
    assert replay.receipt_id == first.receipt_id
    assert replay.completed_at == first.completed_at
    assert found_first is not None and found_first.request == first_request
    assert found_second is not None and found_second.request == second_request
    assert found_second.receipt_id == second.receipt_id
    assert await db_session.scalar(select(func.count()).select_from(PortabilityImportReceipt)) == 2


@pytest.mark.parametrize(
    "field",
    [
        "actor_user_id",
        "archive_id",
        "manifest_digest",
        "record_ref",
        "record_digest",
        "mapping_digest",
        "row_count",
        "resource_count",
    ],
)
async def test_idempotency_key_metadata_mismatch_fails_closed(
    db_session,
    field,
):
    subject, actor = await _roots(db_session, field)
    request = _request(subject, actor)
    await record_completed_import(db_session, request)
    replacements = {
        "actor_user_id": uuid.uuid4(),
        "archive_id": uuid.uuid4(),
        "manifest_digest": "d" * 64,
        "record_ref": "different_record",
        "record_digest": "e" * 64,
        "mapping_digest": "f" * 64,
        "row_count": request.row_count + 1,
        "resource_count": request.resource_count + 1,
    }
    conflicting = replace(request, **{field: replacements[field]})

    with pytest.raises(ReceiptServiceError) as raised:
        await record_completed_import(db_session, conflicting)

    assert raised.value.code == "receipt_metadata_mismatch"
    assert "different_record" not in str(raised.value)
    assert await db_session.scalar(select(func.count()).select_from(PortabilityImportReceipt)) == 1


async def test_lookup_validates_key_and_missing_returns_none(db_session):
    with pytest.raises(ReceiptServiceError) as raised:
        await find_import_receipt(
            db_session,
            subject_id="not-a-uuid",
            operation_id=uuid.uuid4(),
        )
    assert raised.value.code == "receipt_uuid_invalid"

    assert (
        await find_import_receipt(
            db_session,
            subject_id=uuid.uuid4(),
            operation_id=uuid.uuid4(),
        )
        is None
    )


async def test_postgres_concurrent_exact_insert_has_one_create_and_one_replay(
    db_session,
    monkeypatch,
):
    if db_session.get_bind().dialect.name != "postgresql":
        pytest.skip("PostgreSQL concurrency contract")
    subject, actor = await _roots(db_session, "postgres-race")
    request = _request(subject, actor)
    await db_session.commit()
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )

    import vitals.services.portability.receipts as receipt_service

    original_find = receipt_service._find_model
    arrived = 0
    release = asyncio.Event()

    async def synchronized_find(session, *, subject_id, operation_id):
        nonlocal arrived
        found = await original_find(
            session,
            subject_id=subject_id,
            operation_id=operation_id,
        )
        if found is None:
            arrived += 1
            if arrived == 2:
                release.set()
            await release.wait()
        return found

    monkeypatch.setattr(receipt_service, "_find_model", synchronized_find)

    async def write_once():
        async with factory() as session:
            result = await record_completed_import(session, request)
            await session.commit()
            return result

    first, second = await asyncio.wait_for(
        asyncio.gather(write_once(), write_once()),
        timeout=15,
    )
    assert sorted((first.replayed, second.replayed)) == [False, True]
    assert first.receipt_id == second.receipt_id

    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(PortabilityImportReceipt)) == 1
