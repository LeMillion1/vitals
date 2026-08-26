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
from sqlalchemy.ext.asyncio import async_sessionmaker

from vitals.enums import Domain
from vitals.models.labs import LabResult
from vitals.models.supplements import Supplement
from vitals.models.weight import ProgressPhoto, WeightLog
from vitals.persistence.rls import bound_subject, in_platform_scope
from vitals.services import share_service
from vitals.services.modules_service import MODULE_REGISTRY
from vitals.utils.timeutils import now_local
from vitals.utils.passwords import verify_password
from web.config import get_web_config

ALL_ON = {k: True for k in MODULE_REGISTRY}

# A window that ends well before "today" so ``assemble_context`` never trims it
# to the last closed day — these tests are about the cut, not about the clock.
START = date(2026, 3, 1)
END = date(2026, 3, 30)


async def _seed_weights(session, *, legacy_owner_roots) -> None:
    session.add_all(
        [
            # One day before the window opens and one after it closes: neither
            # may show up in the snapshot.
            WeightLog(subject_id=legacy_owner_roots.subject_id, date=START - timedelta(days=1), domain="weight", source="manual", weight_kg=90.0),
            WeightLog(subject_id=legacy_owner_roots.subject_id, date=START, domain="weight", source="manual", weight_kg=89.0),
            WeightLog(subject_id=legacy_owner_roots.subject_id, date=date(2026, 3, 15), domain="weight", source="manual", weight_kg=87.5),
            WeightLog(subject_id=legacy_owner_roots.subject_id, date=END, domain="weight", source="manual", weight_kg=86.0),
            WeightLog(subject_id=legacy_owner_roots.subject_id, date=END + timedelta(days=1), domain="weight", source="manual", weight_kg=85.0),
        ]
    )
    await session.flush()


async def _prepared_owner(session):
    return await share_service.prepare_legacy_owner(
        session,
        actor_username=get_web_config().auth_username,
    )


async def _create(session, **kwargs):
    params = dict(
        title="Endocrinologist",
        domains=[Domain.WEIGHT.value],
        period_start=START,
        period_end=END,
        enabled=ALL_ON,
        prepared_owner=await _prepared_owner(session),
    )
    params.update(kwargs)
    return await share_service.create_report(session, **params)


# Every row these tests create belongs to the one person the report is about.
pytestmark = pytest.mark.usefixtures("owned_by_legacy_subject")


# ── Snapshot ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_period_cuts_exactly_at_its_edges(legacy_owner_roots, db_session):
    await _seed_weights(db_session, legacy_owner_roots=legacy_owner_roots)
    snap = await share_service.build_snapshot(
        db_session,
        prepared_owner=await _prepared_owner(db_session),
        domains=[Domain.WEIGHT.value],
        period_start=START,
        period_end=END,
        enabled=ALL_ON,
    )
    dates = [point[0] for point in snap["blocks"]["weight"]["points"]]
    assert dates == ["2026-03-01", "2026-03-15", "2026-03-30"]
    assert snap["blocks"]["weight"]["delta_kg"] == -3.0


@pytest.mark.asyncio
async def test_snapshot_is_frozen_against_later_edits(db_session, *, legacy_owner_roots):
    await _seed_weights(db_session, legacy_owner_roots=legacy_owner_roots)
    row, _ = await _create(db_session)
    await db_session.commit()
    before = json.dumps(row.snapshot, sort_keys=True)

    db_session.add(
        WeightLog(subject_id=legacy_owner_roots.subject_id, date=date(2026, 3, 20), domain="weight", source="manual", weight_kg=70.0)
    )
    await db_session.commit()
    await db_session.refresh(row)

    assert json.dumps(row.snapshot, sort_keys=True) == before


@pytest.mark.asyncio
async def test_unticked_domain_is_absent(db_session, *, legacy_owner_roots):
    await _seed_weights(db_session, legacy_owner_roots=legacy_owner_roots)
    db_session.add(
        Supplement(subject_id=legacy_owner_roots.subject_id,
            domain="supplements", source="manual",
            name="Creatine", key="creatine", dose="5 g", active=True,
        )
    )
    await db_session.flush()

    snap = await share_service.build_snapshot(
        db_session,
        prepared_owner=await _prepared_owner(db_session),
        domains=[Domain.WEIGHT.value],
        period_start=START,
        period_end=END,
        enabled=ALL_ON,
    )
    assert Domain.SUPPLEMENTS.value not in snap["blocks"]
    assert Domain.SUPPLEMENTS.value not in snap["domains"]


@pytest.mark.asyncio
async def test_disabled_module_cannot_be_published(db_session, *, legacy_owner_roots):
    db_session.add(
        Supplement(subject_id=legacy_owner_roots.subject_id,
            domain="supplements", source="manual",
            name="Creatine", key="creatine", dose="5 g", active=True,
        )
    )
    await db_session.flush()

    off = {**ALL_ON, "supplements": False}
    snap = await share_service.build_snapshot(
        db_session,
        prepared_owner=await _prepared_owner(db_session),
        domains=[Domain.SUPPLEMENTS.value, Domain.WEIGHT.value],
        period_start=START,
        period_end=END,
        enabled=off,
    )
    assert Domain.SUPPLEMENTS.value not in snap["domains"]
    assert Domain.SUPPLEMENTS.value not in snap["blocks"]
    assert Domain.SUPPLEMENTS.value not in share_service.available_domains(off)


@pytest.mark.asyncio
async def test_progress_photos_never_reach_a_snapshot(db_session, *, legacy_file_asset_id, legacy_owner_roots):
    """Not a checkbox anywhere, so this is about the builder never reading them —
    including under the preset that asks for everything."""
    await _seed_weights(db_session, legacy_owner_roots=legacy_owner_roots)
    db_session.add(
        ProgressPhoto(subject_id=legacy_owner_roots.subject_id, file_asset_id=legacy_file_asset_id,
            date=date(2026, 3, 10), domain="weight", source="manual",
            file_key="secret-progress-photo.jpg",
        )
    )
    await db_session.flush()

    snap = await share_service.build_snapshot(
        db_session,
        prepared_owner=await _prepared_owner(db_session),
        domains=share_service.PRESETS["full"]["domains"],
        period_start=START,
        period_end=END,
        enabled=ALL_ON,
    )
    assert "secret-progress-photo.jpg" not in json.dumps(snap, ensure_ascii=False)


@pytest.mark.asyncio
async def test_labs_carry_range_and_history_and_honour_flagged_only(db_session, *, legacy_owner_roots):
    db_session.add_all(
        [
            LabResult(subject_id=legacy_owner_roots.subject_id,
                date=date(2026, 1, 5), domain="labs", source="manual",
                marker="Ферритин", value=31.0, unit="нг/мл", ref_low=30, ref_high=400,
                flag="normal",
            ),
            LabResult(subject_id=legacy_owner_roots.subject_id,
                date=date(2026, 3, 10), domain="labs", source="manual",
                marker="Ферритин", value=18.0, unit="нг/мл", ref_low=30, ref_high=400,
                flag="low",
            ),
            LabResult(subject_id=legacy_owner_roots.subject_id,
                date=date(2026, 3, 10), domain="labs", source="manual",
                marker="Глюкоза", value=5.0, unit="ммоль/л", ref_low=3.9, ref_high=6.1,
                flag="normal",
            ),
        ]
    )
    await db_session.flush()

    full = await share_service.build_snapshot(
        db_session,
        prepared_owner=await _prepared_owner(db_session), domains=[Domain.LABS.value],
        period_start=START, period_end=END, enabled=ALL_ON,
    )
    markers = {m["marker"]: m for m in full["blocks"]["labs"]["markers"]}
    assert set(markers) == {"Ферритин", "Глюкоза"}
    assert markers["Ферритин"]["ref_low"] == 30
    assert [p["value"] for p in markers["Ферритин"]["history"]] == [31.0]

    flagged = await share_service.build_snapshot(
        db_session,
        prepared_owner=await _prepared_owner(db_session), domains=[Domain.LABS.value],
        period_start=START, period_end=END, labs_flagged_only=True, enabled=ALL_ON,
    )
    assert [m["marker"] for m in flagged["blocks"]["labs"]["markers"]] == ["Ферритин"]


@pytest.mark.asyncio
async def test_labs_of_the_window_survive_a_bigger_history_after_it(db_session, *, legacy_owner_roots):
    """The block read the newest results in the table and filtered by date after the
    fact, so a report about a past window competed with everything drawn since. Past
    a couple of thousand later results the section came back empty — a document that
    says "every marker measured in the window" and carries none of them."""
    db_session.add_all(
        [
            LabResult(subject_id=legacy_owner_roots.subject_id,
                date=date(2026, 1, 5), domain="labs", source="manual",
                marker="Ферритин", value=31.0, unit="нг/мл", ref_low=30, ref_high=400,
                flag="normal",
            ),
            LabResult(subject_id=legacy_owner_roots.subject_id,
                date=date(2026, 3, 10), domain="labs", source="manual",
                marker="Ферритин", value=18.0, unit="нг/мл", ref_low=30, ref_high=400,
                flag="low",
            ),
        ]
    )
    db_session.add_all(
        [
            LabResult(subject_id=legacy_owner_roots.subject_id,
                date=END + timedelta(days=1 + i // 40), domain="labs", source="manual",
                marker=f"Маркер {i:04d}", value=1.0, flag="normal",
            )
            for i in range(2000)
        ]
    )
    await db_session.flush()

    snap = await share_service.build_snapshot(
        db_session,
        prepared_owner=await _prepared_owner(db_session), domains=[Domain.LABS.value],
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
        db_session,
        prepared_owner=await _prepared_owner(db_session), domains=[Domain.WEIGHT.value, Domain.LABS.value],
        period_start=START, period_end=END, enabled=ALL_ON,
    )
    assert snap["blocks"] == {}


@pytest.mark.asyncio
async def test_contents_line_names_only_the_sections_that_exist(legacy_owner_roots, db_session):
    """The header lists what the document holds, not what was ticked.

    A doctor who reads "Labs" in the contents and finds no such section
    concludes none were taken — when they were, just outside this window.
    """
    await _seed_weights(db_session, legacy_owner_roots=legacy_owner_roots)
    snap = await share_service.build_snapshot(
        db_session,
        prepared_owner=await _prepared_owner(db_session), domains=[Domain.WEIGHT.value, Domain.LABS.value],
        period_start=START, period_end=END, enabled=ALL_ON,
    )
    assert Domain.LABS.value not in snap["blocks"]
    assert snap["domains"] == [Domain.WEIGHT.value]


@pytest.mark.asyncio
async def test_body_metrics_carry_the_catalogue_unit_not_the_sheets(db_session, *, legacy_owner_roots):
    """An InBody printout is in English; the document is not."""
    from vitals.models.body_scan import BodyScan, BodyScanMetric

    scan = BodyScan(subject_id=legacy_owner_roots.subject_id,
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
        db_session,
        prepared_owner=await _prepared_owner(db_session), domains=[Domain.BODY_COMPOSITION.value],
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
async def test_password_is_returned_once_and_stored_only_hashed(legacy_owner_roots, db_session):
    await _seed_weights(db_session, legacy_owner_roots=legacy_owner_roots)
    row, password = await _create(db_session)
    await db_session.commit()

    assert verify_password(password, row.password_hash)
    assert password not in json.dumps(row.snapshot, ensure_ascii=False)
    dump = " ".join(str(v) for v in (row.token, row.password_hash, row.title, row.note))
    assert password not in dump


@pytest.mark.asyncio
async def test_tokens_are_unique(legacy_owner_roots, db_session):
    await _seed_weights(db_session, legacy_owner_roots=legacy_owner_roots)
    first, _ = await _create(db_session)
    second, _ = await _create(db_session)
    await db_session.commit()
    assert first.token != second.token
    assert len(first.token) >= 32


@pytest.mark.asyncio
async def test_link_lifetime_is_independent_of_the_report_period(legacy_owner_roots, db_session):
    await _seed_weights(db_session, legacy_owner_roots=legacy_owner_roots)
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
async def test_resolve_public_hides_missing_revoked_and_expired_alike(
    db_session,
    legacy_owner_roots,
):
    await _seed_weights(db_session, legacy_owner_roots=legacy_owner_roots)
    owner = await _prepared_owner(db_session)
    live, _ = await _create(db_session, prepared_owner=owner)
    revoked, _ = await _create(db_session, prepared_owner=owner)
    expired, _ = await _create(db_session, prepared_owner=owner)
    expired.expires_at = now_local() - timedelta(days=1)
    await share_service.revoke(db_session, revoked.id, prepared_owner=owner)
    await db_session.commit()

    public_factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    async with public_factory() as public:
        assert await share_service.resolve_public(public, revoked.token) is None
        assert bound_subject(public) is None
        assert await share_service.resolve_public(public, expired.token) is None
        assert bound_subject(public) is None
        assert await share_service.resolve_public(public, "no-such-token") is None
        assert bound_subject(public) is None
        assert await share_service.resolve_public(public, "") is None
        assert bound_subject(public) is None
        assert not in_platform_scope(public)

        assert await share_service.resolve_public(public, live.token) is not None
        assert bound_subject(public) == legacy_owner_roots.subject_id
        assert not in_platform_scope(public)


@pytest.mark.asyncio
async def test_register_open_counts_and_timestamps(db_session, legacy_owner_roots):
    await _seed_weights(db_session, legacy_owner_roots=legacy_owner_roots)
    owner = await _prepared_owner(db_session)
    row, _ = await _create(db_session, prepared_owner=owner)
    await db_session.commit()
    assert row.opened_count == 0 and row.last_opened_at is None

    public_factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    async with public_factory() as public:
        await share_service.register_open(public, row.token)
        await share_service.register_open(public, row.token)
        assert bound_subject(public) == legacy_owner_roots.subject_id
        assert not in_platform_scope(public)
        await public.commit()
    await db_session.refresh(row)

    assert row.opened_count == 2
    assert row.last_opened_at is not None


@pytest.mark.asyncio
async def test_purge_expired_clears_the_snapshot_and_keeps_the_metadata(
    db_session,
    legacy_owner_roots,
):
    await _seed_weights(db_session, legacy_owner_roots=legacy_owner_roots)
    owner = await _prepared_owner(db_session)
    live, _ = await _create(db_session, prepared_owner=owner)
    dead, _ = await _create(db_session, prepared_owner=owner)
    dead.expires_at = now_local() - timedelta(minutes=1)
    await db_session.flush()

    purged = await share_service.purge_expired(db_session)
    await db_session.commit()

    assert purged == 1
    assert dead.snapshot is None
    assert dead.title and dead.period_start == START and dead.expires_at is not None
    assert live.snapshot is not None


@pytest.mark.asyncio
async def test_delete_removes_the_row(legacy_owner_roots, db_session):
    await _seed_weights(db_session, legacy_owner_roots=legacy_owner_roots)
    row, _ = await _create(db_session)
    await db_session.commit()

    owner = await _prepared_owner(db_session)
    assert (
        await share_service.delete_report(db_session, row.id, prepared_owner=owner)
        is True
    )
    await db_session.commit()
    assert await share_service.list_reports(
        db_session, prepared_owner=await _prepared_owner(db_session)
    ) == []
