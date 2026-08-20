"""Stage-2 ownership gates for Timeline annotations and Supplements CRUD."""
from __future__ import annotations

import ast
import uuid
from datetime import date, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from vitals.enums import (
    Domain,
    FileAssetPurpose,
    FileAssetStatus,
    FileStorageBackend,
    MilestoneStatus,
    Source,
    UserStatus,
)
from vitals.models.body_scan import BodyScan
from vitals.models.genetics import GeneticVariant
from vitals.models.glp1 import DosePhase, SideEffect
from vitals.models.identity import HealthSubject, User
from vitals.models.labs import LabResult
from vitals.models.milestones import Milestone
from vitals.models.raw_payload import RawPayload
from vitals.models.skincare import SkincareProduct
from vitals.models.supplements import Supplement
from vitals.models.tenancy import FileAsset
from vitals.models.timeline import Annotation
from vitals.models.weight import NoiseMarker, ProgressPhoto
from vitals.ownership import WriteIdentity
from vitals.services import (
    conflict_engine,
    milestones_service,
    supplements_service,
    timeline_service,
    weight_service,
)
from vitals.services.legacy_ownership import LegacySubjectResolutionError

mcp_router = pytest.importorskip("web.routers.mcp")


_DERIVED_TIMELINE_SELECTORS = (
    "dose_phase",
    "side_effect",
    "lab_result",
    "body_scan",
    "progress_photo",
    "milestone",
    "noise_marker",
    "supplement",
    "skincare_product",
    "genetic_variant",
)

_DERIVED_MODELS = {
    "dose_phase": DosePhase,
    "side_effect": SideEffect,
    "lab_result": LabResult,
    "body_scan": BodyScan,
    "progress_photo": ProgressPhoto,
    "milestone": Milestone,
    "noise_marker": NoiseMarker,
    "supplement": Supplement,
    "skincare_product": SkincareProduct,
    "genetic_variant": GeneticVariant,
}

_DERIVED_NEW_ROOTS = {
    "dose_phase": {"actor_user_id"},
    "side_effect": {"actor_user_id"},
    "lab_result": {"actor_user_id"},
    "body_scan": {"actor_user_id", "file_asset_id"},
    "progress_photo": {"actor_user_id", "file_asset_id"},
    "milestone": {"actor_user_id"},
    "noise_marker": {"actor_user_id"},
    "supplement": {"actor_user_id"},
    "skincare_product": {"actor_user_id"},
    "genetic_variant": {"actor_user_id"},
}

_DERIVED_RAW_LINKS = {
    "lab_result": "raw_payload_id",
    "body_scan": "raw_payload_id",
    "genetic_variant": "raw_payload_id",
}


def _derived_day(ordinal: int) -> date:
    return date(2026, 8, 10 + ordinal)


def _derived_timestamp(ordinal: int) -> datetime:
    return datetime(2026, 8, 10 + ordinal, 12, 0)


def _derived_row(
    selector: str,
    *,
    subject_id: uuid.UUID | None,
    ordinal: int,
):
    """Build one minimal fact for each independent Timeline selector."""

    on_date = _derived_day(ordinal)
    created_at = _derived_timestamp(ordinal)
    if selector == "dose_phase":
        return DosePhase(
            subject_id=subject_id,
            domain=Domain.GLP1.value,
            source=Source.MANUAL.value,
            start_date=on_date,
            drug="semaglutide",
            dose_mg=1.0,
        )
    if selector == "side_effect":
        return SideEffect(
            subject_id=subject_id,
            date=on_date,
            domain=Domain.GLP1.value,
            source=Source.MANUAL.value,
            effect_type=f"selector-effect-{ordinal}",
            severity=3,
        )
    if selector == "lab_result":
        return LabResult(
            subject_id=subject_id,
            date=on_date,
            domain=Domain.LABS.value,
            source=Source.MANUAL.value,
            marker=f"selector-marker-{ordinal}",
            value=float(ordinal + 1),
        )
    if selector == "body_scan":
        return BodyScan(
            subject_id=subject_id,
            date=on_date,
            domain=Domain.BODY_COMPOSITION.value,
            source=Source.MANUAL.value,
            device=f"selector-device-{ordinal}",
        )
    if selector == "progress_photo":
        return ProgressPhoto(
            subject_id=subject_id,
            date=on_date,
            domain=Domain.WEIGHT.value,
            source=Source.MANUAL.value,
            file_key=f"uploads/selector-{ordinal}.jpg",
        )
    if selector == "milestone":
        return Milestone(
            subject_id=subject_id,
            created_at=created_at,
            domain=Domain.WEIGHT.value,
            name=f"selector-milestone-{ordinal}",
            status=MilestoneStatus.ACTIVE.value,
        )
    if selector == "noise_marker":
        return NoiseMarker(
            subject_id=subject_id,
            domain=Domain.WEIGHT.value,
            source=Source.MANUAL.value,
            start_date=on_date,
            reason=f"selector-noise-{ordinal}",
        )
    if selector == "supplement":
        return Supplement(
            subject_id=subject_id,
            created_at=created_at,
            domain=Domain.SUPPLEMENTS.value,
            source=Source.MANUAL.value,
            name=f"selector-supplement-{ordinal}",
            key=f"selector_supplement_{ordinal}",
            active=True,
        )
    if selector == "skincare_product":
        return SkincareProduct(
            subject_id=subject_id,
            created_at=created_at,
            name=f"selector-skincare-{ordinal}",
            type="cleanser",
            default_time="evening",
            schedule_days=[],
            active=True,
        )
    if selector == "genetic_variant":
        return GeneticVariant(
            subject_id=subject_id,
            created_at=created_at,
            domain=Domain.GENETICS.value,
            source=Source.MANUAL.value,
            gene=f"SELECTOR{ordinal}",
        )
    raise AssertionError(f"unknown derived Timeline selector: {selector}")


def _derived_ref(selector: str, row) -> str:
    if selector == "dose_phase":
        return f"dose_phase:{row.id}"
    if selector == "side_effect":
        return f"side_effect:{row.id}"
    if selector == "lab_result":
        return f"labs:{row.date.isoformat()}"
    if selector == "body_scan":
        return f"body_scan:{row.id}"
    if selector == "progress_photo":
        return f"progress_photo:{row.id}"
    if selector == "milestone":
        return f"milestone_created:{row.id}"
    if selector == "noise_marker":
        return f"noise_marker:{row.id}"
    if selector == "supplement":
        return f"supplement_started:{row.id}"
    if selector == "skincare_product":
        return f"skincare_added:{row.id}"
    if selector == "genetic_variant":
        return f"genetics_import:{row.created_at.date().isoformat()}"
    raise AssertionError(f"unknown derived Timeline selector: {selector}")


async def _identity(session, slug: str) -> WriteIdentity:
    user = User(
        username=slug,
        normalized_username=slug,
        password_hash="$synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    session.add(user)
    await session.flush()
    subject = HealthSubject(owner_user_id=user.id, timezone="Asia/Almaty")
    session.add(subject)
    await session.flush()
    return WriteIdentity(subject_id=subject.id, actor_user_id=user.id)


async def _complete_progress_photo_graph(
    session,
    row,
    identity: WriteIdentity,
) -> None:
    if not isinstance(row, ProgressPhoto) or row.subject_id is None:
        return
    asset = FileAsset(
        subject_id=identity.subject_id,
        uploaded_by_user_id=identity.actor_user_id,
        purpose=FileAssetPurpose.PROGRESS_PHOTO.value,
        storage_backend=FileStorageBackend.LEGACY_LOCAL.value,
        storage_ref=row.file_key,
        status=FileAssetStatus.LEGACY_PLACEHOLDER.value,
    )
    session.add(asset)
    await session.flush()
    row.actor_user_id = identity.actor_user_id
    row.file_asset_id = asset.id


async def _prepared_supplement_write(
    session,
    identity: WriteIdentity,
    *,
    legacy_bridge: bool = False,
):
    context = conflict_engine.ConflictWriteContext(
        identity=identity,
        evaluation_date=date(2026, 8, 19),
        legacy_bridge=(
            conflict_engine.LegacyConflictBridge.FULLY_UNOWNED
            if legacy_bridge
            else conflict_engine.LegacyConflictBridge.REJECT
        ),
    )
    return await conflict_engine.prepare_scoped_write(session, context=context)


async def _add_owned_supplement(session, identity: WriteIdentity, **kwargs):
    prepared = await _prepared_supplement_write(session, identity)
    return await supplements_service.add_supplement(
        session,
        identity=identity,
        prepared_conflict_write=prepared,
        **kwargs,
    )


def test_derived_timeline_legacy_root_registry_is_exhaustive():
    root_names = {
        "actor_user_id",
        "integration_connection_id",
        "file_asset_id",
    }
    for selector, model in _DERIVED_MODELS.items():
        actual = {name for name in root_names if hasattr(model, name)}
        assert actual == _DERIVED_NEW_ROOTS[selector]
        raw_links = {
            name
            for name in ("raw_id", "raw_payload_id")
            if hasattr(model, name)
        }
        expected_raw = (
            {_DERIVED_RAW_LINKS[selector]}
            if selector in _DERIVED_RAW_LINKS
            else set()
        )
        assert raw_links == expected_raw


@pytest.mark.parametrize("selector", _DERIVED_TIMELINE_SELECTORS)
async def test_each_derived_timeline_selector_rejects_cross_subject_rows(
    db_session,
    selector,
):
    owner = await _identity(db_session, f"derived-owner-{selector}")
    foreign = await _identity(db_session, f"derived-foreign-{selector}")
    owner_row = _derived_row(
        selector,
        subject_id=owner.subject_id,
        ordinal=0,
    )
    foreign_row = _derived_row(
        selector,
        subject_id=foreign.subject_id,
        ordinal=1,
    )
    await _complete_progress_photo_graph(db_session, owner_row, owner)
    await _complete_progress_photo_graph(db_session, foreign_row, foreign)
    db_session.add_all([owner_row, foreign_row])
    await db_session.flush()

    owner_refs = {
        event.ref
        for event in await timeline_service.list_events(
            db_session,
            subject_id=owner.subject_id,
        )
    }
    foreign_refs = {
        event.ref
        for event in await timeline_service.list_events(
            db_session,
            subject_id=foreign.subject_id,
        )
    }

    assert _derived_ref(selector, owner_row) in owner_refs
    assert _derived_ref(selector, foreign_row) not in owner_refs
    assert _derived_ref(selector, foreign_row) in foreign_refs
    assert _derived_ref(selector, owner_row) not in foreign_refs


@pytest.mark.parametrize("selector", _DERIVED_TIMELINE_SELECTORS)
async def test_each_derived_timeline_selector_requires_explicit_legacy_null_bridge(
    db_session,
    selector,
):
    owner = await _identity(db_session, f"derived-legacy-{selector}")
    owner_row = _derived_row(
        selector,
        subject_id=owner.subject_id,
        ordinal=0,
    )
    legacy_row = _derived_row(
        selector,
        subject_id=None,
        ordinal=2,
    )
    await _complete_progress_photo_graph(db_session, owner_row, owner)
    db_session.add_all([owner_row, legacy_row])
    await db_session.flush()

    scoped_refs = {
        event.ref
        for event in await timeline_service.list_events(
            db_session,
            subject_id=owner.subject_id,
        )
    }
    compatibility_refs = {
        event.ref
        for event in await timeline_service.list_events(
            db_session,
            subject_id=owner.subject_id,
            include_legacy_unowned=True,
        )
    }

    assert _derived_ref(selector, owner_row) in scoped_refs
    assert _derived_ref(selector, legacy_row) not in scoped_refs
    assert _derived_ref(selector, owner_row) in compatibility_refs
    assert _derived_ref(selector, legacy_row) in compatibility_refs


@pytest.mark.parametrize("selector", _DERIVED_TIMELINE_SELECTORS)
async def test_derived_timeline_bridge_rejects_partial_actor_roots(
    db_session,
    selector,
):
    owner = await _identity(db_session, f"derived-partial-owner-{selector}")
    foreign = await _identity(db_session, f"derived-partial-foreign-{selector}")
    partial = _derived_row(selector, subject_id=None, ordinal=3)
    partial.actor_user_id = foreign.actor_user_id
    db_session.add(partial)
    await db_session.flush()

    if selector in {"progress_photo", "milestone"}:
        expected_error = (
            weight_service.ProgressPhotoOwnershipError
            if selector == "progress_photo"
            else milestones_service.MilestoneOwnershipError
        )
        with pytest.raises(expected_error):
            await timeline_service.list_events(
                db_session,
                subject_id=owner.subject_id,
                include_legacy_unowned=True,
            )
        return

    refs = {
        event.ref
        for event in await timeline_service.list_events(
            db_session,
            subject_id=owner.subject_id,
            include_legacy_unowned=True,
        )
    }

    assert _derived_ref(selector, partial) not in refs


@pytest.mark.parametrize(
    ("selector", "purpose"),
    (
        ("body_scan", FileAssetPurpose.BODY_SCAN_DOCUMENT.value),
        ("progress_photo", FileAssetPurpose.PROGRESS_PHOTO.value),
    ),
)
async def test_derived_timeline_bridge_rejects_partial_file_roots(
    db_session,
    selector,
    purpose,
):
    owner = await _identity(db_session, f"derived-file-owner-{selector}")
    foreign = await _identity(db_session, f"derived-file-foreign-{selector}")
    asset = FileAsset(
        subject_id=foreign.subject_id,
        purpose=purpose,
        storage_backend=FileStorageBackend.LEGACY_LOCAL.value,
        storage_ref=f"timeline-partial/{selector}",
        status=FileAssetStatus.LEGACY_PLACEHOLDER.value,
    )
    db_session.add(asset)
    await db_session.flush()
    partial = _derived_row(selector, subject_id=None, ordinal=4)
    partial.file_asset_id = asset.id
    db_session.add(partial)
    await db_session.flush()

    if selector == "progress_photo":
        with pytest.raises(weight_service.ProgressPhotoOwnershipError):
            await timeline_service.list_events(
                db_session,
                subject_id=owner.subject_id,
                include_legacy_unowned=True,
            )
        return

    refs = {
        event.ref
        for event in await timeline_service.list_events(
            db_session,
            subject_id=owner.subject_id,
            include_legacy_unowned=True,
        )
    }

    assert _derived_ref(selector, partial) not in refs


@pytest.mark.parametrize("selector", tuple(_DERIVED_RAW_LINKS))
async def test_derived_timeline_bridge_validates_linked_raw_ownership(
    db_session,
    selector,
):
    owner = await _identity(db_session, f"derived-raw-owner-{selector}")
    foreign = await _identity(db_session, f"derived-raw-foreign-{selector}")
    foreign_raw = RawPayload(
        subject_id=foreign.subject_id,
        actor_user_id=foreign.actor_user_id,
        domain=Domain.TIMELINE.value,
        source=Source.MANUAL.value,
        external_id=f"partial-{selector}",
        payload={"synthetic": True},
    )
    legacy_raw = RawPayload(
        domain=Domain.TIMELINE.value,
        source=Source.MANUAL.value,
        external_id=f"legacy-{selector}",
        payload={"synthetic": True},
    )
    db_session.add_all([foreign_raw, legacy_raw])
    await db_session.flush()
    partial = _derived_row(selector, subject_id=None, ordinal=5)
    legacy = _derived_row(selector, subject_id=None, ordinal=6)
    setattr(partial, _DERIVED_RAW_LINKS[selector], foreign_raw.id)
    setattr(legacy, _DERIVED_RAW_LINKS[selector], legacy_raw.id)
    db_session.add_all([partial, legacy])
    await db_session.flush()

    refs = {
        event.ref
        for event in await timeline_service.list_events(
            db_session,
            subject_id=owner.subject_id,
            include_legacy_unowned=True,
        )
    }

    assert _derived_ref(selector, partial) not in refs
    assert _derived_ref(selector, legacy) in refs


async def test_direct_legacy_bridge_rejects_partial_actor_roots(db_session):
    owner = await _identity(db_session, "direct-partial-owner")
    foreign = await _identity(db_session, "direct-partial-foreign")
    annotation = Annotation(
        actor_user_id=foreign.actor_user_id,
        date=date(2026, 8, 18),
        domain=Domain.TIMELINE.value,
        source=Source.MANUAL.value,
        kind="note",
        title="Partial annotation",
    )
    supplement = Supplement(
        actor_user_id=foreign.actor_user_id,
        domain=Domain.SUPPLEMENTS.value,
        source=Source.MANUAL.value,
        name="Partial supplement",
        key="partial_supplement",
    )
    db_session.add_all([annotation, supplement])
    await db_session.flush()

    assert await timeline_service.get_annotation(
        db_session,
        annotation.id,
        subject_id=owner.subject_id,
        include_legacy_unowned=True,
    ) is None
    assert await timeline_service.update_annotation(
        db_session,
        annotation.id,
        title="Must not mutate",
        on_date=annotation.date,
        kind=annotation.kind,
        domain=annotation.domain,
        identity=owner,
        include_legacy_unowned=True,
    ) is None
    assert await timeline_service.delete_annotation(
        db_session,
        annotation.id,
        identity=owner,
        include_legacy_unowned=True,
    ) is False
    assert await supplements_service.get_supplement(
        db_session,
        supplement.id,
        subject_id=owner.subject_id,
        include_legacy_unowned=True,
    ) is None
    partial_prepared = await _prepared_supplement_write(db_session, owner)
    with pytest.raises(conflict_engine.ConflictPreparedWriteError):
        await supplements_service.set_active(
            db_session,
            supplement.id,
            False,
            identity=owner,
            include_legacy_unowned=True,
            prepared_conflict_write=partial_prepared,
        )
    assert await supplements_service.delete_supplement(
        db_session,
        supplement.id,
        identity=owner,
        include_legacy_unowned=True,
    ) is False
    assert annotation.subject_id is None and annotation.title == "Partial annotation"
    assert supplement.subject_id is None and supplement.active is True


async def test_owned_creates_and_updates_preserve_provenance(db_session):
    identity = await _identity(db_session, "ownership-provenance")

    annotation = await timeline_service.create_annotation(
        db_session,
        title="Private annotation",
        on_date=date(2026, 8, 19),
        source=Source.MCP.value,
        identity=identity,
    )
    supplement = await _add_owned_supplement(
        db_session,
        identity,
        name="Creatine",
        source=Source.MCP.value,
    )

    assert (annotation.subject_id, annotation.actor_user_id, annotation.source) == (
        identity.subject_id,
        identity.actor_user_id,
        Source.MCP.value,
    )
    assert (supplement.subject_id, supplement.actor_user_id, supplement.source) == (
        identity.subject_id,
        identity.actor_user_id,
        Source.MCP.value,
    )

    system_identity = WriteIdentity(identity.subject_id, None)
    system_prepared = await _prepared_supplement_write(
        db_session,
        system_identity,
    )
    await timeline_service.update_annotation(
        db_session,
        annotation.id,
        title="Updated annotation",
        on_date=annotation.date,
        kind=annotation.kind,
        domain=annotation.domain,
        identity=system_identity,
    )
    await supplements_service.update_supplement(
        db_session,
        supplement.id,
        name="Creatine monohydrate",
        dose=supplement.dose,
        timing=supplement.timing,
        evidence=supplement.evidence,
        active=supplement.active,
        contraindications=supplement.contraindications,
        note=supplement.note,
        identity=system_identity,
        prepared_conflict_write=system_prepared,
    )

    assert annotation.actor_user_id == identity.actor_user_id
    assert supplement.actor_user_id == identity.actor_user_id
    assert annotation.source == Source.MCP.value
    assert supplement.source == Source.MCP.value


async def test_crud_and_timeline_feed_reject_cross_subject_ids(db_session):
    first = await _identity(db_session, "ownership-first")
    second = await _identity(db_session, "ownership-second")
    annotation = await timeline_service.create_annotation(
        db_session,
        title="First subject annotation",
        on_date=date(2026, 8, 19),
        identity=first,
    )
    supplement = await _add_owned_supplement(
        db_session,
        first,
        name="First subject supplement",
    )

    assert await timeline_service.get_annotation(
        db_session, annotation.id, subject_id=second.subject_id
    ) is None
    assert await timeline_service.update_annotation(
        db_session,
        annotation.id,
        title="Forged",
        on_date=annotation.date,
        kind=annotation.kind,
        domain=annotation.domain,
        identity=second,
    ) is None
    assert await timeline_service.delete_annotation(
        db_session, annotation.id, identity=second
    ) is False

    assert await supplements_service.get_supplement(
        db_session, supplement.id, subject_id=second.subject_id
    ) is None
    second_prepared = await _prepared_supplement_write(db_session, second)
    assert await supplements_service.update_supplement(
        db_session,
        supplement.id,
        name="Forged",
        active=False,
        identity=second,
        prepared_conflict_write=second_prepared,
    ) is None
    assert await supplements_service.set_active(
        db_session,
        supplement.id,
        False,
        identity=second,
        prepared_conflict_write=second_prepared,
    ) is None
    assert await supplements_service.delete_supplement(
        db_session, supplement.id, identity=second
    ) is False

    first_events = await timeline_service.list_events(
        db_session, subject_id=first.subject_id
    )
    second_events = await timeline_service.list_events(
        db_session, subject_id=second.subject_id
    )
    assert len(first_events) == 2
    assert any(event.title == "First subject annotation" for event in first_events)
    assert any(
        "First subject supplement" in event.title for event in first_events
    )
    assert second_events == []
    assert annotation.title == "First subject annotation"
    assert supplement.name == "First subject supplement"
    assert supplement.active is True


async def test_legacy_null_rows_need_explicit_sole_subject_bridge(db_session):
    identity = await _identity(db_session, "ownership-legacy")
    legacy_annotation = Annotation(
        date=date(2026, 8, 18),
        domain="timeline",
        source=Source.MANUAL.value,
        kind="note",
        title="Legacy annotation",
    )
    legacy_supplement = Supplement(
        domain="supplements",
        source=Source.MANUAL.value,
        name="Legacy supplement",
        key="legacy_supplement",
    )
    db_session.add_all([legacy_annotation, legacy_supplement])
    await db_session.flush()

    assert list(
        await timeline_service.list_annotations(
            db_session, subject_id=identity.subject_id
        )
    ) == []
    assert list(
        await supplements_service.list_supplements(
            db_session, subject_id=identity.subject_id
        )
    ) == []
    assert list(
        await timeline_service.list_annotations(
            db_session,
            subject_id=identity.subject_id,
            include_legacy_unowned=True,
        )
    ) == [legacy_annotation]
    assert list(
        await supplements_service.list_supplements(
            db_session,
            subject_id=identity.subject_id,
            include_legacy_unowned=True,
        )
    ) == [legacy_supplement]

    await timeline_service.update_annotation(
        db_session,
        legacy_annotation.id,
        title="Adopted annotation",
        on_date=legacy_annotation.date,
        kind=legacy_annotation.kind,
        domain=legacy_annotation.domain,
        identity=identity,
        include_legacy_unowned=True,
    )
    legacy_prepared = await _prepared_supplement_write(
        db_session,
        identity,
        legacy_bridge=True,
    )
    await supplements_service.set_active(
        db_session,
        legacy_supplement.id,
        False,
        identity=identity,
        include_legacy_unowned=True,
        prepared_conflict_write=legacy_prepared,
    )
    assert legacy_annotation.subject_id == identity.subject_id
    assert legacy_supplement.subject_id == identity.subject_id
    assert legacy_annotation.actor_user_id is None
    assert legacy_supplement.actor_user_id is None


async def test_web_creates_use_authenticated_owner_identity(auth_client, db_session):
    timeline_response = await auth_client.post(
        "/timeline",
        data={"title": "Owned web event", "date": "2026-08-19"},
    )
    supplement_response = await auth_client.post(
        "/supplements/save",
        data={"name": "Owned web supplement", "active": "true"},
    )

    assert timeline_response.status_code == 303
    assert supplement_response.status_code == 303
    annotation = await db_session.scalar(
        select(Annotation).where(Annotation.title == "Owned web event")
    )
    supplement = await db_session.scalar(
        select(Supplement).where(Supplement.name == "Owned web supplement")
    )
    subject = await db_session.scalar(select(HealthSubject))
    assert annotation is not None and supplement is not None and subject is not None
    assert (annotation.subject_id, annotation.actor_user_id, annotation.source) == (
        subject.id,
        subject.owner_user_id,
        Source.MANUAL.value,
    )
    assert (supplement.subject_id, supplement.actor_user_id, supplement.source) == (
        subject.id,
        subject.owner_user_id,
        Source.MANUAL.value,
    )


async def test_web_sole_subject_bridge_keeps_legacy_null_rows_working(
    auth_client, db_session
):
    annotation = Annotation(
        date=date(2026, 8, 17),
        domain="timeline",
        source=Source.MANUAL.value,
        kind="note",
        title="Legacy web annotation",
    )
    supplement = Supplement(
        domain="supplements",
        source=Source.MANUAL.value,
        name="Legacy web supplement",
        key="legacy_web_supplement",
        active=True,
    )
    db_session.add_all([annotation, supplement])
    await db_session.commit()

    timeline_page = await auth_client.get(
        "/timeline", headers={"Accept": "text/html"}
    )
    supplements_page = await auth_client.get(
        "/supplements", headers={"Accept": "text/html"}
    )
    assert timeline_page.status_code == 200
    assert supplements_page.status_code == 200
    assert "Legacy web annotation" in timeline_page.text
    assert "Legacy web supplement" in supplements_page.text

    toggle = await auth_client.post(
        f"/supplements/{supplement.id}/toggle", data={"active": "false"}
    )
    delete = await auth_client.post(f"/timeline/{annotation.id}/delete")
    assert toggle.status_code == 303
    assert delete.status_code == 303
    await db_session.refresh(supplement)
    subject = await db_session.scalar(select(HealthSubject))
    assert subject is not None
    assert supplement.subject_id == subject.id
    assert supplement.actor_user_id is None
    assert supplement.active is False
    assert await db_session.get(Annotation, annotation.id) is None


async def test_web_legacy_owner_bridge_closes_with_second_subject(
    auth_client, db_session
):
    second = await _identity(db_session, "web-second-subject")
    foreign = await _add_owned_supplement(
        db_session,
        second,
        name="Foreign supplement",
    )
    await db_session.commit()

    response = await auth_client.post(
        f"/supplements/{foreign.id}/toggle", data={"active": "false"}
    )
    # Global module-state resolution also uses the sole-subject bridge. With two
    # subjects it falls back to optional-off, so the request is rejected before
    # the route can dereference the client-controlled ID.
    assert response.status_code == 404
    await db_session.refresh(foreign)
    assert foreign.active is True


async def test_mcp_v1_stamps_owner_and_mcp_source(
    db_session, session_factory, legacy_owner_roots, monkeypatch
):
    from vitals.services import modules_service

    monkeypatch.setattr(mcp_router, "get_session_factory", lambda: session_factory)
    for key in ("timeline", "supplements"):
        await modules_service.set_module_enabled(
            db_session,
            key=key,
            enabled=True,
            subject_id=legacy_owner_roots.subject_id,
        )
    await db_session.commit()

    event_payload = await mcp_router.log_event(
        title="Owned MCP event", on_date="2026-08-19"
    )
    supplement_payload = await mcp_router.add_supplement(name="Owned MCP supplement")
    annotation = await db_session.get(Annotation, event_payload["id"])
    supplement = await db_session.get(Supplement, supplement_payload["id"])

    assert annotation is not None and supplement is not None
    assert (annotation.subject_id, annotation.actor_user_id, annotation.source) == (
        legacy_owner_roots.subject_id,
        legacy_owner_roots.user_id,
        Source.MCP.value,
    )
    assert (supplement.subject_id, supplement.actor_user_id, supplement.source) == (
        legacy_owner_roots.subject_id,
        legacy_owner_roots.user_id,
        Source.MCP.value,
    )

    await mcp_router.update_event(annotation.id, title="Updated MCP event")
    await mcp_router.update_supplement(
        supplement.id, name="Updated MCP supplement"
    )
    await db_session.refresh(annotation)
    await db_session.refresh(supplement)
    assert annotation.actor_user_id == legacy_owner_roots.user_id
    assert supplement.actor_user_id == legacy_owner_roots.user_id
    assert annotation.source == Source.MCP.value
    assert supplement.source == Source.MCP.value


async def test_mcp_v1_fails_closed_before_cross_subject_id_use(
    db_session, session_factory, legacy_owner_roots, monkeypatch
):
    from vitals.services import modules_service

    monkeypatch.setattr(mcp_router, "get_session_factory", lambda: session_factory)
    for key in ("timeline", "supplements"):
        await modules_service.set_module_enabled(
            db_session,
            key=key,
            enabled=True,
            subject_id=legacy_owner_roots.subject_id,
        )
    second = await _identity(db_session, "mcp-second-subject")
    foreign = await _add_owned_supplement(
        db_session,
        second,
        name="MCP foreign supplement",
    )
    await db_session.commit()

    with pytest.raises(LegacySubjectResolutionError):
        await mcp_router.set_supplement_active(foreign.id, active=False)
    await db_session.refresh(foreign)
    assert foreign.active is True


_MUTATORS = {
    "create_annotation",
    "update_annotation",
    "delete_annotation",
    "add_supplement",
    "update_supplement",
    "set_active",
    "delete_supplement",
}
_READS = {
    "get_annotation",
    "list_annotations",
    "list_events",
    "overlays_for",
    "get_supplement",
    "list_supplements",
    "resolve_active",
}

# These composition readers pre-date AccessContext propagation. Keeping the
# allowlist literal makes the remaining PR-04/report cutover debt visible and
# prevents a new unscoped reader from entering unnoticed. The exact assertion
# also forces a scoped migration to remove its stale exception immediately.
_KNOWN_LEGACY_READERS = {
    ("vitals/services/today_service.py", "build", "list_events"),
    ("vitals/services/digest_service.py", "assemble_context", "list_supplements"),
    ("vitals/services/digest_service.py", "assemble_context", "list_annotations"),
    ("vitals/services/share_service.py", "_supplements_block", "list_supplements"),
}


def _production_calls():
    repo_root = Path(__file__).resolve().parents[1]
    for top in ("vitals", "web"):
        for path in (repo_root / top).rglob("*.py"):
            relative = path.relative_to(repo_root).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            function_stack: list[str] = []

            class Visitor(ast.NodeVisitor):
                def visit_FunctionDef(self, node):
                    function_stack.append(node.name)
                    self.generic_visit(node)
                    function_stack.pop()

                visit_AsyncFunctionDef = visit_FunctionDef

                def visit_Call(self, node):
                    if isinstance(node.func, ast.Attribute):
                        callee = node.func.attr
                    elif isinstance(node.func, ast.Name):
                        callee = node.func.id
                    else:
                        callee = ""
                    if callee in _MUTATORS | _READS:
                        yield_item.append(
                            (
                                relative,
                                function_stack[-1] if function_stack else "<module>",
                                callee,
                                {kw.arg for kw in node.keywords if kw.arg},
                            )
                        )
                    self.generic_visit(node)

            yield_item: list[tuple[str, str, str, set[str]]] = []
            Visitor().visit(tree)
            yield from yield_item


def test_production_callsite_inventory_requires_owned_boundaries():
    calls = list(_production_calls())
    missing_identity = [
        (path, function, callee)
        for path, function, callee, keywords in calls
        if callee in _MUTATORS and "identity" not in keywords
    ]
    assert missing_identity == []

    unscoped_reads = {
        (path, function, callee)
        for path, function, callee, keywords in calls
        if callee in _READS and "subject_id" not in keywords
    }
    assert unscoped_reads == _KNOWN_LEGACY_READERS

    source = (Path(__file__).resolve().parents[1] / "web/routers/mcp.py").read_text(
        encoding="utf-8"
    )
    assert 'if domain in {"timeline", "supplements"}:' in source
    assert '"identity": ownership.owner_action()' in source
