"""Cross-surface boundary contracts for the Labs Stage-2 ownership cutover."""

from __future__ import annotations

from tests.job_runner import run_job_for_every_subject

import asyncio
from datetime import date
from io import BytesIO
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.datastructures import Headers, UploadFile
from starlette.requests import Request

from vitals.integrations.llm_client import LLMCallResult
from vitals.enums import (
    Domain,
    FileAssetPurpose,
    IntegrationConnectionStatus,
    IntegrationProvider,
    RuleType,
    Severity,
    Source,
)
from vitals.models.conflict_rule import ConflictRule
from vitals.models.labs import LabMarker, LabResult
from vitals.models.raw_payload import RawPayload
from vitals.models.tenancy import FileAsset, IntegrationConnection
from vitals.ownership import WriteIdentity
from vitals.services import (
    conflict_engine,
    conflict_registrations,
    file_asset_service,
    garmin_service,
    hevy_service,
    labs_service,
    raw_payload_service,
    supplements_service,
)
from vitals.services.body_scan import scans


RESULT_DATE = date(2026, 8, 19)
BOUNDARY_DATE = date(2026, 8, 20)


def _identity(legacy_owner_roots, *, system: bool = False) -> WriteIdentity:
    return WriteIdentity(
        legacy_owner_roots.subject_id,
        None if system else legacy_owner_roots.user_id,
    )


def _context(
    identity: WriteIdentity,
    *,
    on_date: date = RESULT_DATE,
    legacy: bool = False,
) -> conflict_engine.ConflictWriteContext:
    return conflict_engine.ConflictWriteContext(
        identity=identity,
        evaluation_date=on_date,
        legacy_bridge=(
            conflict_engine.LegacyConflictBridge.FULLY_UNOWNED
            if legacy
            else conflict_engine.LegacyConflictBridge.REJECT
        ),
    )


async def _prepared(session: AsyncSession, context):
    return await conflict_engine.prepare_scoped_write(session, context=context)


async def _openrouter_connection(
    session: AsyncSession, subject_id
) -> IntegrationConnection:
    connection = await session.scalar(
        select(IntegrationConnection).where(
            IntegrationConnection.subject_id == subject_id,
            IntegrationConnection.provider == IntegrationProvider.OPENROUTER.value,
        )
    )
    assert connection is not None
    return connection


def _upload() -> UploadFile:
    return UploadFile(
        BytesIO(b"\x89PNG\r\n\x1a\nsynthetic-lab-document"),
        filename="panel.png",
        headers=Headers({"content-type": "image/png"}),
    )


async def test_mcp_batch_keeps_exact_raw_and_normalized_mcp_provenance(
    db_session,
    session_factory,
    legacy_owner_roots,
    monkeypatch,
):
    mcp_router = pytest.importorskip("web.routers.mcp")
    monkeypatch.setattr(mcp_router, "get_session_factory", lambda: session_factory)

    response = await mcp_router.log_lab_results(
        on_date=RESULT_DATE.isoformat(),
        lab_name="Synthetic Lab",
        results=[
            {"marker": "Ferritin", "value": 91, "unit": "ng/mL"},
            {"marker": "TSH", "value": 2.2, "unit": "mIU/L"},
        ],
    )

    assert response["created"] == 2
    raw = await db_session.scalar(
        select(RawPayload).where(RawPayload.source == Source.MCP.value)
    )
    assert raw is not None
    assert (
        raw.subject_id,
        raw.actor_user_id,
        raw.integration_connection_id,
        raw.file_asset_id,
        raw.domain,
        raw.source,
    ) == (
        legacy_owner_roots.subject_id,
        legacy_owner_roots.user_id,
        None,
        None,
        Domain.LABS.value,
        Source.MCP.value,
    )
    results = list(
        await db_session.scalars(
            select(LabResult).order_by(LabResult.marker)
        )
    )
    assert len(results) == 2
    assert {
        (row.subject_id, row.actor_user_id, row.source, row.raw_payload_id)
        for row in results
    } == {
        (
            legacy_owner_roots.subject_id,
            legacy_owner_roots.user_id,
            Source.MCP.value,
            raw.id,
        )
    }


async def test_mcp_update_preserves_omitted_date_and_provenance_and_structures_block(
    db_session,
    session_factory,
    legacy_owner_roots,
    monkeypatch,
    owner_write,
):
    mcp_router = pytest.importorskip("web.routers.mcp")
    monkeypatch.setattr(mcp_router, "get_session_factory", lambda: session_factory)
    conflict_registrations.register_all_resolvers()
    db_session.add(
        ConflictRule(
            rule_type=RuleType.HARD_BLOCK.value,
            domain_a=Domain.SUPPLEMENTS.value,
            condition_a={"key": "potassium", "active": True},
            domain_b=Domain.LABS.value,
            condition_b={"marker": "Potassium", "value": {"$gt": 5.0}},
            severity=Severity.BLOCK.value,
            message="Synthetic potassium block.",
            active=True,
        )
    )
    await supplements_service.add_supplement(
        db_session,
        name="Synthetic potassium",
        key="potassium",
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write()
    )
    await db_session.commit()

    created = await mcp_router.log_lab_result(
        marker="Potassium",
        value=4.2,
        on_date=RESULT_DATE.isoformat(),
        unit="mmol/L",
        ref_low=3.5,
        ref_high=5.1,
        lab_name="Synthetic Lab",
        note="original note",
    )
    row = await db_session.get(LabResult, created["id"])
    assert row is not None
    raw = await db_session.get(RawPayload, row.raw_payload_id)
    assert raw is not None
    assert (
        raw.subject_id,
        raw.actor_user_id,
        raw.integration_connection_id,
        raw.file_asset_id,
        raw.domain,
        raw.source,
        raw.processed_at is not None,
    ) == (
        legacy_owner_roots.subject_id,
        legacy_owner_roots.user_id,
        None,
        None,
        Domain.LABS.value,
        Source.MCP.value,
        True,
    )
    provenance = (
        row.subject_id,
        row.actor_user_id,
        row.source,
        row.raw_payload_id,
    )

    blocked = await mcp_router.update_lab_result(row.id, value=5.5)
    assert blocked["blocked"] is True
    assert blocked["violations"][0]["message"] == "Synthetic potassium block."
    assert blocked["hint"] == (
        "Retry the same call with override=True to save anyway."
    )
    await db_session.refresh(row)
    assert row.value == 4.2

    updated = await mcp_router.update_lab_result(
        row.id,
        value=5.5,
        override=True,
    )
    await db_session.refresh(row)
    assert updated["date"] == RESULT_DATE.isoformat()
    assert (
        updated["marker"],
        updated["unit"],
        updated["ref_low"],
        updated["ref_high"],
        updated["lab_name"],
        updated["note"],
    ) == (
        "Potassium",
        "mmol/L",
        3.5,
        5.1,
        "Synthetic Lab",
        "original note",
    )
    assert (
        row.subject_id,
        row.actor_user_id,
        row.source,
        row.raw_payload_id,
    ) == provenance


async def test_lab_upload_releases_preflight_transaction_before_llm(
    db_session,
    legacy_owner_roots,
    monkeypatch,
    platform_ai_ready,
):
    from web.routers import labs as labs_router

    observed = []

    async def extraction_probe(*args, **kwargs):
        del args, kwargs
        observed.append(db_session.in_transaction())
        raise RuntimeError("synthetic stop before persistence")

    monkeypatch.setattr(
        labs_service,
        "extract_from_file_with_usage",
        extraction_probe,
    )
    response = await labs_router.upload_document(
        request=Request(
            {"type": "http", "method": "POST", "path": "/labs/upload", "headers": []}
        ),
        file=_upload(),
        db=db_session,
        username="tester",
    )

    assert observed == [False]
    assert response.status_code == 200
    assert b'"reason":"error"' in response.body
    assert await db_session.scalar(select(func.count()).select_from(FileAsset)) == 1
    assert await db_session.scalar(select(func.count()).select_from(RawPayload)) == 1


@pytest.mark.parametrize(
    "connection_status",
    [IntegrationConnectionStatus.PENDING, IntegrationConnectionStatus.DISABLED],
)
async def test_lab_upload_ignores_inactive_historical_subject_openrouter(
    db_session,
    legacy_owner_roots,
    monkeypatch,
    connection_status,
    platform_ai_ready,
):
    from web.routers import labs as labs_router

    connection = await _openrouter_connection(
        db_session, legacy_owner_roots.subject_id
    )
    connection.status = connection_status.value
    await db_session.commit()
    calls = []

    async def extraction_probe(*args, **kwargs):
        del args, kwargs
        calls.append("llm")
        return LLMCallResult(
            value={"date": RESULT_DATE.isoformat(), "lab_name": None, "results": []},
            upstream_request_id="subject-c-is-historical-only",
            model="synthetic/model",
            input_tokens=1,
            output_tokens=1,
            cost_microunits=1,
        )

    monkeypatch.setattr(
        labs_service,
        "extract_from_file_with_usage",
        extraction_probe,
    )
    response = await labs_router.upload_document(
        request=Request(
            {"type": "http", "method": "POST", "path": "/labs/upload", "headers": []}
        ),
        file=_upload(),
        db=db_session,
        username="tester",
    )
    assert response.status_code == 200
    assert b'"ok":true' in response.body
    assert calls == ["llm"]


@pytest.mark.parametrize(
    ("process_mode", "scheduler_started"),
    [
        ("combined", True),
        ("web", False),
    ],
)
async def test_startup_hormone_seed_receives_one_subject_system_capability(
    db_session,
    session_factory,
    legacy_owner_roots,
    monkeypatch,
    process_mode,
    scheduler_started,
):
    from vitals.process_mode import ProcessMode
    from vitals.scheduler import jobs as jobs_module
    from vitals.scheduler import scheduler as scheduler_module
    from vitals.services import conflict_catalog
    from vitals.services.hrt import catalog, reminders
    from vitals.services.proactive import prefs
    from web import main as web_main

    async def no_op(*args, **kwargs):
        del args, kwargs

    calls = []

    async def seed_probe(
        session,
        *,
        identity,
        prepared_conflict_write,
    ):
        context = conflict_engine.require_prepared_identity(
            session,
            prepared=prepared_conflict_write,
            identity=identity,
        )
        # Labs is closed: the boundary carries the subject, not an escape hatch.
        calls.append((identity, identity.subject_id, context.legacy_bridge))
        return {"created": 0, "updated": 0}

    async def get_prefs_probe(session):
        del session
        return {}

    class SchedulerProbe:
        def __init__(self):
            self.started = False
            self.stopped = False

        def start(self):
            self.started = True

        def shutdown(self):
            self.stopped = True

    scheduler_probe = SchedulerProbe()
    monkeypatch.setattr(
        web_main,
        "load_process_mode",
        lambda: ProcessMode(process_mode),
    )
    monkeypatch.setattr(web_main, "get_session_factory", lambda: session_factory)
    monkeypatch.setattr(
        web_main,
        "get_redis_client",
        lambda: (_ for _ in ()).throw(RuntimeError("synthetic no redis")),
    )
    monkeypatch.setattr(jobs_module, "register_all_jobs", lambda settings: None)
    monkeypatch.setattr(prefs, "get_prefs", get_prefs_probe)
    monkeypatch.setattr(conflict_catalog, "sync_catalog", no_op)
    monkeypatch.setattr(catalog, "sync_catalog", no_op)
    monkeypatch.setattr(reminders, "seed_hormone_panel", seed_probe)
    monkeypatch.setattr(
        scheduler_module,
        "setup_scheduler",
        lambda *args, **kwargs: scheduler_probe,
    )
    app = SimpleNamespace(state=SimpleNamespace(mcp_lifespan=None))

    async with web_main.lifespan(app):
        assert scheduler_probe.started is scheduler_started
        assert (app.state.scheduler is scheduler_probe) is scheduler_started

    assert scheduler_probe.stopped is scheduler_started
    assert calls == [
        (
            WriteIdentity(legacy_owner_roots.subject_id, None),
            legacy_owner_roots.subject_id,
            conflict_engine.LegacyConflictBridge.FULLY_UNOWNED,
        )
    ]


async def test_nightly_labs_sweep_receives_system_identity_and_live_capability(
    db_session,
    session_factory,
    legacy_owner_roots,
    monkeypatch,
):
    calls = []

    async def no_op(*args, **kwargs):
        del args, kwargs
        return 0

    async def labs_probe(
        session,
        *,
        identity,
        prepared_conflict_write,
    ):
        context = conflict_engine.require_prepared_identity(
            session,
            prepared=prepared_conflict_write,
            identity=identity,
        )
        calls.append((identity, identity.subject_id, context.legacy_bridge))
        return 0

    monkeypatch.setattr(garmin_service, "reparse_owned_pending", no_op)
    monkeypatch.setattr(hevy_service, "reparse_owned_pending", no_op)
    monkeypatch.setattr(scans, "reparse_owned_pending", no_op)
    monkeypatch.setattr(labs_service, "reparse_owned_pending", labs_probe)

    await run_job_for_every_subject(raw_payload_service.sweep_pending_job, session_factory)

    assert calls == [
        (
            WriteIdentity(legacy_owner_roots.subject_id, None),
            legacy_owner_roots.subject_id,
            conflict_engine.LegacyConflictBridge.FULLY_UNOWNED,
        )
    ]


async def test_more_labs_read_uses_resolved_exact_subject_scope(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    from web.routers import more as more_router

    calls = []

    async def latest_probe(session, *, subject_id):
        assert session is db_session
        calls.append(subject_id)
        return []

    monkeypatch.setattr(labs_service, "latest_per_marker", latest_probe)
    monkeypatch.setattr(
        more_router.templates,
        "TemplateResponse",
        lambda request, name, context: context,
    )
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/more",
            "headers": [],
            "state": {"enabled_modules": {"labs": True}},
        }
    )

    context = await more_router.more_screen(
        request,
        db=db_session,
        username="tester",
    )

    assert calls == [legacy_owner_roots.subject_id]
    assert context["labs_out_of_range"] == 0


async def _owned_parser_raw(
    session: AsyncSession,
    *,
    identity: WriteIdentity,
    connection: IntegrationConnection,
    suffix: str,
    marker: str,
) -> RawPayload:
    storage_ref = f"labs/synthetic-{suffix}.png"
    asset = await file_asset_service.register_legacy_local(
        session,
        subject_id=identity.subject_id,
        uploaded_by_user_id=identity.actor_user_id,
        purpose=FileAssetPurpose.LAB_DOCUMENT,
        storage_ref=storage_ref,
        media_type="image/png",
        size_bytes=24,
        content_sha256=suffix[0] * 64,
    )
    return await raw_payload_service.upsert_owned_raw_payload(
        session,
        identity=identity,
        integration_connection_id=connection.id,
        file_asset_id=asset.id,
        domain=Domain.LABS.value,
        source=Source.LAB_PARSER.value,
        external_id=storage_ref,
        payload={
            "date": RESULT_DATE.isoformat(),
            "lab_name": "Synthetic Lab",
            "results": [{"marker": marker, "value": 10}],
        },
    )


async def test_owned_raw_replay_savepoint_isolates_partial_row_failure(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    human = _identity(legacy_owner_roots)
    connection = await _openrouter_connection(db_session, human.subject_id)
    first = await _owned_parser_raw(
        db_session,
        identity=human,
        connection=connection,
        suffix="a",
        marker="Failed marker",
    )
    second = await _owned_parser_raw(
        db_session,
        identity=human,
        connection=connection,
        suffix="b",
        marker="Successful marker",
    )
    first_id, second_id = first.id, second.id
    await db_session.commit()

    original_ingest = labs_service.ingest_extracted

    async def flaky_ingest(session, extracted, **kwargs):
        candidate = kwargs["existing_raw_payload"]
        if candidate.id == first_id:
            session.add(
                LabResult(
                    subject_id=human.subject_id,
                    actor_user_id=human.actor_user_id,
                    date=RESULT_DATE,
                    domain=Domain.LABS.value,
                    source=Source.LAB_PARSER.value,
                    marker="Partial marker",
                    value=1,
                    raw_payload_id=first_id,
                )
            )
            await session.flush()
            raise RuntimeError("synthetic failure after partial write")
        return await original_ingest(session, extracted, **kwargs)

    monkeypatch.setattr(labs_service, "ingest_extracted", flaky_ingest)
    system = _identity(legacy_owner_roots, system=True)
    prepared = await _prepared(
        db_session,
        _context(system, on_date=BOUNDARY_DATE),
    )

    assert await labs_service.reparse_owned_pending(
        db_session,
        identity=system,
        prepared_conflict_write=prepared,
    ) == 1
    assert await db_session.scalar(
        select(func.count()).select_from(LabResult).where(
            LabResult.raw_payload_id == first_id
        )
    ) == 0
    successful = await db_session.scalar(
        select(LabResult).where(LabResult.raw_payload_id == second_id)
    )
    assert successful is not None and successful.marker == "Successful marker"
    await db_session.refresh(first)
    await db_session.refresh(second)
    assert first.processed_at is None
    assert second.processed_at is not None


async def test_owned_raw_replay_scans_past_full_malformed_head_batch(
    db_session,
    legacy_owner_roots,
):
    """Malformed C-backed roots stay isolated without starving a later valid raw."""

    human = _identity(legacy_owner_roots)
    connection = await _openrouter_connection(db_session, human.subject_id)
    malformed = []
    for index in range(raw_payload_service.REPARSE_BATCH + 1):
        row = RawPayload(
            subject_id=human.subject_id,
            actor_user_id=human.actor_user_id,
            integration_connection_id=connection.id,
            file_asset_id=None,
            domain=Domain.LABS.value,
            source=Source.LAB_PARSER.value,
            external_id=f"labs/malformed-head-{index}.png",
            payload={
                "date": RESULT_DATE.isoformat(),
                "lab_name": "Malformed Synthetic Lab",
                "results": [{"marker": f"Bad {index}", "value": index + 1}],
            },
            processed_at=None,
        )
        db_session.add(row)
        malformed.append(row)
    await db_session.flush()
    valid = await _owned_parser_raw(
        db_session,
        identity=human,
        connection=connection,
        suffix="e-valid-after-malformed-head",
        marker="Valid after malformed head",
    )
    malformed_ids = [row.id for row in malformed]
    valid_id = valid.id
    await db_session.commit()

    system = _identity(legacy_owner_roots, system=True)
    prepared = await _prepared(
        db_session,
        _context(system, on_date=BOUNDARY_DATE),
    )
    done = await labs_service.reparse_owned_pending(
        db_session,
        identity=system,
        prepared_conflict_write=prepared,
        limit=raw_payload_service.REPARSE_BATCH,
    )

    assert done == 1
    result = await db_session.scalar(
        select(LabResult).where(LabResult.raw_payload_id == valid_id)
    )
    assert result is not None and result.marker == "Valid after malformed head"
    assert await db_session.scalar(
        select(func.count()).select_from(LabResult).where(
            LabResult.raw_payload_id.in_(malformed_ids)
        )
    ) == 0
    malformed_processed = list(
        await db_session.scalars(
            select(RawPayload.processed_at).where(RawPayload.id.in_(malformed_ids))
        )
    )
    assert malformed_processed == [None] * len(malformed_ids)


async def test_stage3a_parser_history_replays_without_becoming_live_upload(
    db_session,
    legacy_owner_roots,
):
    system = _identity(legacy_owner_roots, system=True)
    connection = await _openrouter_connection(db_session, system.subject_id)
    raw = RawPayload(
        subject_id=system.subject_id,
        actor_user_id=None,
        integration_connection_id=connection.id,
        file_asset_id=None,
        domain=Domain.LABS.value,
        source=Source.LAB_PARSER.value,
        external_id="labs/stage3a-history.png",
        payload={
            "date": RESULT_DATE.isoformat(),
            "lab_name": "Historical Synthetic Lab",
            "results": [{"marker": "Historical ferritin", "value": 72}],
        },
    )
    db_session.add(raw)
    await db_session.commit()

    live_prepared = await _prepared(
        db_session,
        _context(system, legacy=True),
    )
    with pytest.raises(
        conflict_engine.ConflictRawOwnershipError,
        match="no file root",
    ):
        await labs_service.add_result(
            db_session,
            on_date=RESULT_DATE,
            marker="Must not normalize live",
            value=1,
            source=Source.LAB_PARSER.value,
            raw_payload_id=raw.id,
            identity=system,
            prepared_conflict_write=live_prepared,
        )
    await db_session.rollback()

    prepared = await _prepared(
        db_session,
        _context(system, on_date=BOUNDARY_DATE, legacy=True),
    )
    assert await labs_service.reparse_owned_pending(
        db_session,
        identity=system,
        prepared_conflict_write=prepared,
    ) == 1
    result = await db_session.scalar(
        select(LabResult).where(LabResult.raw_payload_id == raw.id)
    )
    raw = await db_session.get(RawPayload, raw.id)
    assert result is not None
    assert raw is not None
    assert (
        result.subject_id,
        result.actor_user_id,
        result.source,
        raw.actor_user_id,
        raw.integration_connection_id,
        raw.file_asset_id,
        raw.processed_at is not None,
    ) == (
        system.subject_id,
        None,
        Source.LAB_PARSER.value,
        None,
        connection.id,
        None,
        True,
    )


@pytest.mark.integration
async def test_postgres_concurrent_first_marker_writes_serialize_on_subject_root(
    db_session,
    legacy_owner_roots,
):
    assert db_session.bind is not None
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    identity = _identity(legacy_owner_roots)
    context = _context(identity)
    await db_session.commit()

    session_a = factory()
    prepared_a = await _prepared(session_a, context)
    await labs_service.add_result(
        session_a,
        on_date=RESULT_DATE,
        marker="Concurrent marker",
        value=1,
        identity=identity,
        prepared_conflict_write=prepared_a,
    )

    async def write_b() -> None:
        async with factory() as session_b:
            prepared_b = await _prepared(session_b, context)
            await labs_service.add_result(
                session_b,
                on_date=RESULT_DATE,
                marker="Concurrent marker",
                value=2,
                identity=identity,
                prepared_conflict_write=prepared_b,
            )
            await session_b.commit()

    task_b = asyncio.create_task(write_b())
    await asyncio.sleep(0.25)
    assert not task_b.done(), "writer B must wait on the subject/root lock"
    await session_a.commit()
    await session_a.close()
    await asyncio.wait_for(task_b, timeout=5)

    async with factory() as verify:
        markers = list(
            await verify.scalars(
                select(LabMarker).where(
                    LabMarker.subject_id == identity.subject_id,
                    LabMarker.name == "Concurrent marker",
                )
            )
        )
        results = list(
            await verify.scalars(
                select(LabResult).where(
                    LabResult.subject_id == identity.subject_id,
                    LabResult.marker == "Concurrent marker",
                )
            )
        )
    assert len(markers) == 1
    assert sorted(row.value for row in results) == [1, 2]
