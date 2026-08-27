"""Subject and policy contracts for reusable clinical projection slices."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from vitals.enums import Domain, Source, UserStatus
from vitals.models.identity import HealthSubject, User
from vitals.models.garmin import GarminDaily
from vitals.models.hevy import HevyWorkout
from vitals.models.labs import LabResult
from vitals.models.raw_payload import RawPayload
from vitals.models.weight import NoiseMarker, WeightLog
from vitals.services.conflicts import engine
from vitals.services.care import record_projection as care_projection
from vitals.services.emergency import projection as emergency_projection
from vitals.services.labs import results as lab_results
from vitals.services.weight import queries as weight_queries


async def _foreign_subject(db_session, *, suffix: str) -> HealthSubject:
    owner = User(
        username=f"projection-{suffix}",
        normalized_username=f"projection-{suffix}",
        password_hash="$synthetic-projection-hash",
        status=UserStatus.ACTIVE.value,
    )
    db_session.add(owner)
    await db_session.flush()
    subject = HealthSubject(
        owner_user_id=owner.id,
        display_name="Synthetic foreign projection subject",
        timezone="UTC",
    )
    db_session.add(subject)
    await db_session.flush()
    return subject


async def test_weight_projection_policies_preserve_order_caps_and_subject_scope(
    db_session,
    legacy_owner_roots,
):
    subject_id = legacy_owner_roots.subject_id
    foreign = await _foreign_subject(db_session, suffix="weight")
    db_session.add_all(
        [
            WeightLog(
                subject_id=subject_id,
                date=date(2026, 1, day),
                domain=Domain.WEIGHT.value,
                source=Source.MANUAL.value,
                weight_kg=81.0 - day,
                superseded=False,
            )
            for day in (1, 2, 3)
        ]
        + [
            WeightLog(
                subject_id=foreign.id,
                date=date(2026, 1, 4),
                domain=Domain.WEIGHT.value,
                source=Source.MANUAL.value,
                weight_kg=60.0,
                superseded=False,
            )
        ]
        + [
            NoiseMarker(
                subject_id=subject_id,
                domain=Domain.WEIGHT.value,
                source=Source.MANUAL.value,
                start_date=date(2026, 1, day),
                end_date=date(2026, 1, day),
                reason=f"Synthetic marker {day}",
            )
            for day in (1, 10, 20)
        ]
        + [
            NoiseMarker(
                subject_id=foreign.id,
                domain=Domain.WEIGHT.value,
                source=Source.MANUAL.value,
                start_date=date(2026, 1, 21),
                end_date=date(2026, 1, 21),
                reason="Foreign marker must not consume the cap",
            )
        ]
    )
    await db_session.flush()

    care = await weight_queries.care_weight_history(
        db_session,
        subject_id=subject_id,
        end=date(2026, 1, 25),
        history_limit=2,
        noise_limit=2,
    )
    emergency = await weight_queries.emergency_weight_history(
        db_session,
        subject_id=subject_id,
        end=date(2026, 1, 25),
        history_limit=2,
        noise_limit=2,
    )

    assert [row.date for row in care.rows] == [date(2026, 1, 2), date(2026, 1, 3)]
    assert [row.date for row in emergency.rows] == [
        date(2026, 1, 2),
        date(2026, 1, 3),
    ]
    assert care.history_truncated is True
    assert emergency.history_truncated is True
    assert [row.start_date for row in care.noise_markers] == [
        date(2026, 1, 10),
        date(2026, 1, 20),
    ]
    assert care.noise_truncated is False
    assert [row.start_date for row in emergency.noise_markers] == [
        date(2026, 1, 20),
        date(2026, 1, 10),
    ]
    assert emergency.noise_truncated is True


async def test_lab_projection_keeps_latest_per_marker_and_foreign_rows_out_of_caps(
    db_session,
    legacy_owner_roots,
):
    subject_id = legacy_owner_roots.subject_id
    foreign = await _foreign_subject(db_session, suffix="labs")
    db_session.add_all(
        [
            LabResult(
                subject_id=subject_id,
                date=date(2026, 2, 1),
                domain=Domain.LABS.value,
                source=Source.MANUAL.value,
                marker="A",
                value=1.0,
                flag="normal",
            ),
            LabResult(
                subject_id=subject_id,
                date=date(2026, 2, 2),
                domain=Domain.LABS.value,
                source=Source.MANUAL.value,
                marker="A",
                value=2.0,
                flag="high",
            ),
            LabResult(
                subject_id=subject_id,
                date=date(2026, 2, 2),
                domain=Domain.LABS.value,
                source=Source.MANUAL.value,
                marker="B",
                value=3.0,
                flag="low",
            ),
            LabResult(
                subject_id=foreign.id,
                date=date(2026, 2, 3),
                domain=Domain.LABS.value,
                source=Source.MANUAL.value,
                marker="A",
                value=999.0,
                flag="critical_high",
            ),
            LabResult(
                subject_id=foreign.id,
                date=date(2026, 2, 3),
                domain=Domain.LABS.value,
                source=Source.MANUAL.value,
                marker="C",
                value=999.0,
                flag="critical_high",
            ),
        ]
    )
    await db_session.flush()

    care = await lab_results.bounded_latest_results_by_marker(
        db_session,
        subject_id=subject_id,
        end=date(2026, 2, 3),
        marker_limit=2,
    )
    emergency = await lab_results.emergency_latest_results_by_marker(
        db_session,
        subject_id=subject_id,
        end=date(2026, 2, 3),
        marker_limit=2,
    )

    expected = [("A", 2.0, "high"), ("B", 3.0, "low")]
    assert [(row.marker, row.value, row.flag) for row in care.rows] == expected
    assert [(row.marker, row.value, row.flag) for row in emergency.rows] == expected
    assert care.truncated is False
    assert emergency.truncated is False
    assert all(not hasattr(row, "raw_payload_id") for row in emergency.rows)
    assert all(not hasattr(row, "note") for row in emergency.rows)


async def test_lab_care_provenance_stays_fail_closed_while_emergency_stays_normalized(
    db_session,
    legacy_owner_roots,
):
    foreign = await _foreign_subject(db_session, suffix="raw")
    raw = RawPayload(
        subject_id=foreign.id,
        actor_user_id=None,
        domain=Domain.LABS.value,
        source=Source.MANUAL.value,
        payload={"synthetic": "foreign raw bytes"},
    )
    db_session.add(raw)
    await db_session.flush()
    db_session.add(
        LabResult(
            subject_id=legacy_owner_roots.subject_id,
            date=date(2026, 3, 1),
            domain=Domain.LABS.value,
            source=Source.MANUAL.value,
            marker="Synthetic marker",
            value=4.0,
            flag="normal",
            raw_payload_id=raw.id,
        )
    )
    await db_session.flush()

    with pytest.raises(engine.ConflictRawOwnershipError):
        await lab_results.bounded_latest_results_by_marker(
            db_session,
            subject_id=legacy_owner_roots.subject_id,
            end=date(2026, 3, 2),
        )

    emergency = await lab_results.emergency_latest_results_by_marker(
        db_session,
        subject_id=legacy_owner_roots.subject_id,
        end=date(2026, 3, 2),
    )
    assert [(row.marker, row.value) for row in emergency.rows] == [
        ("Synthetic marker", 4.0)
    ]


async def test_provider_summaries_preserve_both_audience_mapping_contracts(
    db_session,
    legacy_owner_roots,
    garmin_connection_id,
    hevy_connection_id,
):
    subject_id = legacy_owner_roots.subject_id
    db_session.add_all(
        [
            GarminDaily(
                subject_id=subject_id,
                integration_connection_id=garmin_connection_id,
                date=date(2026, 4, 8),
                domain=Domain.GARMIN.value,
                source=Source.GARMIN_API.value,
                sleep_score=50,
                body_battery_high=30,
            ),
            # A persisted placeholder is neither counted nor selected as latest.
            GarminDaily(
                subject_id=subject_id,
                integration_connection_id=garmin_connection_id,
                date=date(2026, 4, 9),
                domain=Domain.GARMIN.value,
                source=Source.GARMIN_API.value,
            ),
            HevyWorkout(
                subject_id=subject_id,
                integration_connection_id=hevy_connection_id,
                external_id="clinical-old",
                date=date(2026, 4, 1),
                domain=Domain.WORKOUTS.value,
                source=Source.HEVY_API.value,
            ),
            HevyWorkout(
                subject_id=subject_id,
                integration_connection_id=hevy_connection_id,
                external_id="clinical-current",
                date=date(2026, 4, 7),
                domain=Domain.WORKOUTS.value,
                source=Source.HEVY_API.value,
            ),
        ]
    )
    await db_session.flush()
    current_window = SimpleNamespace(
        period_start=date(2026, 4, 5),
        period_end=date(2026, 4, 10),
    )

    care_garmin = await care_projection._load_garmin(
        db_session, subject_id, current_window
    )
    emergency_garmin = await emergency_projection._garmin(
        db_session, subject_id, current_window
    )
    assert care_garmin.value == emergency_garmin.value
    assert care_garmin.value["total_days_logged"] == 1
    assert care_garmin.value["advice"] is not None
    assert care_garmin.row_count == emergency_garmin.row_count == 1
    assert care_garmin.dates == emergency_garmin.dates == (date(2026, 4, 8),)

    care_hevy = await care_projection._load_hevy(
        db_session, subject_id, current_window
    )
    emergency_hevy = await emergency_projection._hevy(
        db_session, subject_id, current_window
    )
    assert care_hevy.value == emergency_hevy.value == {
        "total_workouts": 1,
        "last_workout": "2026-04-07",
    }
    assert care_hevy.row_count == emergency_hevy.row_count == 1
    assert care_hevy.dates == emergency_hevy.dates == (date(2026, 4, 7),)

    historical_window = SimpleNamespace(
        period_start=date(2026, 4, 9),
        period_end=date(2026, 4, 10),
    )
    historical = await emergency_projection._hevy(
        db_session, subject_id, historical_window
    )
    assert historical.value == {
        "total_workouts": 0,
        "last_workout": "2026-04-07",
    }
    assert historical.row_count == 1
    assert historical.dates == (date(2026, 4, 7),)
