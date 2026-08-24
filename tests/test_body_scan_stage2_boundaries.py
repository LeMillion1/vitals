"""Stage-2 ownership and transaction boundaries for BodyScan."""

from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta
from io import BytesIO

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.datastructures import Headers, UploadFile
from starlette.requests import Request

from vitals.integrations.llm_client import LLMCallResult
from vitals.enums import (
    Domain,
    FileAssetPurpose,
    FileAssetStatus,
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
    RuleType,
    Severity,
    Source,
    UserStatus,
)
from vitals.models.body_scan import BodyScan, BodyScanMetric
from vitals.models.conflict_rule import ConflictRule
from vitals.models.identity import HealthSubject, User
from vitals.models.raw_payload import RawPayload
from vitals.models.system_alert import SystemAlert
from vitals.models.tenancy import FileAsset, IntegrationConnection
from vitals.models.weight import WeightLog
from vitals.ownership import WriteIdentity
from vitals.services import (
    body_scan_service,
    conflict_engine,
    file_asset_service,
    raw_payload_service,
    weight_service,
)
from vitals.services import modules_service
from vitals.utils.timeutils import now_local


# These tests seed rows with no owner on purpose: they pin what a scoped
# reader or writer does when the ownership backfill has not reached a row yet,
# which is a state the application itself can no longer create. The schema
# says so, so this module asks for the one that stood before the contract.
pytestmark = pytest.mark.pre_ownership_contract


SCAN_DATE = date(2026, 8, 20)
NEXT_DATE = date(2026, 8, 21)


def _identity(legacy_owner_roots, *, system: bool = False) -> WriteIdentity:
    return WriteIdentity(
        legacy_owner_roots.subject_id,
        None if system else legacy_owner_roots.user_id,
    )


def _context(
    identity: WriteIdentity,
    *,
    on_date: date = SCAN_DATE,
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


async def _prepared_weight(
    session: AsyncSession,
    identity: WriteIdentity,
    *,
    on_date: date = SCAN_DATE,
    legacy: bool = False,
):
    return await weight_service.prepare_weight_write(
        session,
        context=_context(identity, on_date=on_date, legacy=legacy),
    )


async def _enable_body_comp(session: AsyncSession, legacy_owner_roots) -> None:
    subject_ids = list(
        await session.scalars(select(HealthSubject.id).order_by(HealthSubject.id))
    )
    assert len(subject_ids) == 1
    await modules_service.set_module_enabled(
        session,
        key="body_comp",
        enabled=True,
        subject_id=subject_ids[0],
    )
    await session.commit()


async def _new_owner(
    session: AsyncSession,
    slug: str,
) -> tuple[User, HealthSubject, WriteIdentity, IntegrationConnection]:
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
    connection = IntegrationConnection(
        subject_id=subject.id,
        provider=IntegrationProvider.OPENROUTER.value,
        connection_type=IntegrationConnectionType.AI_GATEWAY.value,
        external_account_discriminator=f"synthetic-{slug}",
        status=IntegrationConnectionStatus.ACTIVE.value,
    )
    session.add(connection)
    await session.flush()
    return user, subject, WriteIdentity(subject.id, user.id), connection


async def _openrouter_connection(
    session: AsyncSession,
    subject_id,
) -> IntegrationConnection:
    connection = await session.scalar(
        select(IntegrationConnection).where(
            IntegrationConnection.subject_id == subject_id,
            IntegrationConnection.provider == IntegrationProvider.OPENROUTER.value,
        )
    )
    assert connection is not None
    return connection


async def _upload_raw(
    session: AsyncSession,
    *,
    identity: WriteIdentity,
    connection: IntegrationConnection,
    suffix: str,
    payload: dict | None = None,
) -> tuple[FileAsset, RawPayload]:
    storage_ref = f"body/synthetic-{suffix}.png"
    asset = await file_asset_service.register_legacy_local(
        session,
        subject_id=identity.subject_id,
        uploaded_by_user_id=identity.actor_user_id,
        purpose=FileAssetPurpose.BODY_SCAN_DOCUMENT,
        storage_ref=storage_ref,
        media_type="image/png",
        size_bytes=21,
        content_sha256="a" * 64,
    )
    raw = await raw_payload_service.upsert_owned_raw_payload(
        session,
        identity=identity,
        integration_connection_id=connection.id,
        file_asset_id=asset.id,
        domain=Domain.BODY_COMPOSITION.value,
        source=Source.BODY_SCAN.value,
        external_id=storage_ref,
        payload=payload
        or {
            "date": SCAN_DATE.isoformat(),
            "device": "Synthetic BIA",
            "metrics": [{"label": "Weight", "value": 80.5, "unit": "kg"}],
        },
    )
    return asset, raw


def _metrics(*, weight: float = 80.5, fat: float = 22.0) -> list[dict]:
    return [
        {"label": "Weight", "value": weight, "unit": "kg"},
        {"label": "Percent Body Fat", "value": fat, "unit": "%"},
        {"label": "Lean Body Mass", "value": weight * (1 - fat / 100), "unit": "kg"},
    ]


async def _rawless_scan(
    session: AsyncSession,
    *,
    identity: WriteIdentity,
    on_date: date,
    source: str = Source.MANUAL.value,
    note: str | None = None,
    metrics: list[dict] | None = None,
) -> BodyScan:
    return await body_scan_service.save_scan(
        session,
        on_date=on_date,
        metrics=metrics or _metrics(),
        note=note,
        source=source,
        identity=identity,
        prepared_weight_write=await _prepared_weight(
            session,
            identity,
            on_date=on_date,
        ),
    )


async def test_upload_confirm_keeps_exact_s_a_c_f_raw_and_metric_inheritance(
    db_session,
    legacy_owner_roots,
):
    identity = _identity(legacy_owner_roots)
    connection = await _openrouter_connection(db_session, identity.subject_id)
    asset, raw = await _upload_raw(
        db_session,
        identity=identity,
        connection=connection,
        suffix="exact",
        payload={
            "date": SCAN_DATE.isoformat(),
            "device": "Synthetic BIA",
            # The parser misread body fat; the owner corrects it in preview.
            "metrics": [{"label": "Percent Body Fat", "value": 99.9, "unit": "%"}],
        },
    )

    scan = await body_scan_service.save_scan(
        db_session,
        on_date=SCAN_DATE,
        device="Synthetic BIA",
        file_key=raw.external_id,
        raw_payload_id=raw.id,
        metrics=_metrics(fat=22.0),
        identity=identity,
        prepared_weight_write=await _prepared_weight(db_session, identity),
    )

    assert (
        raw.subject_id,
        raw.actor_user_id,
        raw.integration_connection_id,
        raw.file_asset_id,
        raw.domain,
        raw.source,
    ) == (
        identity.subject_id,
        identity.actor_user_id,
        connection.id,
        asset.id,
        Domain.BODY_COMPOSITION.value,
        Source.BODY_SCAN.value,
    )
    assert (
        scan.subject_id,
        scan.actor_user_id,
        scan.file_asset_id,
        scan.raw_payload_id,
        scan.source,
    ) == (
        identity.subject_id,
        identity.actor_user_id,
        asset.id,
        raw.id,
        Source.BODY_SCAN.value,
    )
    children = list(
        await db_session.scalars(
            select(BodyScanMetric).where(BodyScanMetric.scan_id == scan.id)
        )
    )
    assert children
    assert {(row.subject_id, row.scan_id) for row in children} == {
        (identity.subject_id, scan.id)
    }
    bridged = await db_session.scalar(
        select(WeightLog).where(WeightLog.source == Source.BODY_SCAN.value)
    )
    assert bridged is not None
    assert (
        bridged.subject_id,
        bridged.actor_user_id,
        bridged.raw_payload_id,
        bridged.integration_connection_id,
        bridged.source,
    ) == (
        identity.subject_id,
        identity.actor_user_id,
        raw.id,
        connection.id,
        Source.BODY_SCAN.value,
    )
    assert raw.processed_at is not None
    # The owner's correction lands on the normalized row; what the parser
    # actually said stays on the raw payload, unedited.
    fat = next(m for m in children if m.metric_key == "body_fat_pct")
    assert fat.value == 22.0
    assert raw.payload["metrics"][0]["value"] == 99.9


async def test_mcp_structured_write_is_raw_first_and_splits_scan_from_weight_source(
    db_session,
    session_factory,
    legacy_owner_roots,
    monkeypatch,
):
    mcp_router = pytest.importorskip("web.routers.mcp")
    await _enable_body_comp(
        db_session,
        legacy_owner_roots,
    )
    monkeypatch.setattr(mcp_router, "get_session_factory", lambda: session_factory)
    original_ingest = body_scan_service.ingest_structured_scan
    original_refresh = body_scan_service.refresh_alerts
    captured = []

    async def ingest_probe(*args, **kwargs):
        raw = kwargs["raw_payload"]
        context = weight_service.require_prepared_weight_identity(
            args[0],
            prepared=kwargs["prepared_weight_write"],
            identity=kwargs["identity"],
        )
        captured.append(
            (
                "ingest",
                kwargs["identity"],
                context.legacy_bridge,
                raw.source,
                raw.integration_connection_id,
                raw.file_asset_id,
            )
        )
        return await original_ingest(*args, **kwargs)

    async def refresh_probe(*args, **kwargs):
        context = weight_service.require_prepared_weight_identity(
            args[0],
            prepared=kwargs["prepared_weight_write"],
            identity=kwargs["identity"],
        )
        captured.append(
            (
                "refresh",
                kwargs["identity"],
                context.legacy_bridge,
                kwargs["subject_id"],
                kwargs["on_date"],
            )
        )
        return await original_refresh(*args, **kwargs)

    monkeypatch.setattr(body_scan_service, "ingest_structured_scan", ingest_probe)
    monkeypatch.setattr(body_scan_service, "refresh_alerts", refresh_probe)

    result = await mcp_router.log_body_scan(
        metrics=_metrics(weight=79.4),
        on_date=SCAN_DATE.isoformat(),
        device="Synthetic MCP BIA",
        note="synthetic MCP note",
    )
    assert result.get("error") is None

    raw = await db_session.scalar(
        select(RawPayload).where(RawPayload.source == Source.MCP.value)
    )
    scan = await db_session.scalar(
        select(BodyScan).where(BodyScan.source == Source.MCP.value)
    )
    weight = await db_session.scalar(
        select(WeightLog).where(WeightLog.source == Source.BODY_SCAN.value)
    )
    assert raw is not None and scan is not None and weight is not None
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
        Domain.BODY_COMPOSITION.value,
        Source.MCP.value,
    )
    assert (scan.raw_payload_id, scan.source, weight.raw_payload_id, weight.source) == (
        raw.id,
        Source.MCP.value,
        raw.id,
        Source.BODY_SCAN.value,
    )
    assert captured == [
        (
            "ingest",
            _identity(legacy_owner_roots),
            conflict_engine.LegacyConflictBridge.FULLY_UNOWNED,
            Source.MCP.value,
            None,
            None,
        ),
        (
            "refresh",
            _identity(legacy_owner_roots),
            conflict_engine.LegacyConflictBridge.FULLY_UNOWNED,
            legacy_owner_roots.subject_id,
            SCAN_DATE,
        ),
    ]


async def test_mcp_module_gate_rejects_structured_write_before_raw_creation(
    db_session,
    session_factory,
    legacy_owner_roots,
    monkeypatch,
):
    del legacy_owner_roots
    mcp_router = pytest.importorskip("web.routers.mcp")
    monkeypatch.setattr(mcp_router, "get_session_factory", lambda: session_factory)

    result = await mcp_router.log_body_scan(
        metrics=_metrics(),
        on_date=SCAN_DATE.isoformat(),
    )

    assert result == {"error": "module 'body_comp' is disabled"}
    assert await db_session.scalar(select(func.count()).select_from(RawPayload)) == 0
    assert await db_session.scalar(select(func.count()).select_from(BodyScan)) == 0


async def test_web_upload_and_confirm_keep_owned_boundary_kwargs_and_chain(
    db_session,
    legacy_owner_roots,
    tmp_path,
    monkeypatch,
    platform_ai_ready,
):
    from web.routers import weight as weight_router

    async def extracted(image_urls, *, llm, model, max_tokens):
        del image_urls, llm, max_tokens
        return LLMCallResult(
            value={
                "date": SCAN_DATE.isoformat(),
                "device": "Synthetic Web BIA",
                "metrics": _metrics(weight=78.3),
            },
            upstream_request_id="synthetic-body-boundary",
            model=model,
            input_tokens=10,
            output_tokens=10,
            cost_microunits=1,
        )

    original_save = body_scan_service.save_scan
    original_refresh = body_scan_service.refresh_alerts
    captured: list[tuple] = []

    async def save_probe(*args, **kwargs):
        prepared = kwargs["prepared_weight_write"]
        context = weight_service.require_prepared_weight_identity(
            args[0],
            prepared=prepared,
            identity=kwargs["identity"],
        )
        # save_scan takes its scope from the capability rather than a separate
        # argument, so the capability's subject is what the probe records.
        captured.append((
            "save",
            kwargs["identity"],
            context.identity.subject_id,
            context.legacy_bridge,
            kwargs["on_date"],
        ))
        return await original_save(*args, **kwargs)

    async def refresh_probe(*args, **kwargs):
        context = weight_service.require_prepared_weight_identity(
            args[0],
            prepared=kwargs["prepared_weight_write"],
            identity=kwargs["identity"],
        )
        captured.append((
            "refresh",
            kwargs["identity"],
            kwargs["subject_id"],
            context.legacy_bridge,
            kwargs["on_date"],
        ))
        return await original_refresh(*args, **kwargs)

    monkeypatch.setattr(weight_router, "STATIC_DIR", tmp_path)
    monkeypatch.setattr(
        body_scan_service,
        "extract_prepared_file_with_usage",
        extracted,
    )
    monkeypatch.setattr(body_scan_service, "save_scan", save_probe)
    monkeypatch.setattr(body_scan_service, "refresh_alerts", refresh_probe)
    upload = UploadFile(
        BytesIO(b"synthetic-body-scan"),
        filename="scan.png",
        headers=Headers({"content-type": "image/png"}),
    )
    request = Request(
        {"type": "http", "method": "POST", "path": "/weight/body-scan/upload", "headers": []}
    )
    response = await weight_router.body_scan_upload(
        request=request,
        file=upload,
        date=None,
        db=db_session,
        username="tester",
    )
    body = json.loads(response.body)
    assert body["ok"] is True

    confirm = await weight_router.body_scan_confirm(
        request=request,
        payload=weight_router.BodyScanConfirm(**body["scan"]),
        db=db_session,
        username="tester",
    )
    assert confirm.status_code == 200
    assert captured == [
        (
            "save",
            _identity(legacy_owner_roots),
            legacy_owner_roots.subject_id,
            conflict_engine.LegacyConflictBridge.FULLY_UNOWNED,
            SCAN_DATE,
        ),
        (
            "refresh",
            _identity(legacy_owner_roots),
            legacy_owner_roots.subject_id,
            conflict_engine.LegacyConflictBridge.FULLY_UNOWNED,
            SCAN_DATE,
        ),
    ]
    raw = await db_session.get(RawPayload, body["scan"]["raw_payload_id"])
    scan = await db_session.scalar(select(BodyScan))
    assert raw is not None and scan is not None
    assert (scan.subject_id, scan.actor_user_id, scan.raw_payload_id) == (
        legacy_owner_roots.subject_id,
        legacy_owner_roots.user_id,
        raw.id,
    )

    retry = await weight_router.body_scan_confirm(
        request=request,
        payload=weight_router.BodyScanConfirm(**body["scan"]),
        db=db_session,
        username="tester",
    )
    assert retry.status_code == 400
    assert "already normalized" in json.loads(retry.body)["error"]
    assert captured[-1] == (
        "save",
        _identity(legacy_owner_roots),
        legacy_owner_roots.subject_id,
        conflict_engine.LegacyConflictBridge.FULLY_UNOWNED,
        SCAN_DATE,
    )
    assert await db_session.scalar(select(func.count()).select_from(BodyScan)) == 1
    assert await db_session.scalar(
        select(func.count()).select_from(BodyScanMetric)
    ) == 3
    assert await db_session.scalar(select(func.count()).select_from(WeightLog)) == 1


async def test_web_upload_ignores_disabled_historical_subject_openrouter(
    db_session,
    legacy_owner_roots,
    tmp_path,
    monkeypatch,
    platform_ai_ready,
):
    from web.routers import weight as weight_router

    identity = _identity(legacy_owner_roots)
    connection = await _openrouter_connection(db_session, identity.subject_id)
    connection.status = IntegrationConnectionStatus.DISABLED.value
    await db_session.commit()

    async def extracted(image_urls, *, llm, model, max_tokens):
        del image_urls, llm, max_tokens
        return LLMCallResult(
            value={
                "date": SCAN_DATE.isoformat(),
                "device": "Synthetic platform BIA",
                "metrics": _metrics(),
            },
            upstream_request_id="synthetic-platform-body-scan",
            model=model,
            input_tokens=10,
            output_tokens=10,
            cost_microunits=1,
        )

    monkeypatch.setattr(weight_router, "STATIC_DIR", tmp_path)
    monkeypatch.setattr(
        body_scan_service,
        "extract_prepared_file_with_usage",
        extracted,
    )
    upload = UploadFile(
        BytesIO(b"synthetic-revoked-body-scan"),
        filename="scan.png",
        headers=Headers({"content-type": "image/png"}),
    )
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/weight/body-scan/upload",
            "headers": [],
        }
    )

    response = await weight_router.body_scan_upload(
        request=request,
        file=upload,
        date=None,
        db=db_session,
        username="tester",
    )

    assert json.loads(response.body)["ok"] is True
    raw = await db_session.scalar(select(RawPayload))
    assert raw is not None and raw.integration_connection_id is None
    assert raw.subject_id == identity.subject_id


async def test_subject_a_reads_notes_delete_history_catalog_and_bia_exclude_b(
    db_session,
    legacy_owner_roots,
):
    owner_a = _identity(legacy_owner_roots)
    _, _, owner_b, _ = await _new_owner(db_session, "body-boundary-b")
    scan_a = await _rawless_scan(
        db_session,
        identity=owner_a,
        on_date=SCAN_DATE,
        note="A only",
        metrics=_metrics(weight=80, fat=20)
        + [{"label": "Phase Angle", "value": 6.2, "unit": "deg"}],
    )
    scan_b = await _rawless_scan(
        db_session,
        identity=owner_b,
        on_date=NEXT_DATE,
        note="B only",
        metrics=_metrics(weight=90, fat=30)
        + [
            {"label": "Phase Angle", "value": 4.1, "unit": "deg"},
            {"metric_key": "b_only", "value": 1},
        ],
    )
    await db_session.commit()

    assert [row.id for row in await body_scan_service.list_scans(
        db_session, subject_id=owner_a.subject_id
    )] == [scan_a.id]
    assert (await body_scan_service.latest_scan(
        db_session, subject_id=owner_a.subject_id
    )).id == scan_a.id
    assert (await body_scan_service.get_scan(
        db_session, scan_a.id, subject_id=owner_a.subject_id
    )).note == "A only"
    assert await body_scan_service.get_scan(
        db_session, scan_b.id, subject_id=owner_a.subject_id
    ) is None
    provenance = (scan_a.subject_id, scan_a.actor_user_id, scan_a.source)
    assert await body_scan_service.update_scan_note(
        db_session,
        scan_b.id,
        note="must not cross subjects",
        identity=owner_a,
        prepared_weight_write=await _prepared_weight(
            db_session,
            owner_a,
            on_date=NEXT_DATE,
        ),
    ) is None
    noted = await body_scan_service.update_scan_note(
        db_session,
        scan_a.id,
        note="A updated",
        identity=owner_a,
        prepared_weight_write=await _prepared_weight(db_session, owner_a),
    )
    assert noted is not None and noted.note == "A updated"
    assert (noted.subject_id, noted.actor_user_id, noted.source) == provenance
    history = await body_scan_service.metric_history(
        db_session,
        "phase_angle",
        subject_id=owner_a.subject_id,
    )
    assert history == [
        {
            "date": SCAN_DATE.isoformat(),
            "value": 6.2,
            "unit": "deg",
            "segment": None,
            "ref_low": None,
            "ref_high": None,
        }
    ]
    catalog = await body_scan_service.available_metrics(
        db_session,
        subject_id=owner_a.subject_id,
    )
    assert "b_only" not in {item["value"] for item in catalog}
    bia = await body_scan_service.bia_chart_points(
        db_session,
        subject_id=owner_a.subject_id,
    )
    assert bia == {
        "bf": [{"date": SCAN_DATE.isoformat(), "value": 20.0}],
        "lbm": [{"date": SCAN_DATE.isoformat(), "value": 64.0}],
    }
    assert not await body_scan_service.delete_scan(
        db_session,
        scan_b.id,
        subject_id=owner_a.subject_id,
        identity=owner_a,
        prepared_weight_write=await _prepared_weight(
            db_session,
            owner_a,
            on_date=NEXT_DATE,
        ),
    )
    assert await db_session.get(BodyScan, scan_b.id) is not None


async def test_mcp_note_delete_and_web_delete_prepare_before_target_reads(
    db_session,
    session_factory,
    legacy_owner_roots,
    monkeypatch,
):
    from web.routers import mcp as mcp_router
    from web.routers import weight as weight_router

    identity = _identity(legacy_owner_roots)
    await _enable_body_comp(
        db_session,
        legacy_owner_roots,
    )
    mcp_scan = await _rawless_scan(
        db_session,
        identity=identity,
        on_date=SCAN_DATE,
        note="MCP target",
        metrics=[{"label": "Phase Angle", "value": 6.0}],
    )
    web_scan = await _rawless_scan(
        db_session,
        identity=identity,
        on_date=SCAN_DATE,
        note="Web target",
        metrics=[{"label": "Phase Angle", "value": 6.1}],
    )
    await db_session.commit()
    monkeypatch.setattr(mcp_router, "get_session_factory", lambda: session_factory)

    original_get = body_scan_service.get_scan
    original_refresh = body_scan_service.refresh_alerts
    original_mcp_prepare = mcp_router._mcp_v1_weight_write
    original_web_prepare = weight_router._prepare_weight_write
    events: list[str] = []

    async def get_probe(*args, **kwargs):
        events.append("target_read")
        return await original_get(*args, **kwargs)

    async def mcp_prepare_probe(*args, **kwargs):
        events.append("mcp_prepare")
        return await original_mcp_prepare(*args, **kwargs)

    async def web_prepare_probe(*args, **kwargs):
        events.append("web_prepare")
        return await original_web_prepare(*args, **kwargs)

    async def refresh_probe(*args, **kwargs):
        events.append("refresh_alerts")
        return await original_refresh(*args, **kwargs)

    monkeypatch.setattr(body_scan_service, "get_scan", get_probe)
    monkeypatch.setattr(body_scan_service, "refresh_alerts", refresh_probe)
    monkeypatch.setattr(mcp_router, "_mcp_v1_weight_write", mcp_prepare_probe)
    monkeypatch.setattr(weight_router, "_prepare_weight_write", web_prepare_probe)

    noted = await mcp_router.log_note(
        "body_comp",
        mcp_scan.id,
        "prepared before note target",
    )
    assert noted["note"] == "prepared before note target"
    assert events[0] == "mcp_prepare"
    assert "target_read" in events[1:]

    events.clear()
    deleted = await mcp_router.delete_record("body_comp", mcp_scan.id)
    assert deleted["deleted"] is True
    assert events[0] == "mcp_prepare"
    assert "target_read" in events[1:]
    assert "refresh_alerts" in events[1:]

    events.clear()
    response = await weight_router.delete_body_scan_entry(
        request=Request(
            {
                "type": "http",
                "method": "POST",
                "path": f"/weight/body-scan/{web_scan.id}/delete",
                "headers": [],
                "query_string": b"",
                "scheme": "http",
                "server": ("test", 80),
            }
        ),
        scan_id=web_scan.id,
        db=db_session,
        username="tester",
    )
    assert response.status_code == 303
    assert events[0] == "web_prepare"
    assert "target_read" in events[1:]
    assert await db_session.get(BodyScan, web_scan.id) is None


async def test_fully_null_legacy_graph_is_invisible_to_the_closed_domain(
    db_session,
    legacy_owner_roots,
):
    """The bridge that once adopted an unowned scan on read is gone.

    While body_comp still had a compatibility arm, a scan with no subject was
    visible to the sole owner and adoptable on delete. Closing the domain makes
    the subject the only key: an unowned row is now outside every scope, which
    is what stops a second person's request from ever reaching it.
    """
    identity = _identity(legacy_owner_roots)
    legacy = BodyScan(
        date=SCAN_DATE,
        domain=Domain.BODY_COMPOSITION.value,
        source=Source.MANUAL.value,
        note="fully null legacy",
    )
    legacy.metrics.append(
        BodyScanMetric(
            metric_key="phase_angle",
            label="Phase Angle",
            value=6.0,
        )
    )
    db_session.add(legacy)
    await db_session.commit()

    visible = await body_scan_service.list_scans(
        db_session,
        subject_id=identity.subject_id,
    )
    assert visible == []
    assert not await body_scan_service.delete_scan(
        db_session,
        legacy.id,
        subject_id=identity.subject_id,
        identity=identity,
        prepared_weight_write=await _prepared_weight(
            db_session,
            identity,
            legacy=True,
        ),
    )
    assert await db_session.get(BodyScan, legacy.id) is not None


async def test_legacy_raw_replay_weight_bridge_remains_scoped_readable(
    db_session,
    legacy_owner_roots,
):
    system = _identity(legacy_owner_roots, system=True)
    raw = RawPayload(
        domain=Domain.BODY_COMPOSITION.value,
        source=Source.BODY_SCAN.value,
        external_id="body_scan:legacy-weight-bridge",
        payload={
            "date": SCAN_DATE.isoformat(),
            "metrics": [{"label": "Weight", "value": 76.4, "unit": "kg"}],
        },
    )
    db_session.add(raw)
    await db_session.commit()

    assert await body_scan_service.reparse_owned_pending(
        db_session,
        identity=system,
    ) == 1
    bridged = await weight_service.get_active_weight(
        db_session,
        SCAN_DATE,
        subject_id=system.subject_id,
    )
    assert bridged is not None
    assert (
        bridged.subject_id,
        bridged.actor_user_id,
        bridged.integration_connection_id,
        bridged.raw_payload_id,
        bridged.source,
    ) == (
        system.subject_id,
        None,
        None,
        raw.id,
        Source.BODY_SCAN.value,
    )


async def test_stage3a_parser_history_replays_scan_and_weight_without_file_adoption(
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
        domain=Domain.BODY_COMPOSITION.value,
        source=Source.BODY_SCAN.value,
        external_id="body_scan:stage3a-history",
        payload={
            "date": SCAN_DATE.isoformat(),
            "device": "Historical Synthetic BIA",
            "metrics": [{"label": "Weight", "value": 77.2, "unit": "kg"}],
        },
    )
    db_session.add(raw)
    await db_session.commit()

    with pytest.raises(
        conflict_engine.ConflictRawOwnershipError,
        match="no file root",
    ):
        await body_scan_service.save_scan(
            db_session,
            on_date=SCAN_DATE,
            raw_payload_id=raw.id,
            metrics=_metrics(weight=77.2),
            identity=system,
            prepared_weight_write=await _prepared_weight(
                db_session,
                system,
                legacy=True,
            ),
        )
    await db_session.rollback()

    assert await body_scan_service.reparse_owned_pending(
        db_session,
        identity=system,
    ) == 1
    scan = await db_session.scalar(
        select(BodyScan).where(BodyScan.raw_payload_id == raw.id)
    )
    weight = await weight_service.get_active_weight(
        db_session,
        SCAN_DATE,
        subject_id=system.subject_id,
    )
    raw = await db_session.get(RawPayload, raw.id)
    assert scan is not None and weight is not None and raw is not None
    assert (
        scan.subject_id,
        scan.actor_user_id,
        scan.file_asset_id,
        scan.file_key,
    ) == (system.subject_id, None, None, None)
    assert (
        weight.subject_id,
        weight.actor_user_id,
        weight.integration_connection_id,
        weight.raw_payload_id,
    ) == (system.subject_id, None, connection.id, raw.id)
    assert raw.processed_at is not None


async def test_stage3a_mcp_history_without_a_subject_is_unreadable_and_unlinkable(
    db_session,
    legacy_owner_roots,
):
    """A half-migrated MCP graph — owned raw, unowned scan — stays out of reach.

    The compatibility bridge that once surfaced such a scan is gone, so the row
    is invisible, and its raw still cannot be relinked to a new scan, which is
    what stops the unowned half from being quietly adopted.
    """
    identity = _identity(legacy_owner_roots)
    raw = RawPayload(
        subject_id=identity.subject_id,
        actor_user_id=None,
        integration_connection_id=None,
        file_asset_id=None,
        domain=Domain.BODY_COMPOSITION.value,
        source=Source.MCP.value,
        external_id="body_scan:stage3a-mcp-history",
        payload={"date": SCAN_DATE.isoformat(), "metrics": []},
    )
    scan = BodyScan(
        date=SCAN_DATE,
        domain=Domain.BODY_COMPOSITION.value,
        source=Source.MCP.value,
        raw_payload_id=None,
    )
    scan.metrics.append(
        BodyScanMetric(
            metric_key="phase_angle",
            label="Phase Angle",
            value=6.1,
        )
    )
    db_session.add_all([raw, scan])
    await db_session.flush()
    scan.raw_payload_id = raw.id
    await db_session.commit()

    assert await body_scan_service.list_scans(
        db_session,
        subject_id=identity.subject_id,
    ) == []

    with pytest.raises(conflict_engine.ConflictRawOwnershipError):
        await body_scan_service.save_scan(
            db_session,
            on_date=NEXT_DATE,
            raw_payload_id=raw.id,
            metrics=_metrics(),
            source=Source.MCP.value,
            identity=identity,
            prepared_weight_write=await _prepared_weight(
                db_session,
                identity,
                on_date=NEXT_DATE,
                legacy=True,
            ),
        )


@pytest.mark.parametrize("actor_mode", ["null", "foreign"])
async def test_exact_manual_scan_actor_must_be_subject_owner(
    db_session,
    legacy_owner_roots,
    actor_mode,
):
    """A manual scan names either the subject's owner or nobody at all.

    The Stage-3B backfill stamped the subject onto migrated history without
    inventing an actor for it, so a null actor is what a pre-multi-user manual
    scan legitimately looks like. Any *other* user's id on that row is a forged
    attribution and stays refused.
    """
    identity = _identity(legacy_owner_roots)
    actor_user_id = None
    if actor_mode == "foreign":
        foreign_user, _, _, _ = await _new_owner(db_session, "forged-manual-actor")
        actor_user_id = foreign_user.id
    scan = BodyScan(
        subject_id=identity.subject_id,
        actor_user_id=actor_user_id,
        date=SCAN_DATE,
        domain=Domain.BODY_COMPOSITION.value,
        source=Source.MANUAL.value,
    )
    scan.metrics.append(
        BodyScanMetric(
            subject_id=identity.subject_id,
            metric_key="phase_angle",
            label="Phase Angle",
            value=6.0,
        )
    )
    db_session.add(scan)
    await db_session.commit()

    if actor_mode == "null":
        visible = await body_scan_service.list_scans(
            db_session,
            subject_id=identity.subject_id,
        )
        assert [row.id for row in visible] == [scan.id]
        return

    with pytest.raises(body_scan_service.BodyScanOwnershipError):
        await body_scan_service.list_scans(
            db_session,
            subject_id=identity.subject_id,
        )


async def test_exact_upload_chain_cannot_share_one_foreign_actor(
    db_session,
    legacy_owner_roots,
):
    identity = _identity(legacy_owner_roots)
    connection = await _openrouter_connection(db_session, identity.subject_id)
    asset, raw = await _upload_raw(
        db_session,
        identity=identity,
        connection=connection,
        suffix="forged-shared-actor",
    )
    foreign_user, _, _, _ = await _new_owner(db_session, "forged-upload-actor")
    asset.uploaded_by_user_id = foreign_user.id
    raw.actor_user_id = foreign_user.id
    scan = BodyScan(
        subject_id=identity.subject_id,
        actor_user_id=foreign_user.id,
        file_asset_id=asset.id,
        raw_payload_id=raw.id,
        file_key=asset.storage_ref,
        date=SCAN_DATE,
        domain=Domain.BODY_COMPOSITION.value,
        source=Source.BODY_SCAN.value,
    )
    scan.metrics.append(
        BodyScanMetric(
            subject_id=identity.subject_id,
            metric_key="phase_angle",
            label="Phase Angle",
            value=6.0,
        )
    )
    db_session.add(scan)
    await db_session.commit()

    with pytest.raises(conflict_engine.ConflictRawOwnershipError):
        await body_scan_service.list_scans(
            db_session,
            subject_id=identity.subject_id,
        )


async def test_exact_mcp_chain_cannot_share_one_foreign_actor(
    db_session,
    legacy_owner_roots,
):
    identity = _identity(legacy_owner_roots)
    foreign_user, _, _, _ = await _new_owner(db_session, "forged-mcp-actor")
    raw = RawPayload(
        subject_id=identity.subject_id,
        actor_user_id=foreign_user.id,
        domain=Domain.BODY_COMPOSITION.value,
        source=Source.MCP.value,
        external_id="synthetic-forged-mcp",
        payload={"date": SCAN_DATE.isoformat(), "metrics": []},
    )
    db_session.add(raw)
    await db_session.flush()
    scan = BodyScan(
        subject_id=identity.subject_id,
        actor_user_id=foreign_user.id,
        raw_payload_id=raw.id,
        date=SCAN_DATE,
        domain=Domain.BODY_COMPOSITION.value,
        source=Source.MCP.value,
    )
    db_session.add(scan)
    await db_session.commit()

    with pytest.raises(conflict_engine.ConflictRawOwnershipError):
        await body_scan_service.list_scans(
            db_session,
            subject_id=identity.subject_id,
        )


@pytest.mark.parametrize("raw_source", [Source.MCP.value, Source.BODY_SCAN.value])
async def test_fully_null_scan_never_reaches_a_scope_through_its_owned_raw(
    db_session,
    legacy_owner_roots,
    raw_source,
):
    """Owning the raw does not pull an ownerless scan into the owner's history.

    The scan's own subject is the only thing the reader scopes on, so a row that
    names nobody stays outside every scope no matter whose payload it points at.
    """
    identity = _identity(legacy_owner_roots)
    if raw_source == Source.BODY_SCAN.value:
        connection = await _openrouter_connection(db_session, identity.subject_id)
        _, raw = await _upload_raw(
            db_session,
            identity=identity,
            connection=connection,
            suffix="reverse-mixed-upload",
        )
    else:
        raw = RawPayload(
            subject_id=identity.subject_id,
            actor_user_id=identity.actor_user_id,
            domain=Domain.BODY_COMPOSITION.value,
            source=Source.MCP.value,
            external_id="reverse-mixed-mcp",
            payload={"date": SCAN_DATE.isoformat(), "metrics": []},
        )
        db_session.add(raw)
        await db_session.flush()
    legacy_scan = BodyScan(
        raw_payload_id=raw.id,
        date=SCAN_DATE,
        domain=Domain.BODY_COMPOSITION.value,
        source=raw_source,
    )
    legacy_scan.metrics.append(
        BodyScanMetric(
            metric_key="phase_angle",
            label="Phase Angle",
            value=6.0,
        )
    )
    db_session.add(legacy_scan)
    await db_session.commit()

    assert await body_scan_service.list_scans(
        db_session,
        subject_id=identity.subject_id,
    ) == []


async def _write_broken_graph(session) -> bool:
    """Commit a deliberately invalid graph, and say whether it survived.

    Several of the shapes below are ones the *schema* forbids on PostgreSQL:
    ``fk_body_scan_metrics_scan_subject`` is a composite key over
    ``(scan_id, subject_id)``, so a metric whose subject differs from its scan's
    — or a scan that lost its subject while its metric kept one — cannot be
    written at all. That is a stronger guarantee than the service refusing to
    read them, and it is the one production has.

    SQLite does not enforce that composite key, which is why the rows can be
    built there and why the service-level refusal below is what these cases
    exercise on the fast path. Asserting the same thing two ways is the point:
    on the database that ships, the state is unreachable; on the one the suite
    runs, the reader still refuses it.

    Returns ``False`` when the database refused, and leaves the session clean.
    """

    from sqlalchemy.exc import IntegrityError

    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return False
    return True


@pytest.mark.parametrize(
    "invalid_link",
    ["foreign_scan", "partial_legacy_scan", "foreign_metric"],
)
async def test_owned_replay_rejects_foreign_or_partial_link_suppression(
    db_session,
    legacy_owner_roots,
    invalid_link,
):
    identity = _identity(legacy_owner_roots)
    system = _identity(legacy_owner_roots, system=True)
    connection = await _openrouter_connection(db_session, identity.subject_id)
    asset, raw = await _upload_raw(
        db_session,
        identity=identity,
        connection=connection,
        suffix=f"invalid-replay-link-{invalid_link}",
    )
    scan_subject_id = identity.subject_id
    scan_actor_user_id = identity.actor_user_id
    metric_subject_id = identity.subject_id
    if invalid_link == "foreign_scan":
        foreign_user, foreign_subject, _, _ = await _new_owner(
            db_session,
            "foreign-replay-link",
        )
        scan_subject_id = foreign_subject.id
        scan_actor_user_id = foreign_user.id
        metric_subject_id = foreign_subject.id
    elif invalid_link == "partial_legacy_scan":
        scan_subject_id = None
        metric_subject_id = None
    elif invalid_link == "foreign_metric":
        _, foreign_subject, _, _ = await _new_owner(
            db_session,
            "foreign-replay-metric",
        )
        metric_subject_id = foreign_subject.id
    scan = BodyScan(
        subject_id=scan_subject_id,
        actor_user_id=scan_actor_user_id,
        file_asset_id=(asset.id if scan_subject_id == identity.subject_id else None),
        raw_payload_id=raw.id,
        file_key=(
            asset.storage_ref if scan_subject_id == identity.subject_id else None
        ),
        date=SCAN_DATE,
        domain=Domain.BODY_COMPOSITION.value,
        source=Source.BODY_SCAN.value,
    )
    scan.metrics.append(
        BodyScanMetric(
            subject_id=metric_subject_id,
            metric_key="phase_angle",
            label="Phase Angle",
            value=6.0,
        )
    )
    db_session.add(scan)
    if not await _write_broken_graph(db_session):
        # The composite key refused it. Nothing to replay, which is the
        # assertion — see ``_write_broken_graph``.
        return

    with pytest.raises(
        conflict_engine.ConflictRawOwnershipError,
        match="foreign or partial normalized provenance",
    ):
        await body_scan_service.reparse_owned_pending(
            db_session,
            identity=system,
        )
    await db_session.refresh(raw)
    assert raw.processed_at is None
    assert list(
        await db_session.scalars(
            select(BodyScan.id).where(BodyScan.raw_payload_id == raw.id)
        )
    ) == [scan.id]


async def test_latest_scan_rejects_newer_partial_legacy_instead_of_using_stale(
    db_session,
    legacy_owner_roots,
):
    identity = _identity(legacy_owner_roots)
    await _rawless_scan(
        db_session,
        identity=identity,
        on_date=SCAN_DATE,
        metrics=[{"label": "Phase Angle", "value": 6.0}],
    )
    partial = BodyScan(
        actor_user_id=identity.actor_user_id,
        date=NEXT_DATE,
        domain=Domain.BODY_COMPOSITION.value,
        source=Source.MANUAL.value,
    )
    partial.metrics.append(
        BodyScanMetric(
            metric_key="phase_angle",
            label="Phase Angle",
            value=6.1,
        )
    )
    db_session.add(partial)
    await db_session.commit()

    with pytest.raises(body_scan_service.BodyScanOwnershipError):
        await body_scan_service.latest_scan(
            db_session,
            subject_id=identity.subject_id,
        )


@pytest.mark.parametrize(
    "broken_part",
    [
        "scan_actor_without_subject",
        "scan_file_without_subject",
        # A bare raw link on an ownerless scan is not a broken chain — it is
        # what a running ownership backfill looks like mid-flight, and
        # test_fully_null_scan_never_reaches_a_scope_through_its_owned_raw
        # pins the invisibility that covers it instead.
        "metric_missing_subject",
        "metric_foreign_subject",
        "raw_missing_subject",
        "raw_missing_actor",
        "raw_missing_connection",
        "raw_missing_file",
    ],
)
async def test_every_partial_scan_metric_and_raw_chain_fails_closed(
    db_session,
    legacy_owner_roots,
    broken_part,
):
    identity = _identity(legacy_owner_roots)
    connection = await _openrouter_connection(db_session, identity.subject_id)
    asset, raw = await _upload_raw(
        db_session,
        identity=identity,
        connection=connection,
        suffix=f"partial-{broken_part}",
    )
    scan = BodyScan(
        subject_id=identity.subject_id,
        actor_user_id=identity.actor_user_id,
        file_asset_id=asset.id,
        raw_payload_id=raw.id,
        date=SCAN_DATE,
        domain=Domain.BODY_COMPOSITION.value,
        source=Source.BODY_SCAN.value,
    )
    scan.metrics.append(
        BodyScanMetric(
            subject_id=identity.subject_id,
            metric_key="phase_angle",
            label="Phase Angle",
            value=6.0,
        )
    )
    if broken_part == "scan_actor_without_subject":
        scan.subject_id = None
        scan.file_asset_id = None
        scan.raw_payload_id = None
    elif broken_part == "scan_file_without_subject":
        scan.subject_id = None
        scan.actor_user_id = None
        scan.raw_payload_id = None
    elif broken_part == "metric_missing_subject":
        scan.metrics[0].subject_id = None
    elif broken_part == "metric_foreign_subject":
        _, foreign_subject, _, _ = await _new_owner(
            db_session, "partial-metric-foreign"
        )
        scan.metrics[0].subject_id = foreign_subject.id
    elif broken_part == "raw_missing_subject":
        raw.subject_id = None
    elif broken_part == "raw_missing_actor":
        raw.actor_user_id = None
    elif broken_part == "raw_missing_connection":
        raw.integration_connection_id = None
    elif broken_part == "raw_missing_file":
        raw.file_asset_id = None
    db_session.add(scan)
    if not await _write_broken_graph(db_session):
        # The composite key refused it — see ``_write_broken_graph``.
        return

    with pytest.raises(
        (
            body_scan_service.BodyScanOwnershipError,
            conflict_engine.ConflictRawOwnershipError,
        )
    ):
        await body_scan_service.list_scans(
            db_session,
            subject_id=identity.subject_id,
        )


@pytest.mark.parametrize(
    "tamper",
    [
        "uploader",
        "asset_purpose",
        "asset_status",
        "raw_domain",
        "raw_source",
        "connection_provider",
        "connection_status",
    ],
)
async def test_upload_chain_validates_uploader_asset_raw_and_openrouter(
    db_session,
    legacy_owner_roots,
    tamper,
):
    identity = _identity(legacy_owner_roots)
    connection = await _openrouter_connection(db_session, identity.subject_id)
    asset, raw = await _upload_raw(
        db_session,
        identity=identity,
        connection=connection,
        suffix=f"tamper-{tamper}",
    )
    if tamper == "uploader":
        user, _, _, _ = await _new_owner(db_session, "foreign-uploader")
        asset.uploaded_by_user_id = user.id
    elif tamper == "asset_purpose":
        asset.purpose = FileAssetPurpose.LAB_DOCUMENT.value
    elif tamper == "asset_status":
        asset.status = FileAssetStatus.DELETED.value
        asset.deleted_at = now_local()
    elif tamper == "raw_domain":
        raw.domain = Domain.LABS.value
    elif tamper == "raw_source":
        raw.source = Source.MCP.value
    elif tamper == "connection_provider":
        wrong_connection = IntegrationConnection(
            subject_id=identity.subject_id,
            provider=IntegrationProvider.GARMIN.value,
            connection_type=IntegrationConnectionType.ACCOUNT.value,
            external_account_discriminator="synthetic-wrong-provider",
            status=IntegrationConnectionStatus.ACTIVE.value,
        )
        db_session.add(wrong_connection)
        await db_session.flush()
        raw.integration_connection_id = wrong_connection.id
    elif tamper == "connection_status":
        connection.status = IntegrationConnectionStatus.PENDING.value
    await db_session.commit()

    with pytest.raises(conflict_engine.ConflictRawOwnershipError):
        await body_scan_service.save_scan(
            db_session,
            on_date=SCAN_DATE,
            file_key=raw.external_id,
            raw_payload_id=raw.id,
            metrics=_metrics(),
            identity=identity,
            prepared_weight_write=await _prepared_weight(db_session, identity),
        )
    assert await db_session.scalar(select(func.count()).select_from(BodyScan)) == 0
    assert await db_session.scalar(select(func.count()).select_from(WeightLog)) == 0


async def test_capability_is_rejected_before_raw_or_scan_target_resolution(
    db_session,
    legacy_owner_roots,
):
    owner = _identity(legacy_owner_roots)
    _, _, foreign, _ = await _new_owner(db_session, "body-capability-foreign")
    wrong_capability = await _prepared_weight(db_session, foreign)

    with pytest.raises(conflict_engine.ConflictPreparedWriteError):
        await body_scan_service.save_scan(
            db_session,
            on_date=SCAN_DATE,
            file_key="body/nonexistent.png",
            raw_payload_id=999_999,
            metrics=_metrics(),
            identity=owner,
            prepared_weight_write=wrong_capability,
        )
    with pytest.raises(conflict_engine.ConflictPreparedWriteError):
        await body_scan_service.delete_scan(
            db_session,
            999_999,
            subject_id=owner.subject_id,
            identity=owner,
            prepared_weight_write=wrong_capability,
        )


async def test_scoped_source_matrix_and_persisted_source_tamper_fail_closed(
    db_session,
    legacy_owner_roots,
):
    identity = _identity(legacy_owner_roots)
    connection = await _openrouter_connection(db_session, identity.subject_id)
    _, raw = await _upload_raw(
        db_session,
        identity=identity,
        connection=connection,
        suffix="source-matrix",
    )

    with pytest.raises(conflict_engine.ConflictRawOwnershipError):
        await body_scan_service.save_scan(
            db_session,
            on_date=SCAN_DATE,
            metrics=[{"label": "Phase Angle", "value": 6.0}],
            source=Source.BODY_SCAN.value,
            identity=identity,
            prepared_weight_write=await _prepared_weight(db_session, identity),
        )
    with pytest.raises(conflict_engine.ConflictRawOwnershipError):
        await body_scan_service.save_scan(
            db_session,
            on_date=SCAN_DATE,
            raw_payload_id=raw.id,
            metrics=[{"label": "Phase Angle", "value": 6.0}],
            source=Source.MANUAL.value,
            identity=identity,
            prepared_weight_write=await _prepared_weight(db_session, identity),
        )
    assert await db_session.scalar(select(func.count()).select_from(BodyScan)) == 0

    manual = await body_scan_service.save_scan(
        db_session,
        on_date=SCAN_DATE,
        metrics=[{"label": "Phase Angle", "value": 6.0}],
        source=Source.MANUAL.value,
        identity=identity,
        prepared_weight_write=await _prepared_weight(db_session, identity),
    )
    assert (
        manual.subject_id,
        manual.actor_user_id,
        manual.source,
        manual.raw_payload_id,
        manual.file_asset_id,
        manual.file_key,
    ) == (
        identity.subject_id,
        identity.actor_user_id,
        Source.MANUAL.value,
        None,
        None,
        None,
    )
    await db_session.commit()

    manual.source = Source.BODY_SCAN.value
    await db_session.commit()
    with pytest.raises(conflict_engine.ConflictRawOwnershipError):
        await body_scan_service.get_scan(
            db_session,
            manual.id,
            subject_id=identity.subject_id,
        )


async def test_direct_retry_of_normalized_raw_is_typed_and_write_free(
    db_session,
    legacy_owner_roots,
):
    identity = _identity(legacy_owner_roots)
    connection = await _openrouter_connection(db_session, identity.subject_id)
    _, raw = await _upload_raw(
        db_session,
        identity=identity,
        connection=connection,
        suffix="direct-retry",
    )
    kwargs = {
        "on_date": SCAN_DATE,
        "file_key": raw.external_id,
        "raw_payload_id": raw.id,
        "metrics": _metrics(weight=77.7),
        "identity": identity,
    }
    first = await body_scan_service.save_scan(
        db_session,
        **kwargs,
        prepared_weight_write=await _prepared_weight(db_session, identity),
    )
    await db_session.commit()
    first_metric_ids = list(
        await db_session.scalars(
            select(BodyScanMetric.id).where(BodyScanMetric.scan_id == first.id)
        )
    )
    first_weight_id = await db_session.scalar(
        select(WeightLog.id).where(WeightLog.raw_payload_id == raw.id)
    )

    with pytest.raises(body_scan_service.BodyScanRawAlreadyNormalizedError):
        await body_scan_service.save_scan(
            db_session,
            **kwargs,
            prepared_weight_write=await _prepared_weight(db_session, identity),
        )
    assert list(await db_session.scalars(select(BodyScan.id))) == [first.id]
    assert list(await db_session.scalars(select(BodyScanMetric.id))) == first_metric_ids
    assert list(
        await db_session.scalars(
            select(WeightLog.id).where(WeightLog.raw_payload_id == raw.id)
        )
    ) == [first_weight_id]


async def test_conflict_block_is_write_free_and_override_is_attributed(
    db_session,
    legacy_owner_roots,
):
    identity = _identity(legacy_owner_roots)
    rule = ConflictRule(
        subject_id=identity.subject_id,
        rule_type=RuleType.HARD_BLOCK.value,
        domain_a=Domain.BODY_COMPOSITION.value,
        condition_a={"scan": True},
        domain_b=Domain.LABS.value,
        condition_b={"marker": "synthetic-body-risk"},
        severity=Severity.BLOCK.value,
        message="Synthetic BodyScan block.",
        active=True,
    )
    db_session.add(rule)
    await db_session.commit()

    async def labs(session, *, scope):
        del session, scope
        return [{"marker": "synthetic-body-risk"}]

    conflict_engine.register_domain_resolver(
        Domain.BODY_COMPOSITION.value,
        body_scan_service.resolve_active_scoped,
    )
    conflict_engine.register_domain_resolver(Domain.LABS.value, labs)
    prepared = await _prepared_weight(db_session, identity)
    with pytest.raises(conflict_engine.ConflictBlocked):
        await body_scan_service.save_scan(
            db_session,
            on_date=SCAN_DATE,
            metrics=_metrics(),
            source=Source.MANUAL.value,
            identity=identity,
            prepared_weight_write=prepared,
        )
    assert await db_session.scalar(select(func.count()).select_from(BodyScan)) == 0
    assert await db_session.scalar(select(func.count()).select_from(BodyScanMetric)) == 0
    assert await db_session.scalar(select(func.count()).select_from(WeightLog)) == 0
    assert await db_session.scalar(select(func.count()).select_from(SystemAlert)) == 0

    saved = await body_scan_service.save_scan(
        db_session,
        on_date=SCAN_DATE,
        metrics=_metrics(),
        source=Source.MANUAL.value,
        override=True,
        identity=identity,
        prepared_weight_write=prepared,
    )
    alert = await db_session.scalar(
        select(SystemAlert).where(SystemAlert.alert_key == f"conflict:{rule.id}")
    )
    assert (saved.subject_id, saved.actor_user_id, saved.source) == (
        identity.subject_id,
        identity.actor_user_id,
        Source.MANUAL.value,
    )
    assert alert is not None
    assert (alert.subject_id, alert.integration_connection_id) == (
        identity.subject_id,
        None,
    )
    assert alert.overridden_by_user_id == identity.actor_user_id
    assert alert.override_at is not None


async def test_visceral_and_phase_alerts_are_typed_scoped_and_actorless(
    db_session,
    legacy_owner_roots,
):
    owner = _identity(legacy_owner_roots)
    system = _identity(legacy_owner_roots, system=True)
    await _rawless_scan(
        db_session,
        identity=owner,
        on_date=SCAN_DATE,
        metrics=[
            {
                "metric_key": "visceral_fat_area",
                "label": "Visceral Fat Area",
                "value": 125,
                "unit": "cm2",
                "ref_high": 100,
            },
            {
                "metric_key": "phase_angle",
                "label": "Phase Angle",
                "value": 4.2,
                "ref_low": 5.0,
            },
        ],
    )
    await db_session.commit()

    await body_scan_service.refresh_alerts(
        db_session,
        subject_id=system.subject_id,
        on_date=SCAN_DATE,
        identity=system,
        prepared_weight_write=await _prepared_weight(db_session, system),
    )
    alerts = list(
        await db_session.scalars(
            select(SystemAlert).where(
                SystemAlert.alert_key.in_(
                    [body_scan_service.VISCERAL_ALERT_KEY, body_scan_service.PHASE_ALERT_KEY]
                )
            )
        )
    )
    assert {row.alert_key for row in alerts} == {
        body_scan_service.VISCERAL_ALERT_KEY,
        body_scan_service.PHASE_ALERT_KEY,
    }
    assert {
        (
            row.subject_id,
            row.integration_connection_id,
            row.overridden_by_user_id,
            row.resolved_by_user_id,
            row.domain,
        )
        for row in alerts
    } == {
        (owner.subject_id, None, None, None, Domain.BODY_COMPOSITION.value)
    }


async def test_same_day_scans_keep_independent_conflicts_and_resolver_entities(
    db_session,
    legacy_owner_roots,
):
    identity = _identity(legacy_owner_roots)
    historical = await body_scan_service.save_scan(
        db_session,
        on_date=SCAN_DATE - timedelta(days=1),
        metrics=[{"label": "Phase Angle", "value": 5.9}],
        source=Source.MANUAL.value,
        identity=identity,
        prepared_weight_write=await _prepared_weight(
            db_session,
            identity,
            on_date=SCAN_DATE - timedelta(days=1),
        ),
    )
    rule = ConflictRule(
        subject_id=identity.subject_id,
        rule_type=RuleType.HARD_BLOCK.value,
        domain_a=Domain.BODY_COMPOSITION.value,
        condition_a={"scan": True},
        domain_b=Domain.LABS.value,
        condition_b={"marker": "synthetic-same-day-risk"},
        severity=Severity.BLOCK.value,
        message="Synthetic same-day BodyScan block.",
        active=True,
    )
    db_session.add(rule)
    await db_session.commit()

    async def labs(session, *, scope):
        del session, scope
        return [{"marker": "synthetic-same-day-risk"}]

    conflict_engine.register_domain_resolver(
        Domain.BODY_COMPOSITION.value,
        body_scan_service.resolve_active_scoped,
    )
    conflict_engine.register_domain_resolver(Domain.LABS.value, labs)
    first = await body_scan_service.save_scan(
        db_session,
        on_date=SCAN_DATE,
        metrics=[{"label": "Phase Angle", "value": 6.0}],
        source=Source.MANUAL.value,
        override=True,
        identity=identity,
        prepared_weight_write=await _prepared_weight(db_session, identity),
    )
    second = await body_scan_service.save_scan(
        db_session,
        on_date=SCAN_DATE,
        metrics=[{"label": "Phase Angle", "value": 6.1}],
        source=Source.MANUAL.value,
        override=True,
        identity=identity,
        prepared_weight_write=await _prepared_weight(db_session, identity),
    )

    alerts = list(
        await db_session.scalars(
            select(SystemAlert)
            .where(SystemAlert.alert_key == f"conflict:{rule.id}")
            .order_by(SystemAlert.id)
        )
    )
    assert len(alerts) == 2
    assert len({row.entity_ref for row in alerts}) == 2
    assert all(
        row.entity_ref.startswith("body_scan:create:") for row in alerts
    )
    assert {
        (row.subject_id, row.overridden_by_user_id, row.override_at is not None)
        for row in alerts
    } == {(identity.subject_id, identity.actor_user_id, True)}

    resolved = await body_scan_service.resolve_active_scoped(
        db_session,
        scope=conflict_engine.ConflictScope(
            subject_id=identity.subject_id,
            evaluation_date=SCAN_DATE,
        ),
    )
    assert resolved == [
        {
            conflict_engine.CONFLICT_ENTITY_KEY: f"body_scan:{second.id}",
            "scan": True,
            "source": Source.MANUAL.value,
        },
        {
            conflict_engine.CONFLICT_ENTITY_KEY: f"body_scan:{first.id}",
            "scan": True,
            "source": Source.MANUAL.value,
        },
    ]
    assert all(
        row[conflict_engine.CONFLICT_ENTITY_KEY] != f"body_scan:{historical.id}"
        for row in resolved
    )


async def test_owned_replay_isolates_savepoints_and_is_idempotent(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    human = _identity(legacy_owner_roots)
    system = _identity(legacy_owner_roots, system=True)
    connection = await _openrouter_connection(db_session, human.subject_id)
    _, failed = await _upload_raw(
        db_session,
        identity=human,
        connection=connection,
        suffix="replay-failed",
    )
    _, successful = await _upload_raw(
        db_session,
        identity=human,
        connection=connection,
        suffix="replay-success",
    )
    failed_id, successful_id = failed.id, successful.id
    await db_session.commit()

    original_save = body_scan_service.save_scan

    async def flaky_save(session, **kwargs):
        if kwargs.get("raw_payload_id") == failed_id:
            session.add(
                BodyScan(
                    subject_id=human.subject_id,
                    actor_user_id=human.actor_user_id,
                    date=SCAN_DATE,
                    domain=Domain.BODY_COMPOSITION.value,
                    source=Source.BODY_SCAN.value,
                    raw_payload_id=failed_id,
                )
            )
            await session.flush()
            raise RuntimeError("synthetic failure after partial BodyScan write")
        return await original_save(session, **kwargs)

    monkeypatch.setattr(body_scan_service, "save_scan", flaky_save)
    assert await body_scan_service.reparse_owned_pending(
        db_session,
        identity=system,
    ) == 1
    assert await db_session.scalar(
        select(func.count()).select_from(BodyScan).where(
            BodyScan.raw_payload_id == failed_id
        )
    ) == 0
    assert await db_session.scalar(
        select(func.count()).select_from(BodyScan).where(
            BodyScan.raw_payload_id == successful_id
        )
    ) == 1
    await db_session.refresh(failed)
    await db_session.refresh(successful)
    assert failed.processed_at is None
    assert successful.processed_at is not None
    assert await body_scan_service.reparse_owned_pending(
        db_session,
        identity=system,
    ) == 0


@pytest.mark.integration
async def test_postgres_governance_precedes_targets_and_concurrent_writers_serialize(
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
    await db_session.commit()

    session_a = factory()
    prepared_a = await _prepared_weight(session_a, identity)
    await body_scan_service.save_scan(
        session_a,
        on_date=SCAN_DATE,
        metrics=[{"label": "Phase Angle", "value": 6.0}],
        source=Source.MANUAL.value,
        identity=identity,
        prepared_weight_write=prepared_a,
    )

    async def writer_b() -> None:
        async with factory() as session_b:
            prepared_b = await _prepared_weight(
                session_b,
                identity,
                on_date=SCAN_DATE,
            )
            await body_scan_service.save_scan(
                session_b,
                on_date=SCAN_DATE,
                metrics=[{"label": "Phase Angle", "value": 6.1}],
                source=Source.MANUAL.value,
                identity=identity,
                prepared_weight_write=prepared_b,
            )
            await session_b.commit()

    task_b = asyncio.create_task(writer_b())
    await asyncio.sleep(0.25)
    assert not task_b.done(), "writer B must wait on governance before target locks"
    await session_a.commit()
    await session_a.close()
    await asyncio.wait_for(task_b, timeout=5)

    async with factory() as verify:
        scans = list(
            await verify.scalars(
                select(BodyScan).where(BodyScan.subject_id == identity.subject_id)
            )
        )
    assert len(scans) == 2
    assert {(row.date, row.source) for row in scans} == {
        (SCAN_DATE, Source.MANUAL.value)
    }


@pytest.mark.integration
async def test_postgres_concurrent_owned_replay_claims_one_raw_exactly_once(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    assert db_session.bind is not None
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    human = _identity(legacy_owner_roots)
    system = _identity(legacy_owner_roots, system=True)
    connection = await _openrouter_connection(db_session, human.subject_id)
    _, raw = await _upload_raw(
        db_session,
        identity=human,
        connection=connection,
        suffix="concurrent-replay",
        payload={
            "date": SCAN_DATE.isoformat(),
            "device": "Synthetic concurrent BIA",
            "metrics": [
                {"label": "Phase Angle", "value": 6.0},
                {
                    "label": "Visceral Fat Area",
                    "value": 90,
                    "unit": "cm2",
                },
            ],
        },
    )
    raw_id = raw.id
    await db_session.commit()

    original_prepare = weight_service.prepare_weight_write
    both_selected = asyncio.Event()
    arrivals = 0

    async def prepare_barrier(*args, **kwargs):
        nonlocal arrivals
        arrivals += 1
        if arrivals == 2:
            both_selected.set()
        await asyncio.wait_for(both_selected.wait(), timeout=5)
        return await original_prepare(*args, **kwargs)

    monkeypatch.setattr(weight_service, "prepare_weight_write", prepare_barrier)

    async def worker() -> int:
        async with factory() as session:
            done = await body_scan_service.reparse_owned_pending(
                session,
                identity=system,
            )
            await session.commit()
            return done

    results = await asyncio.wait_for(
        asyncio.gather(worker(), worker()),
        timeout=10,
    )
    assert sorted(results) == [0, 1]
    assert arrivals == 2

    async with factory() as verify:
        scans = list(
            await verify.scalars(
                select(BodyScan).where(BodyScan.raw_payload_id == raw_id)
            )
        )
        metrics = list(
            await verify.scalars(
                select(BodyScanMetric).where(
                    BodyScanMetric.scan_id.in_([row.id for row in scans])
                )
            )
        )
        persisted_raw = await verify.get(RawPayload, raw_id)
    assert len(scans) == 1
    assert len(metrics) == 2
    assert {row.subject_id for row in metrics} == {human.subject_id}
    assert persisted_raw is not None and persisted_raw.processed_at is not None
