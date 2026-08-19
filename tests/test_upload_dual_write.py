"""Ownership, IDOR, and transaction contracts for persisted medical uploads."""
from __future__ import annotations

import os
from datetime import date

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import (
    Domain,
    FileAssetPurpose,
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
    Source,
    UserStatus,
)
from vitals.models.body_scan import BodyScan, BodyScanMetric
from vitals.models.identity import HealthSubject, User
from vitals.models.labs import LabMarker, LabResult
from vitals.models.raw_payload import RawPayload
from vitals.models.tenancy import FileAsset, IntegrationConnection
from vitals.models.weight import ProgressPhoto, WeightLog
from vitals.ownership import WriteIdentity
from vitals.services import (
    body_scan_service,
    conflict_engine,
    file_asset_service,
    labs_service,
    raw_payload_service,
    weight_service,
)
from vitals.services.upload_ownership_service import UploadOwnershipError
from web.templating import STATIC_DIR


async def _identity_graph(
    session: AsyncSession, slug: str
) -> tuple[User, HealthSubject, WriteIdentity]:
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
    return user, subject, WriteIdentity(subject.id, user.id)


async def _owned_document(
    session: AsyncSession,
    *,
    user: User,
    subject: HealthSubject,
    identity: WriteIdentity,
    purpose: FileAssetPurpose,
    storage_ref: str,
    domain: str,
    source: str,
    payload: dict,
) -> tuple[FileAsset, RawPayload]:
    connection_id = None
    if source == Source.LAB_PARSER.value:
        connection = IntegrationConnection(
            subject_id=subject.id,
            provider=IntegrationProvider.OPENROUTER.value,
            connection_type=IntegrationConnectionType.AI_GATEWAY.value,
            external_account_discriminator=f"synthetic-{storage_ref}",
            status=IntegrationConnectionStatus.ACTIVE.value,
        )
        session.add(connection)
        await session.flush()
        connection_id = connection.id
    asset = await file_asset_service.register_legacy_local(
        session,
        subject_id=subject.id,
        uploaded_by_user_id=user.id,
        purpose=purpose,
        storage_ref=storage_ref,
        media_type="image/png",
        size_bytes=8,
        content_sha256="a" * 64,
    )
    raw = await raw_payload_service.upsert_owned_raw_payload(
        session,
        identity=identity,
        integration_connection_id=connection_id,
        file_asset_id=asset.id,
        domain=domain,
        source=source,
        external_id=storage_ref,
        payload=payload,
    )
    return asset, raw


async def _prepared(
    session: AsyncSession,
    identity: WriteIdentity,
    *,
    on_date: date = date(2026, 8, 19),
):
    context = conflict_engine.ConflictWriteContext(
        identity=identity,
        evaluation_date=on_date,
        legacy_bridge=conflict_engine.LegacyConflictBridge.REJECT,
    )
    return await conflict_engine.prepare_scoped_write(session, context=context)


async def _prepared_weight(
    session: AsyncSession,
    identity: WriteIdentity,
    *,
    on_date: date = date(2026, 8, 19),
):
    context = conflict_engine.ConflictWriteContext(
        identity=identity,
        evaluation_date=on_date,
        legacy_bridge=conflict_engine.LegacyConflictBridge.REJECT,
    )
    return await weight_service.prepare_weight_write(session, context=context)


async def test_body_upload_confirm_copies_subject_actor_and_file_to_all_facts(
    db_session,
):
    user, subject, identity = await _identity_graph(db_session, "body-owner")
    asset, raw = await _owned_document(
        db_session,
        user=user,
        subject=subject,
        identity=identity,
        purpose=FileAssetPurpose.BODY_SCAN_DOCUMENT,
        storage_ref="body/owned-scan.png",
        domain=Domain.BODY_COMPOSITION.value,
        source=Source.BODY_SCAN.value,
        payload={"metrics": [{"label": "Вес", "value": 81.2}]},
    )
    prepared_weight = await _prepared_weight(db_session, identity)

    scan = await body_scan_service.save_scan(
        db_session,
        on_date=date(2026, 8, 19),
        file_key=raw.external_id,
        raw_payload_id=raw.id,
        metrics=[{"label": "Вес", "value": 81.2}],
        identity=identity,
        prepared_weight_write=prepared_weight,
    )

    assert (scan.subject_id, scan.actor_user_id, scan.file_asset_id) == (
        subject.id,
        user.id,
        asset.id,
    )
    metric = await db_session.scalar(
        select(BodyScanMetric).where(BodyScanMetric.scan_id == scan.id)
    )
    assert metric is not None and metric.subject_id == subject.id
    bridged = await db_session.scalar(
        select(WeightLog).where(WeightLog.source == Source.BODY_SCAN.value)
    )
    assert bridged is not None
    assert (bridged.subject_id, bridged.actor_user_id) == (subject.id, user.id)
    assert raw.processed_at is not None


async def test_lab_upload_confirm_copies_subject_actor_and_rejects_key_tampering(
    db_session,
):
    user, subject, identity = await _identity_graph(db_session, "lab-owner")
    asset, raw = await _owned_document(
        db_session,
        user=user,
        subject=subject,
        identity=identity,
        purpose=FileAssetPurpose.LAB_DOCUMENT,
        storage_ref="labs/owned-panel.png",
        domain=Domain.LABS.value,
        source=Source.LAB_PARSER.value,
        payload={"results": [{"marker": "Ferritin", "value": 95}]},
    )
    prepared = await _prepared(db_session, identity)

    with pytest.raises(UploadOwnershipError, match="file_key"):
        await labs_service.confirm_extracted(
            db_session,
            on_date=date(2026, 8, 19),
            markers=[{"marker": "Ferritin", "value": 95}],
            raw_payload_id=raw.id,
            file_key="labs/client-substitution.png",
            identity=identity,
            prepared_conflict_write=prepared,
        )
    assert await db_session.scalar(select(func.count()).select_from(LabResult)) == 0

    results = await labs_service.confirm_extracted(
        db_session,
        on_date=date(2026, 8, 19),
        markers=[{"marker": "Ferritin", "value": 105}],
        raw_payload_id=raw.id,
        file_key=asset.storage_ref,
        identity=identity,
        prepared_conflict_write=prepared,
    )
    assert len(results) == 1
    assert (results[0].subject_id, results[0].actor_user_id) == (
        subject.id,
        user.id,
    )
    marker = await db_session.scalar(select(LabMarker))
    assert marker is not None
    assert (marker.subject_id, marker.actor_user_id) == (subject.id, user.id)
    assert raw.processed_at is not None


@pytest.mark.parametrize("domain", ["body", "labs"])
async def test_foreign_raw_id_cannot_authorize_upload_confirmation(
    db_session, domain
):
    _owner, _owner_subject, owner_identity = await _identity_graph(
        db_session, "confirm-owner"
    )
    foreign_user, foreign_subject, foreign_identity = await _identity_graph(
        db_session, "confirm-foreign"
    )
    is_body = domain == "body"
    purpose = (
        FileAssetPurpose.BODY_SCAN_DOCUMENT
        if is_body
        else FileAssetPurpose.LAB_DOCUMENT
    )
    storage_ref = "body/foreign.png" if is_body else "labs/foreign.png"
    raw_domain = (
        Domain.BODY_COMPOSITION.value if is_body else Domain.LABS.value
    )
    raw_source = Source.BODY_SCAN.value if is_body else Source.LAB_PARSER.value
    _asset, raw = await _owned_document(
        db_session,
        user=foreign_user,
        subject=foreign_subject,
        identity=foreign_identity,
        purpose=purpose,
        storage_ref=storage_ref,
        domain=raw_domain,
        source=raw_source,
        payload={},
    )
    prepared_weight = await _prepared_weight(db_session, owner_identity)
    prepared = await _prepared(db_session, owner_identity)

    with pytest.raises(UploadOwnershipError, match="subject scope"):
        if is_body:
            await body_scan_service.save_scan(
                db_session,
                on_date=date(2026, 8, 19),
                file_key=storage_ref,
                raw_payload_id=raw.id,
                metrics=[],
                identity=owner_identity,
                prepared_weight_write=prepared_weight,
            )
        else:
            await labs_service.confirm_extracted(
                db_session,
                on_date=date(2026, 8, 19),
                markers=[{"marker": "TSH", "value": 2.0}],
                raw_payload_id=raw.id,
                file_key=storage_ref,
                identity=owner_identity,
                prepared_conflict_write=prepared,
            )

    model = BodyScan if is_body else LabResult
    assert await db_session.scalar(select(func.count()).select_from(model)) == 0
    assert raw.processed_at is None


async def test_progress_photo_reads_and_deletes_are_strictly_subject_scoped(
    db_session,
):
    owner, owner_subject, owner_identity = await _identity_graph(
        db_session, "photo-owner"
    )
    foreign, foreign_subject, foreign_identity = await _identity_graph(
        db_session, "photo-foreign"
    )
    owner_asset = await file_asset_service.register_legacy_local(
        db_session,
        subject_id=owner_subject.id,
        uploaded_by_user_id=owner.id,
        purpose=FileAssetPurpose.PROGRESS_PHOTO,
        storage_ref="uploads/owner.png",
    )
    foreign_asset = await file_asset_service.register_legacy_local(
        db_session,
        subject_id=foreign_subject.id,
        uploaded_by_user_id=foreign.id,
        purpose=FileAssetPurpose.PROGRESS_PHOTO,
        storage_ref="uploads/foreign.png",
    )
    own_photo = await weight_service.add_progress_photo(
        db_session,
        on_date=date(2026, 8, 19),
        file_key=owner_asset.storage_ref,
        identity=owner_identity,
        file_asset_id=owner_asset.id,
    )
    foreign_photo = await weight_service.add_progress_photo(
        db_session,
        on_date=date(2026, 8, 19),
        file_key=foreign_asset.storage_ref,
        identity=foreign_identity,
        file_asset_id=foreign_asset.id,
    )

    visible = await weight_service.list_progress_photos(
        db_session, subject_id=owner_subject.id
    )
    assert [row.id for row in visible] == [own_photo.id]
    assert (
        await weight_service.delete_progress_photo(
            db_session,
            foreign_photo.id,
            subject_id=owner_subject.id,
        )
        is None
    )
    assert await db_session.get(ProgressPhoto, foreign_photo.id) is foreign_photo


async def test_body_and_lab_bare_ids_are_rejected_outside_subject_scope(db_session):
    _owner, owner_subject, _owner_identity = await _identity_graph(
        db_session, "fact-owner"
    )
    foreign, foreign_subject, _foreign_identity = await _identity_graph(
        db_session, "fact-foreign"
    )
    scan = BodyScan(
        subject_id=foreign_subject.id,
        actor_user_id=foreign.id,
        date=date(2026, 8, 19),
        domain=Domain.BODY_COMPOSITION.value,
        source=Source.BODY_SCAN.value,
    )
    result = LabResult(
        subject_id=foreign_subject.id,
        actor_user_id=foreign.id,
        date=date(2026, 8, 19),
        domain=Domain.LABS.value,
        source=Source.LAB_PARSER.value,
        marker="TSH",
        value=2.0,
    )
    marker = LabMarker(
        subject_id=foreign_subject.id,
        actor_user_id=foreign.id,
        domain=Domain.LABS.value,
        name="TSH",
    )
    db_session.add_all([scan, result, marker])
    await db_session.flush()

    assert (
        await body_scan_service.get_scan(
            db_session, scan.id, subject_id=owner_subject.id
        )
        is None
    )
    assert not await body_scan_service.delete_scan(
        db_session, scan.id, subject_id=owner_subject.id
    )
    assert not await labs_service.delete_result(
        db_session, result.id, subject_id=owner_subject.id
    )
    assert (
        await labs_service.defer_retest(
            db_session,
            "TSH",
            until=date(2026, 9, 1),
            subject_id=owner_subject.id,
        )
        is None
    )
    assert await db_session.get(BodyScan, scan.id) is scan
    assert await db_session.get(LabResult, result.id) is result
    assert marker.defer_until is None


@pytest.mark.parametrize("domain", ["body", "labs"])
async def test_owned_pending_upload_reparse_preserves_ownership(
    db_session, domain
):
    user, subject, identity = await _identity_graph(
        db_session, f"{domain}-reparse-owner"
    )
    is_body = domain == "body"
    storage_ref = "body/reparse.png" if is_body else "labs/reparse.png"
    payload = (
        {
            "date": "2026-08-19",
            "device": "Synthetic",
            "metrics": [{"label": "Белок", "value": 10.2}],
        }
        if is_body
        else {
            "date": "2026-08-19",
            "lab_name": "Synthetic",
            "results": [{"marker": "Ferritin", "value": 95}],
        }
    )
    asset, raw = await _owned_document(
        db_session,
        user=user,
        subject=subject,
        identity=identity,
        purpose=(
            FileAssetPurpose.BODY_SCAN_DOCUMENT
            if is_body
            else FileAssetPurpose.LAB_DOCUMENT
        ),
        storage_ref=storage_ref,
        domain=(Domain.BODY_COMPOSITION.value if is_body else Domain.LABS.value),
        source=(Source.BODY_SCAN.value if is_body else Source.LAB_PARSER.value),
        payload=payload,
    )

    if is_body:
        await body_scan_service.reparse_from_raw(db_session, raw)
        normalized = await db_session.scalar(select(BodyScan))
        child = await db_session.scalar(select(BodyScanMetric))
        assert child is not None and child.subject_id == subject.id
        assert normalized.file_asset_id == asset.id
    else:
        system_identity = WriteIdentity(subject.id, None)
        prepared = await _prepared(db_session, system_identity)
        await labs_service.reparse_owned_pending(
            db_session,
            identity=system_identity,
            prepared_conflict_write=prepared,
        )
        normalized = await db_session.scalar(select(LabResult))
    assert normalized is not None
    assert (normalized.subject_id, normalized.actor_user_id) == (
        subject.id,
        user.id,
    )
    assert normalized.raw_payload_id == raw.id


async def test_legacy_service_calls_remain_nullable_and_usable(db_session):
    photo = await weight_service.add_progress_photo(
        db_session,
        on_date=date(2026, 8, 19),
        file_key="uploads/legacy.png",
    )
    scan = await body_scan_service.save_scan(
        db_session,
        on_date=date(2026, 8, 19),
        metrics=[{"label": "Белок", "value": 9.9}],
    )
    results = await labs_service.confirm_extracted(
        db_session,
        on_date=date(2026, 8, 19),
        markers=[{"marker": "TSH", "value": 2.0}],
    )

    assert (photo.subject_id, photo.actor_user_id, photo.file_asset_id) == (
        None,
        None,
        None,
    )
    assert (scan.subject_id, scan.actor_user_id, scan.file_asset_id) == (
        None,
        None,
        None,
    )
    assert (results[0].subject_id, results[0].actor_user_id) == (None, None)
    assert await weight_service.delete_progress_photo(db_session, photo.id)
    assert await body_scan_service.delete_scan(db_session, scan.id)


async def _fake_lab_extract(contents, *, llm, content_type, filename=None):
    return {
        "date": "2026-08-19",
        "lab_name": "Synthetic",
        "results": [{"marker": "Ferritin", "value": 95}],
    }


async def _fake_body_extract(contents, *, llm, content_type, filename=None):
    return {
        "date": "2026-08-19",
        "device": "Synthetic",
        "metrics": [{"label": "Вес", "value": 81.2}],
    }


def _directory_snapshot(relative: str) -> set[str]:
    path = os.path.join(STATIC_DIR, "uploads", relative)
    return set(os.listdir(path)) if os.path.isdir(path) else set()


def _upload_files() -> set[str]:
    root = os.path.join(STATIC_DIR, "uploads")
    return {
        os.path.join(parent, name)
        for parent, _directories, names in os.walk(root)
        for name in names
    }


@pytest.fixture
def synthetic_upload_cleanup():
    before = _upload_files()
    yield
    for path in _upload_files() - before:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


async def test_lab_precommit_failure_rolls_back_metadata_and_removes_bytes(
    auth_client, db_session, monkeypatch, synthetic_upload_cleanup
):
    monkeypatch.setattr(labs_service, "extract_from_file", _fake_lab_extract)

    async def fail_raw(*args, **kwargs):
        raise RuntimeError("synthetic pre-commit failure")

    monkeypatch.setattr(raw_payload_service, "upsert_owned_raw_payload", fail_raw)
    before = _directory_snapshot("labs")
    with pytest.raises(RuntimeError, match="pre-commit"):
        await auth_client.post(
            "/labs/upload",
            files={"file": ("panel.png", b"synthetic-lab", "image/png")},
        )

    assert _directory_snapshot("labs") == before
    assert await db_session.scalar(select(func.count()).select_from(FileAsset)) == 0
    assert await db_session.scalar(select(func.count()).select_from(RawPayload)) == 0


async def test_progress_precommit_failure_rolls_back_metadata_and_removes_bytes(
    auth_client, db_session, monkeypatch, synthetic_upload_cleanup
):
    async def fail_photo(*args, **kwargs):
        raise RuntimeError("synthetic progress pre-commit failure")

    monkeypatch.setattr(weight_service, "add_progress_photo", fail_photo)
    before = _directory_snapshot("")
    with pytest.raises(RuntimeError, match="progress pre-commit"):
        await auth_client.post(
            "/weight/photo",
            data={"date": "2026-08-19"},
            files={"file": ("progress.png", b"synthetic-photo", "image/png")},
        )

    assert _directory_snapshot("") == before
    assert await db_session.scalar(select(func.count()).select_from(FileAsset)) == 0
    assert await db_session.scalar(select(func.count()).select_from(ProgressPhoto)) == 0


@pytest.mark.parametrize(
    ("endpoint", "relative_dir", "extract_service", "fake_extract", "file_name"),
    [
        ("/labs/upload", "labs", labs_service, _fake_lab_extract, "panel.png"),
        (
            "/weight/body-scan/upload",
            "body",
            body_scan_service,
            _fake_body_extract,
            "scan.png",
        ),
    ],
)
async def test_document_commit_ambiguity_preserves_committed_metadata_and_bytes(
    auth_client,
    db_session,
    monkeypatch,
    endpoint,
    relative_dir,
    extract_service,
    fake_extract,
    file_name,
    synthetic_upload_cleanup,
):
    monkeypatch.setattr(extract_service, "extract_from_file", fake_extract)
    real_commit = db_session.commit

    async def commit_then_lose_ack():
        await real_commit()
        raise RuntimeError("synthetic lost commit acknowledgement")

    monkeypatch.setattr(db_session, "commit", commit_then_lose_ack)
    before = _directory_snapshot(relative_dir)
    with pytest.raises(RuntimeError, match="lost commit"):
        await auth_client.post(
            endpoint,
            files={"file": (file_name, b"synthetic-document", "image/png")},
        )

    added = _directory_snapshot(relative_dir) - before
    assert len(added) == 1
    asset = await db_session.scalar(select(FileAsset))
    raw = await db_session.scalar(select(RawPayload))
    assert asset is not None and raw is not None
    assert raw.file_asset_id == asset.id
    assert os.path.isfile(os.path.join(STATIC_DIR, "uploads", relative_dir, added.pop()))


async def test_progress_commit_ambiguity_preserves_committed_metadata_and_bytes(
    auth_client, db_session, monkeypatch, synthetic_upload_cleanup
):
    real_commit = db_session.commit

    async def commit_then_lose_ack():
        await real_commit()
        raise RuntimeError("synthetic lost progress commit acknowledgement")

    monkeypatch.setattr(db_session, "commit", commit_then_lose_ack)
    before = _directory_snapshot("")
    with pytest.raises(RuntimeError, match="lost progress commit"):
        await auth_client.post(
            "/weight/photo",
            data={"date": "2026-08-19"},
            files={"file": ("progress.png", b"synthetic-photo", "image/png")},
        )

    photo = await db_session.scalar(select(ProgressPhoto))
    asset = await db_session.scalar(select(FileAsset))
    assert photo is not None and asset is not None
    assert photo.file_asset_id == asset.id
    added = {
        name
        for name in (_directory_snapshot("") - before)
        if name == os.path.basename(photo.file_key)
    }
    assert added == {os.path.basename(photo.file_key)}


async def test_web_document_upload_stamps_openrouter_connection(
    auth_client, db_session, monkeypatch, synthetic_upload_cleanup
):
    monkeypatch.setattr(labs_service, "extract_from_file", _fake_lab_extract)
    response = await auth_client.post(
        "/labs/upload",
        files={"file": ("panel.png", b"synthetic-lab", "image/png")},
    )
    assert response.status_code == 200
    payload = response.json()["lab"]
    raw = await db_session.get(RawPayload, payload["raw_payload_id"])
    asset = await db_session.get(FileAsset, raw.file_asset_id)
    connection = await db_session.get(
        IntegrationConnection, raw.integration_connection_id
    )
    owner = await db_session.get(User, raw.actor_user_id)

    assert raw.subject_id == asset.subject_id
    assert raw.actor_user_id == asset.uploaded_by_user_id
    assert owner is not None and owner.normalized_username == "tester"
    assert connection is not None
    assert connection.provider == IntegrationProvider.OPENROUTER.value
    assert connection.subject_id == raw.subject_id
    assert asset.storage_ref == payload["file_key"]
