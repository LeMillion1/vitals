"""Doctor reports — the snapshot and the row's lifecycle.

The properties that make the feature safe to hand to somebody else live here:
the document is frozen, the window cuts exactly where it says, a switched-off
module never reaches the file, progress photos never do at all, and the link's
lifetime is not the report's period.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from vitals.enums import Domain
from vitals.models.labs import LabResult
from vitals.models.supplements import Supplement
from vitals.models.weight import ProgressPhoto, WeightLog
from vitals.services import share_service
from vitals.services.modules_service import MODULE_REGISTRY
from vitals.utils.timeutils import now_local
from web.security import verify_password

ALL_ON = {k: True for k in MODULE_REGISTRY}

# A window that ends well before "today" so ``assemble_context`` never trims it
# to the last closed day — these tests are about the cut, not about the clock.
START = date(2026, 3, 1)
END = date(2026, 3, 30)


async def _seed_weights(session) -> None:
    session.add_all(
        [
            # One day before the window opens and one after it closes: neither
            # may show up in the snapshot.
            WeightLog(date=START - timedelta(days=1), domain="weight", source="manual", weight_kg=90.0),
            WeightLog(date=START, domain="weight", source="manual", weight_kg=89.0),
            WeightLog(date=date(2026, 3, 15), domain="weight", source="manual", weight_kg=87.5),
            WeightLog(date=END, domain="weight", source="manual", weight_kg=86.0),
            WeightLog(date=END + timedelta(days=1), domain="weight", source="manual", weight_kg=85.0),
        ]
    )
    await session.flush()


async def _create(session, **kwargs):
    params = dict(
        title="Endocrinologist",
        domains=[Domain.WEIGHT.value],
        period_start=START,
        period_end=END,
        enabled=ALL_ON,
    )
    params.update(kwargs)
    return await share_service.create_report(session, **params)


# ── Snapshot ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_period_cuts_exactly_at_its_edges(db_session):
    await _seed_weights(db_session)
    snap = await share_service.build_snapshot(
        db_session,
        domains=[Domain.WEIGHT.value],
        period_start=START,
        period_end=END,
        enabled=ALL_ON,
    )
    dates = [point[0] for point in snap["blocks"]["weight"]["points"]]
    assert dates == ["2026-03-01", "2026-03-15", "2026-03-30"]
    assert snap["blocks"]["weight"]["delta_kg"] == -3.0


@pytest.mark.asyncio
async def test_snapshot_is_frozen_against_later_edits(db_session):
    await _seed_weights(db_session)
    row, _ = await _create(db_session)
    await db_session.commit()
    before = json.dumps(row.snapshot, sort_keys=True)

    db_session.add(
        WeightLog(date=date(2026, 3, 20), domain="weight", source="manual", weight_kg=70.0)
    )
    await db_session.commit()
    await db_session.refresh(row)

    assert json.dumps(row.snapshot, sort_keys=True) == before


@pytest.mark.asyncio
async def test_unticked_domain_is_absent(db_session):
    await _seed_weights(db_session)
    db_session.add(
        Supplement(
            domain="supplements", source="manual",
            name="Creatine", key="creatine", dose="5 g", active=True,
        )
    )
    await db_session.flush()

    snap = await share_service.build_snapshot(
        db_session,
        domains=[Domain.WEIGHT.value],
        period_start=START,
        period_end=END,
        enabled=ALL_ON,
    )
    assert Domain.SUPPLEMENTS.value not in snap["blocks"]
    assert Domain.SUPPLEMENTS.value not in snap["domains"]


@pytest.mark.asyncio
async def test_disabled_module_cannot_be_published(db_session):
    db_session.add(
        Supplement(
            domain="supplements", source="manual",
            name="Creatine", key="creatine", dose="5 g", active=True,
        )
    )
    await db_session.flush()

    off = {**ALL_ON, "supplements": False}
    snap = await share_service.build_snapshot(
        db_session,
        domains=[Domain.SUPPLEMENTS.value, Domain.WEIGHT.value],
        period_start=START,
        period_end=END,
        enabled=off,
    )
    assert Domain.SUPPLEMENTS.value not in snap["domains"]
    assert Domain.SUPPLEMENTS.value not in snap["blocks"]
    assert Domain.SUPPLEMENTS.value not in share_service.available_domains(off)


@pytest.mark.asyncio
async def test_progress_photos_never_reach_a_snapshot(db_session):
    """Not a checkbox anywhere, so this is about the builder never reading them —
    including under the preset that asks for everything."""
    await _seed_weights(db_session)
    db_session.add(
        ProgressPhoto(
            date=date(2026, 3, 10), domain="weight", source="manual",
            file_key="secret-progress-photo.jpg",
        )
    )
    await db_session.flush()

    snap = await share_service.build_snapshot(
        db_session,
        domains=share_service.PRESETS["full"]["domains"],
        period_start=START,
        period_end=END,
        enabled=ALL_ON,
    )
    assert "secret-progress-photo.jpg" not in json.dumps(snap, ensure_ascii=False)


@pytest.mark.asyncio
async def test_labs_carry_range_and_history_and_honour_flagged_only(db_session):
    db_session.add_all(
        [
            LabResult(
                date=date(2026, 1, 5), domain="labs", source="manual",
                marker="Ферритин", value=31.0, unit="нг/мл", ref_low=30, ref_high=400,
                flag="normal",
            ),
            LabResult(
                date=date(2026, 3, 10), domain="labs", source="manual",
                marker="Ферритин", value=18.0, unit="нг/мл", ref_low=30, ref_high=400,
                flag="low",
            ),
            LabResult(
                date=date(2026, 3, 10), domain="labs", source="manual",
                marker="Глюкоза", value=5.0, unit="ммоль/л", ref_low=3.9, ref_high=6.1,
                flag="normal",
            ),
        ]
    )
    await db_session.flush()

    full = await share_service.build_snapshot(
        db_session, domains=[Domain.LABS.value],
        period_start=START, period_end=END, enabled=ALL_ON,
    )
    markers = {m["marker"]: m for m in full["blocks"]["labs"]["markers"]}
    assert set(markers) == {"Ферритин", "Глюкоза"}
    assert markers["Ферритин"]["ref_low"] == 30
    assert [p["value"] for p in markers["Ферритин"]["history"]] == [31.0]

    flagged = await share_service.build_snapshot(
        db_session, domains=[Domain.LABS.value],
        period_start=START, period_end=END, labs_flagged_only=True, enabled=ALL_ON,
    )
    assert [m["marker"] for m in flagged["blocks"]["labs"]["markers"]] == ["Ферритин"]


@pytest.mark.asyncio
async def test_labs_of_the_window_survive_a_bigger_history_after_it(db_session):
    """The block read the newest results in the table and filtered by date after the
    fact, so a report about a past window competed with everything drawn since. Past
    a couple of thousand later results the section came back empty — a document that
    says "every marker measured in the window" and carries none of them."""
    db_session.add_all(
        [
            LabResult(
                date=date(2026, 1, 5), domain="labs", source="manual",
                marker="Ферритин", value=31.0, unit="нг/мл", ref_low=30, ref_high=400,
                flag="normal",
            ),
            LabResult(
                date=date(2026, 3, 10), domain="labs", source="manual",
                marker="Ферритин", value=18.0, unit="нг/мл", ref_low=30, ref_high=400,
                flag="low",
            ),
        ]
    )
    db_session.add_all(
        [
            LabResult(
                date=END + timedelta(days=1 + i // 40), domain="labs", source="manual",
                marker=f"Маркер {i:04d}", value=1.0, flag="normal",
            )
            for i in range(2000)
        ]
    )
    await db_session.flush()

    snap = await share_service.build_snapshot(
        db_session, domains=[Domain.LABS.value],
        period_start=START, period_end=END, enabled=ALL_ON,
    )

    markers = {m["marker"]: m for m in snap["blocks"]["labs"]["markers"]}
    assert list(markers) == ["Ферритин"], "the window's marker, and nothing from after it"
    assert markers["Ферритин"]["value"] == 18.0
    assert [p["value"] for p in markers["Ферритин"]["history"]] == [31.0], (
        "the earlier reading is still found behind the window"
    )


@pytest.mark.asyncio
async def test_empty_domain_draws_no_section(db_session):
    snap = await share_service.build_snapshot(
        db_session, domains=[Domain.WEIGHT.value, Domain.LABS.value],
        period_start=START, period_end=END, enabled=ALL_ON,
    )
    assert snap["blocks"] == {}


@pytest.mark.asyncio
async def test_contents_line_names_only_the_sections_that_exist(db_session):
    """The header lists what the document holds, not what was ticked.

    A doctor who reads "Labs" in the contents and finds no such section
    concludes none were taken — when they were, just outside this window.
    """
    await _seed_weights(db_session)
    snap = await share_service.build_snapshot(
        db_session, domains=[Domain.WEIGHT.value, Domain.LABS.value],
        period_start=START, period_end=END, enabled=ALL_ON,
    )
    assert Domain.LABS.value not in snap["blocks"]
    assert snap["domains"] == [Domain.WEIGHT.value]


@pytest.mark.asyncio
async def test_body_metrics_carry_the_catalogue_unit_not_the_sheets(db_session):
    """An InBody printout is in English; the document is not."""
    from vitals.models.body_scan import BodyScan, BodyScanMetric

    scan = BodyScan(
        date=date(2026, 3, 10), domain=Domain.BODY_COMPOSITION.value,
        source="manual", device="InBody",
    )
    db_session.add(scan)
    await db_session.flush()
    db_session.add_all(
        [
            BodyScanMetric(
                scan_id=scan.id, metric_key="skeletal_muscle_mass",
                label="Skeletal Muscle Mass", value=41.7, unit="kg",
            ),
            # Unitless in the registry — the sheet's own unit is all there is.
            BodyScanMetric(
                scan_id=scan.id, metric_key="inbody_score",
                label="InBody Score", value=64.0, unit="/100",
            ),
        ]
    )
    await db_session.flush()

    snap = await share_service.build_snapshot(
        db_session, domains=[Domain.BODY_COMPOSITION.value],
        period_start=START, period_end=END, enabled=ALL_ON,
    )
    units = {
        m["label"]: m["unit"]
        for m in snap["blocks"][Domain.BODY_COMPOSITION.value]["scans"][0]["metrics"]
    }
    assert "kg" not in units.values()
    assert "кг" in units.values()
    assert "/100" in units.values()


# ── Lifecycle ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_password_is_returned_once_and_stored_only_hashed(db_session):
    await _seed_weights(db_session)
    row, password = await _create(db_session)
    await db_session.commit()

    assert verify_password(password, row.password_hash)
    assert password not in json.dumps(row.snapshot, ensure_ascii=False)
    dump = " ".join(str(v) for v in (row.token, row.password_hash, row.title, row.note))
    assert password not in dump


@pytest.mark.asyncio
async def test_tokens_are_unique(db_session):
    await _seed_weights(db_session)
    first, _ = await _create(db_session)
    second, _ = await _create(db_session)
    await db_session.commit()
    assert first.token != second.token
    assert len(first.token) >= 32


@pytest.mark.asyncio
async def test_link_lifetime_is_independent_of_the_report_period(db_session):
    await _seed_weights(db_session)
    row, _ = await _create(
        db_session,
        period_start=END - timedelta(days=179),
        period_end=END,
        expires_days=7,
    )
    await db_session.commit()

    assert (row.period_end - row.period_start).days == 179
    assert 6 <= (row.expires_at - now_local()).days <= 7


@pytest.mark.asyncio
async def test_resolve_public_hides_missing_revoked_and_expired_alike(db_session):
    await _seed_weights(db_session)
    live, _ = await _create(db_session)
    revoked, _ = await _create(db_session)
    expired, _ = await _create(db_session)
    expired.expires_at = now_local() - timedelta(days=1)
    await share_service.revoke(db_session, revoked.id)
    await db_session.commit()

    assert await share_service.resolve_public(db_session, live.token) is not None
    assert await share_service.resolve_public(db_session, revoked.token) is None
    assert await share_service.resolve_public(db_session, expired.token) is None
    assert await share_service.resolve_public(db_session, "no-such-token") is None
    assert await share_service.resolve_public(db_session, "") is None


@pytest.mark.asyncio
async def test_register_open_counts_and_timestamps(db_session):
    await _seed_weights(db_session)
    row, _ = await _create(db_session)
    await db_session.commit()
    assert row.opened_count == 0 and row.last_opened_at is None

    await share_service.register_open(db_session, row)
    await share_service.register_open(db_session, row)
    await db_session.commit()

    assert row.opened_count == 2
    assert row.last_opened_at is not None


@pytest.mark.asyncio
async def test_purge_expired_clears_the_snapshot_and_keeps_the_metadata(db_session):
    await _seed_weights(db_session)
    live, _ = await _create(db_session)
    dead, _ = await _create(db_session)
    dead.expires_at = now_local() - timedelta(minutes=1)
    await db_session.flush()

    purged = await share_service.purge_expired(db_session)
    await db_session.commit()

    assert purged == 1
    assert dead.snapshot is None
    assert dead.title and dead.period_start == START and dead.expires_at is not None
    assert live.snapshot is not None


@pytest.mark.asyncio
async def test_delete_removes_the_row(db_session):
    await _seed_weights(db_session)
    row, _ = await _create(db_session)
    await db_session.commit()

    assert await share_service.delete_report(db_session, row.id) is True
    await db_session.commit()
    assert await share_service.list_reports(db_session) == []


@pytest.mark.asyncio
async def test_symptoms_carry_the_patients_words_not_the_apps(db_session):
    """Two things a doctor must never be handed: this app's normalized key for a
    symptom ("low_heart_rate"), and a raw measurement dressed up as a 1-5
    severity ("40 of 5")."""
    from vitals.enums import SignalKind
    from vitals.models.signals import Signal

    db_session.add_all(
        [
            Signal(
                date=date(2026, 3, 12), domain="signals", source="telegram",
                kind=SignalKind.SYMPTOM.value, key="headache", batch_id="b1",
                note="голова раскалывается", value_num=4.0,
            ),
            # No wording of his own — nothing but the slug, so nothing to print.
            Signal(
                date=date(2026, 3, 13), domain="signals", source="telegram",
                kind=SignalKind.SYMPTOM.value, key="low_heart_rate", batch_id="b2", value_num=40.0,
            ),
            # A measurement the parser filed under a symptom: not a severity.
            Signal(
                date=date(2026, 3, 14), domain="signals", source="telegram",
                kind=SignalKind.SYMPTOM.value, key="pulse", batch_id="b3", note="пульс низкий",
                value_num=40.0,
            ),
        ]
    )
    await db_session.flush()

    snap = await share_service.build_snapshot(
        db_session, domains=[Domain.SIGNALS.value],
        period_start=START, period_end=END, enabled=ALL_ON,
    )
    symptoms = snap["blocks"][Domain.SIGNALS.value]["symptoms"]
    assert [s["what"] for s in symptoms] == ["голова раскалывается", "пульс низкий"]
    assert [s["severity"] for s in symptoms] == [4, None]
    assert "low_heart_rate" not in json.dumps(snap, ensure_ascii=False)
