"""Labs service tests — pure flag logic, manual entry + catalog, history,
out-of-range / retest alerts, defer-retest, and the LLM extraction → ingest path
(with a fake vision client, no network)."""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import select

from vitals.i18n import t
from vitals.models.labs import LabResult
from vitals.models.raw_payload import RawPayload
from vitals.services import alerts_service, labs_service

# asyncio_mode=auto runs the async tests; compute_flag tests stay synchronous.

DAY = date(2026, 6, 10)


# ── Pure flag logic ───────────────────────────────────────────────────────────
def test_compute_flag_classifies():
    assert labs_service.compute_flag(100, 30, 400) == "normal"
    # Wide range [30,400] (width 370): mildly out → low/high.
    assert labs_service.compute_flag(25, 30, 400) == "low"
    assert labs_service.compute_flag(450, 30, 400) == "high"
    # >half the range width beyond a bound → critical (400 + 185 = 585).
    assert labs_service.compute_flag(700, 30, 400) == "critical_high"
    # Narrow range [80,120] (width 40): 50 < 80-20 → critical_low; 70 → low.
    assert labs_service.compute_flag(50, 80, 120) == "critical_low"
    assert labs_service.compute_flag(70, 80, 120) == "low"
    # one-sided range (LDL < 3.0) → relative margin off the bound.
    assert labs_service.compute_flag(2.0, None, 3.0) == "normal"
    assert labs_service.compute_flag(3.5, None, 3.0) == "high"
    assert labs_service.compute_flag(4.5, None, 3.0) == "critical_high"
    # no range → unknown
    assert labs_service.compute_flag(5.0, None, None) is None


# ── Manual entry + catalog ────────────────────────────────────────────────────
async def test_add_result_creates_marker_and_flag(db_session, owner_write):
    r = await labs_service.add_result(
        db_session,
        on_date=DAY,
        marker="TSH",
        value=5.5,
        unit="mIU/L",
        ref_low=0.4,
        ref_high=4.0,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(DAY),
    )
    await db_session.commit()
    assert r.flag == "high"

    markers = await labs_service.list_markers(
        db_session,
        subject_id=owner_write.subject_id,
    )
    assert len(markers) == 1
    assert markers[0].name == "TSH"
    assert markers[0].ref_high == 4.0


async def test_add_result_falls_back_to_catalog_range(db_session, owner_write):
    # First result establishes the catalog range.
    await labs_service.add_result(
        db_session,
        on_date=DAY,
        marker="Ferritin",
        value=95,
        unit="ng/mL",
        ref_low=30,
        ref_high=400,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(DAY),
    )
    await db_session.commit()
    # Second result omits the range → catalog default is used to flag it.
    r = await labs_service.add_result(
        db_session,
        on_date=DAY + timedelta(days=30),
        marker="Ferritin",
        value=20,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(DAY + timedelta(days=30)),
    )
    await db_session.commit()
    assert r.ref_low == 30 and r.ref_high == 400
    assert r.flag == "low"  # below 30 but within half the wide range width


async def test_marker_history_and_latest(db_session, owner_write):
    await labs_service.add_result(
        db_session,
        on_date=DAY,
        marker="TSH",
        value=2.0,
        ref_low=0.4,
        ref_high=4.0,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(DAY),
    )
    await labs_service.add_result(
        db_session,
        on_date=DAY + timedelta(days=90),
        marker="TSH",
        value=3.0,
        ref_low=0.4,
        ref_high=4.0,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(DAY + timedelta(days=90)),
    )
    await db_session.commit()

    hist = await labs_service.marker_history(
        db_session,
        "TSH",
        subject_id=owner_write.subject_id,
    )
    assert [p["value"] for p in hist] == [2.0, 3.0]

    latest = await labs_service.latest_per_marker(
        db_session,
        subject_id=owner_write.subject_id,
    )
    assert len(latest) == 1
    assert latest[0].value == 3.0


# ── Alerts ────────────────────────────────────────────────────────────────────
async def test_refresh_alerts_raises_and_resolves(db_session, owner_write):
    await labs_service.add_result(
        db_session,
        on_date=DAY,
        marker="TSH",
        value=5.5,
        ref_low=0.4,
        ref_high=4.0,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(DAY),
    )
    await db_session.commit()
    await labs_service.refresh_alerts(
        db_session,
        on_date=DAY,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(DAY),
        subject_id=owner_write.subject_id,
    )
    await db_session.commit()

    active = await alerts_service.list_active(db_session, domain="labs", subject_id=owner_write.subject_id)
    assert any(a.alert_key == labs_service.OUT_OF_RANGE_KEY and a.entity_ref.startswith("TSH:") for a in active)

    # A later in-range value clears it.
    await labs_service.add_result(
        db_session,
        on_date=DAY + timedelta(days=90),
        marker="TSH",
        value=2.0,
        ref_low=0.4,
        ref_high=4.0,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(DAY + timedelta(days=90)),
    )
    await db_session.commit()
    await labs_service.refresh_alerts(
        db_session,
        on_date=DAY + timedelta(days=90),
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(DAY + timedelta(days=90)),
        subject_id=owner_write.subject_id,
    )
    await db_session.commit()
    active = await alerts_service.list_active(db_session, domain="labs", subject_id=owner_write.subject_id)
    assert not any(a.alert_key == labs_service.OUT_OF_RANGE_KEY for a in active)


async def test_out_of_range_alert_message_uses_localized_flag(db_session, owner_write):
    """Regression: the raw ``critical_high`` enum must not leak into the alert
    copy — the localized flag label is shown instead."""
    # 700 in a [30, 400] range → critical_high (see compute_flag tests above).
    await labs_service.add_result(
        db_session,
        on_date=DAY,
        marker="Ferritin",
        value=700,
        ref_low=30,
        ref_high=400,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(DAY),
    )
    await db_session.commit()
    await labs_service.refresh_alerts(
        db_session,
        on_date=DAY,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(DAY),
        subject_id=owner_write.subject_id,
    )
    await db_session.commit()

    active = await alerts_service.list_active(db_session, domain="labs", subject_id=owner_write.subject_id)
    alert = next(a for a in active if a.alert_key == labs_service.OUT_OF_RANGE_KEY)
    assert "critical_high" not in alert.message
    assert t("enum.flag.critical_high") in alert.message


async def test_overdue_retest_alert_and_defer(db_session, owner_write):
    await labs_service.add_result(
        db_session,
        on_date=DAY,
        marker="Ferritin",
        value=100,
        ref_low=30,
        ref_high=400,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(DAY),
    )
    marker = await labs_service.get_marker(
        db_session,
        "Ferritin",
        subject_id=owner_write.subject_id,
    )
    marker.retest_interval_days = 90
    await db_session.commit()

    later = DAY + timedelta(days=120)  # overdue
    await labs_service.refresh_alerts(
        db_session,
        on_date=later,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(later),
        subject_id=owner_write.subject_id,
    )
    await db_session.commit()
    active = await alerts_service.list_active(db_session, domain="labs", subject_id=owner_write.subject_id)
    assert any(a.alert_key == labs_service.RETEST_DUE_KEY for a in active)

    # Defer pushes it out and resolves the alert.
    await labs_service.defer_retest(
        db_session,
        "Ferritin",
        until=later + timedelta(days=30),
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(),
        subject_id=owner_write.subject_id,
    )
    await db_session.commit()
    await labs_service.refresh_alerts(
        db_session,
        on_date=later,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(later),
        subject_id=owner_write.subject_id,
    )
    await db_session.commit()
    active = await alerts_service.list_active(db_session, domain="labs", subject_id=owner_write.subject_id)
    assert not any(a.alert_key == labs_service.RETEST_DUE_KEY for a in active)


async def test_dismissed_out_of_range_alert_stays_hidden_until_new_result(db_session, owner_write):
    """Dismissing an out-of-range alert hides it forever for that result — not
    just for the rest of the day, unlike the noise/plateau alerts. Only a new
    out-of-range result for the same marker raises a fresh alert."""
    from freezegun import freeze_time

    await labs_service.add_result(
        db_session,
        on_date=DAY,
        marker="TSH",
        value=9.0,
        ref_low=0.4,
        ref_high=4.0,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(DAY),
    )
    await db_session.commit()

    with freeze_time("2026-06-10 10:00:00"):
        await labs_service.refresh_alerts(
            db_session,
            on_date=DAY,
            identity=owner_write.identity,
            prepared_conflict_write=await owner_write.write(DAY),
            subject_id=owner_write.subject_id,
        )
        await db_session.commit()
        active = await alerts_service.list_active(db_session, domain="labs", subject_id=owner_write.subject_id)
        alert = next(
            (a for a in active if a.alert_key == labs_service.OUT_OF_RANGE_KEY and a.entity_ref.startswith("TSH:")),
            None,
        )
        assert alert is not None

        # User dismisses the alert.
        await alerts_service.resolve_alert(db_session, alert.id)
        await db_session.commit()

        # Second load (same day): stays hidden.
        await labs_service.refresh_alerts(
            db_session,
            on_date=DAY,
            identity=owner_write.identity,
            prepared_conflict_write=await owner_write.write(DAY),
            subject_id=owner_write.subject_id,
        )
        await db_session.commit()
        active = await alerts_service.list_active(db_session, domain="labs", subject_id=owner_write.subject_id)
        assert not any(
            a.alert_key == labs_service.OUT_OF_RANGE_KEY and a.entity_ref.startswith("TSH:")
            for a in active
        ), "Alert should stay hidden after dismiss on the same day"

    # Next calendar day, same underlying result: still hidden — this is the
    # behavior change from the old daily-nag design.
    with freeze_time("2026-06-11 10:00:00"):
        await labs_service.refresh_alerts(
            db_session,
            on_date=DAY + timedelta(days=1),
            identity=owner_write.identity,
            prepared_conflict_write=await owner_write.write(DAY + timedelta(days=1)),
            subject_id=owner_write.subject_id,
        )
        await db_session.commit()
        active = await alerts_service.list_active(db_session, domain="labs", subject_id=owner_write.subject_id)
        assert not any(
            a.alert_key == labs_service.OUT_OF_RANGE_KEY and a.entity_ref.startswith("TSH:")
            for a in active
        ), "Alert should stay hidden indefinitely for the same result — only new data revives it"

        # A genuinely new out-of-range result for the same marker (a new upload)
        # raises a fresh alert.
        await labs_service.add_result(
            db_session,
            on_date=DAY + timedelta(days=1),
            marker="TSH",
            value=9.5,
            ref_low=0.4,
            ref_high=4.0,
            identity=owner_write.identity,
            prepared_conflict_write=await owner_write.write(DAY + timedelta(days=1)),
        )
        await db_session.commit()
        await labs_service.refresh_alerts(
            db_session,
            on_date=DAY + timedelta(days=1),
            identity=owner_write.identity,
            prepared_conflict_write=await owner_write.write(DAY + timedelta(days=1)),
            subject_id=owner_write.subject_id,
        )
        await db_session.commit()
        active = await alerts_service.list_active(db_session, domain="labs", subject_id=owner_write.subject_id)
        new_alerts = [
            a for a in active
            if a.alert_key == labs_service.OUT_OF_RANGE_KEY and a.entity_ref.startswith("TSH:")
        ]
        assert len(new_alerts) == 1, "A new upload should raise exactly one fresh alert"
        assert new_alerts[0].entity_ref != alert.entity_ref


async def test_new_out_of_range_result_supersedes_previous_alert(db_session, owner_write):
    """A new out-of-range result for the same marker resolves the (still-active,
    never dismissed) alert tied to the previous result, instead of leaving it
    active alongside a fresh duplicate."""
    await labs_service.add_result(
        db_session,
        on_date=DAY,
        marker="TSH",
        value=9.0,
        ref_low=0.4,
        ref_high=4.0,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(DAY),
    )
    await db_session.commit()
    await labs_service.refresh_alerts(
        db_session,
        on_date=DAY,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(DAY),
        subject_id=owner_write.subject_id,
    )
    await db_session.commit()
    active = await alerts_service.list_active(db_session, domain="labs", subject_id=owner_write.subject_id)
    tsh_alerts = [a for a in active if a.alert_key == labs_service.OUT_OF_RANGE_KEY and a.entity_ref.startswith("TSH:")]
    assert len(tsh_alerts) == 1
    old_entity = tsh_alerts[0].entity_ref

    await labs_service.add_result(
        db_session,
        on_date=DAY + timedelta(days=1),
        marker="TSH",
        value=9.8,
        ref_low=0.4,
        ref_high=4.0,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(DAY + timedelta(days=1)),
    )
    await db_session.commit()
    await labs_service.refresh_alerts(
        db_session,
        on_date=DAY + timedelta(days=1),
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(DAY + timedelta(days=1)),
        subject_id=owner_write.subject_id,
    )
    await db_session.commit()

    active = await alerts_service.list_active(db_session, domain="labs", subject_id=owner_write.subject_id)
    tsh_alerts = [a for a in active if a.alert_key == labs_service.OUT_OF_RANGE_KEY and a.entity_ref.startswith("TSH:")]
    assert len(tsh_alerts) == 1, "The stale alert for the old result must be superseded, not left active"
    assert tsh_alerts[0].entity_ref != old_entity


async def test_dismissed_retest_due_alert_stays_hidden_until_new_result(db_session, owner_write):
    """Same forever-until-new-data contract as out-of-range alerts applies to
    the overdue-retest reminder."""
    await labs_service.add_result(
        db_session,
        on_date=DAY,
        marker="Ferritin",
        value=100,
        ref_low=30,
        ref_high=400,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(DAY),
    )
    marker = await labs_service.get_marker(
        db_session,
        "Ferritin",
        subject_id=owner_write.subject_id,
    )
    marker.retest_interval_days = 90
    await db_session.commit()

    later = DAY + timedelta(days=120)  # overdue
    await labs_service.refresh_alerts(
        db_session,
        on_date=later,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(later),
        subject_id=owner_write.subject_id,
    )
    await db_session.commit()
    active = await alerts_service.list_active(db_session, domain="labs", subject_id=owner_write.subject_id)
    alert = next((a for a in active if a.alert_key == labs_service.RETEST_DUE_KEY), None)
    assert alert is not None

    await alerts_service.resolve_alert(db_session, alert.id)
    await db_session.commit()

    # Much later, still no new test taken: stays hidden — under the old
    # daily-nag design this would have reappeared the very next day.
    much_later = DAY + timedelta(days=200)
    await labs_service.refresh_alerts(
        db_session,
        on_date=much_later,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(much_later),
        subject_id=owner_write.subject_id,
    )
    await db_session.commit()
    active = await alerts_service.list_active(db_session, domain="labs", subject_id=owner_write.subject_id)
    assert not any(a.alert_key == labs_service.RETEST_DUE_KEY for a in active)

    # The user finally retests — once the new result in turn becomes overdue,
    # a fresh reminder is raised.
    retest_date = DAY + timedelta(days=210)
    await labs_service.add_result(
        db_session,
        on_date=retest_date,
        marker="Ferritin",
        value=90,
        ref_low=30,
        ref_high=400,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(retest_date),
    )
    await db_session.commit()
    final_check = retest_date + timedelta(days=100)
    await labs_service.refresh_alerts(
        db_session,
        on_date=final_check,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(final_check),
        subject_id=owner_write.subject_id,
    )
    await db_session.commit()
    active = await alerts_service.list_active(db_session, domain="labs", subject_id=owner_write.subject_id)
    assert any(a.alert_key == labs_service.RETEST_DUE_KEY for a in active)


# ── LLM extraction → ingest ───────────────────────────────────────────────────
class FakeLLM:
    def __init__(self, payload):
        self.payload = payload
        self.image_urls = []

    async def extract_json(self, prompt, *, system=None, image_url=None, image_urls=None, **kw):
        if image_url:
            self.image_urls.append(image_url)
        if image_urls:
            self.image_urls.extend(image_urls)
        return self.payload


async def _lab_upload(
    db_session, owner_write, platform_ai_ready, *, storage_ref, payload
):
    """The upload boundary a parsed document arrives through.

    A parser result is only as trustworthy as the document behind it, so the
    owned ingest path insists on a real file asset and the raw payload that
    cites it. Building the pair here keeps these tests about extraction.
    """
    from vitals.enums import (
        AIInvocationPurpose,
        AIInvocationSource,
        AIInvocationStatus,
        Domain,
        FileAssetPurpose,
        Source,
    )
    from vitals.models.ai import AIInvocation
    from vitals.utils.timeutils import now_local
    from vitals.services import file_asset_service, raw_payload_service

    asset = await file_asset_service.register_legacy_local(
        db_session,
        subject_id=owner_write.subject_id,
        uploaded_by_user_id=owner_write.identity.actor_user_id,
        purpose=FileAssetPurpose.LAB_DOCUMENT,
        storage_ref=storage_ref,
        media_type="image/png",
        size_bytes=19,
        content_sha256="d" * 64,
    )
    raw = await raw_payload_service.upsert_owned_raw_payload(
        db_session,
        identity=owner_write.identity,
        file_asset_id=asset.id,
        domain=Domain.LABS.value,
        source=Source.LAB_PARSER.value,
        external_id=storage_ref,
        payload=payload,
    )
    # A parser raw has to cite the paid extraction that produced it.
    db_session.add(
        AIInvocation(
            subject_id=owner_write.subject_id,
            actor_user_id=owner_write.identity.actor_user_id,
            raw_payload_id=raw.id,
            platform_integration_connection_id=platform_ai_ready.id,
            purpose=AIInvocationPurpose.LAB_DOCUMENT_PARSE.value,
            source=AIInvocationSource.WEB.value,
            model="synthetic/labs-proof",
            config_version=platform_ai_ready.config_version,
            idempotency_key=f"labs-ingest:{storage_ref}",
            quota_period_start=date(2020, 1, 1),
            quota_period_end=date(2100, 1, 1),
            reserved_cost_microunits=1,
            reserved_units=1,
            charged_cost_microunits=1,
            charged_units=1,
            status=AIInvocationStatus.SUCCEEDED.value,
            started_at=now_local(),
            finished_at=now_local(),
        )
    )
    await db_session.flush()
    return raw


async def test_extract_and_ingest(db_session, owner_write, platform_ai_ready):
    payload = {
        "date": "2026-06-10",
        "lab_name": "Synevo",
        "results": [
            {"marker": "Ferritin", "value": 95, "unit": "ng/mL", "ref_low": 30, "ref_high": 400},
            {"marker": "TSH", "value": 5.5, "unit": "mIU/L", "ref_low": 0.4, "ref_high": 4.0},
        ],
    }
    llm = FakeLLM(payload)
    extracted = await labs_service.extract_from_file(
        b"\x89PNG\r\n\x1a\n-fake-image-bytes", llm=llm, content_type="image/png"
    )
    assert extracted == payload
    assert llm.image_urls[0].startswith("data:image/png;base64,")

    raw = await _lab_upload(
        db_session, owner_write, platform_ai_ready, storage_ref="labs/doc1.png", payload=extracted
    )
    summary = await labs_service.ingest_extracted(
        db_session,
        extracted,
        file_key="labs/doc1.png",
        identity=owner_write.identity,
        existing_raw_payload=raw,
        prepared_conflict_write=await owner_write.write(date(2026, 6, 10)),
    )
    await db_session.commit()
    assert summary["created"] == 2 and summary["skipped"] == 0
    assert {r.marker for r in summary["results"]} == {"Ferritin", "TSH"}

    tsh = await labs_service.list_results(
        db_session,
        marker="TSH",
        subject_id=owner_write.subject_id,
    )
    assert tsh[0].flag == "high"
    assert tsh[0].source == "lab_parser"

    raw = (await db_session.execute(select(RawPayload).where(RawPayload.external_id == "labs/doc1.png"))).scalars().first()
    assert raw is not None and raw.processed_at is not None

    # Re-ingesting the same document dedupes.
    summary2 = await labs_service.ingest_extracted(
        db_session,
        extracted,
        file_key="labs/doc1.png",
        identity=owner_write.identity,
        existing_raw_payload=raw,
        prepared_conflict_write=await owner_write.write(date(2026, 6, 10)),
    )
    await db_session.commit()
    assert summary2["created"] == 0 and summary2["skipped"] == 2 and summary2["results"] == []
    n = (await db_session.execute(select(LabResult))).scalars().all()
    assert len(n) == 2


async def test_extract_and_ingest_multipage_pdf(db_session):
    import fitz
    doc = fitz.open()
    doc.new_page()
    doc.new_page()
    pdf_bytes = doc.write()

    payload = {
        "date": "2026-06-10",
        "lab_name": "Synevo",
        "results": [
            {"marker": "Ferritin", "value": 95, "unit": "ng/mL", "ref_low": 30, "ref_high": 400},
            {"marker": "TSH", "value": 5.5, "unit": "mIU/L", "ref_low": 0.4, "ref_high": 4.0},
        ],
    }
    llm = FakeLLM(payload)
    extracted = await labs_service.extract_from_file(
        pdf_bytes, llm=llm, content_type="application/pdf"
    )
    assert extracted == payload
    # Should have rendered and sent 2 pages
    assert len(llm.image_urls) == 2
    assert all(url.startswith("data:image/png;base64,") for url in llm.image_urls)


def test_normalize_marker():
    # Test aliases
    assert labs_service.normalize_marker("определение иммунореактивного инсулина") == "Инсулин"
    assert labs_service.normalize_marker("тиреотропный гормон (ттг)") == "ТТГ"
    assert labs_service.normalize_marker("определение холестерина общего") == "Холестерин общий"
    # Test fallback capitalization
    assert labs_service.normalize_marker("ferritin") == "Ferritin"
    assert labs_service.normalize_marker("кальций") == "Кальций"


async def test_add_result_normalizes_marker_name(db_session, owner_write):
    # Add a result with a synonym name
    r1 = await labs_service.add_result(
        db_session,
        on_date=DAY,
        marker="определение иммунореактивного инсулина",
        value=38.0,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(DAY),
    )
    # Add a result with standard name
    r2 = await labs_service.add_result(
        db_session,
        on_date=DAY + timedelta(days=1),
        marker="Инсулин",
        value=9.0,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(DAY + timedelta(days=1)),
    )
    await db_session.commit()

    # The marker names should be normalized and merged
    assert r1.marker == "Инсулин"
    assert r2.marker == "Инсулин"

    markers = await labs_service.list_markers(
        db_session,
        subject_id=owner_write.subject_id,
    )
    assert len(markers) == 1
    assert markers[0].name == "Инсулин"

    hist = await labs_service.marker_history(
        db_session,
        "Инсулин",
        subject_id=owner_write.subject_id,
    )
    assert [p["value"] for p in hist] == [38.0, 9.0]



# ── Write-path validation ─────────────────────────────────────────────────────
async def test_add_result_rejects_nameless_marker(db_session, owner_write):
    with pytest.raises(ValueError):
        await labs_service.add_result(
            db_session,
            on_date=date(2026, 6, 10),
            marker="   ",
            value=5.0,
            identity=owner_write.identity,
            prepared_conflict_write=await owner_write.write(date(2026, 6, 10)),
        )


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), 1e12])
async def test_add_result_rejects_implausible_value(db_session, bad_value, owner_write):
    """MCP and vision extraction both reach this without an HTML form in between."""
    with pytest.raises(ValueError):
        await labs_service.add_result(
            db_session,
            on_date=date(2026, 6, 10),
            marker="Ferritin",
            value=bad_value,
            identity=owner_write.identity,
            prepared_conflict_write=await owner_write.write(date(2026, 6, 10)),
        )


async def test_add_result_allows_negative_and_large_real_values(db_session, owner_write):
    """The ceiling only catches the absurd: base excess is legitimately negative
    and cell counts run into the hundreds of thousands."""
    await labs_service.add_result(
        db_session,
        on_date=date(2026, 6, 10),
        marker="Base excess",
        value=-3.5,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(date(2026, 6, 10)),
    )
    await labs_service.add_result(
        db_session,
        on_date=date(2026, 6, 10),
        marker="Platelets",
        value=250000,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(date(2026, 6, 10)),
    )
    await db_session.commit()
    assert len(await labs_service.list_results(
        db_session,
        marker="Base excess",
        subject_id=owner_write.subject_id,
    )) == 1


async def test_ingest_skips_bad_row_and_keeps_the_rest(
    db_session, owner_write, platform_ai_ready
):
    """One garbled marker must not cost the whole document."""
    extracted = {
        "date": "2026-06-11",
        "results": [
            {"marker": "Ferritin", "value": 95},
            {"marker": "Junk", "value": 1e12},
        ],
    }
    raw = await _lab_upload(
        db_session, owner_write, platform_ai_ready, storage_ref="labs/doc-bad.png", payload=extracted
    )
    summary = await labs_service.ingest_extracted(
        db_session,
        extracted,
        file_key="labs/doc-bad.png",
        identity=owner_write.identity,
        existing_raw_payload=raw,
        prepared_conflict_write=await owner_write.write(date(2026, 6, 11)),
    )
    await db_session.commit()
    assert summary["created"] == 1 and summary["skipped"] == 1


# ── An unparsed model reply still reaches raw_payloads ───────────────────────
async def test_unparsed_llm_reply_is_kept_verbatim():
    """``extract_json`` used to swallow a non-JSON reply and hand back ``{}``, so
    the row written to ``raw_payloads`` — advertised as the verbatim payload —
    held nothing at all and the failed parse could never be reviewed or redone."""
    from vitals.integrations.llm_client import LLMClient

    class _Msg:
        content = "Sorry, I cannot read this image."

    class _Resp:
        choices = [type("C", (), {"message": _Msg()})()]

    class _Completions:
        async def create(self, **kw):
            return _Resp()

    client = LLMClient()
    client._client = type(
        "FakeClient", (), {"chat": type("Chat", (), {"completions": _Completions()})()}
    )()

    out = await client.extract_json("extract", image_url="data:image/png;base64,x")
    assert out == {"_unparsed": "Sorry, I cannot read this image."}
    # Whatever comes back still has to behave like a payload dict downstream.
    assert labs_service.normalize_extracted(out) == []
