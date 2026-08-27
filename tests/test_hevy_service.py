"""Hevy service tests — sync/normalise, idempotency, re-normalisation on change,
working-weight history, progression verdicts, and the raw-payload safety net."""
from __future__ import annotations

from datetime import date

from sqlalchemy import func, select

from vitals.enums import (
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
    UserStatus,
)
from vitals.models.hevy import DOMAIN, HevyExercise, HevySet, HevyWorkout
from vitals.models.identity import HealthSubject, User
from vitals.models.raw_payload import RawPayload
from vitals.models.tenancy import IntegrationConnection
from vitals.services.hevy import queries as hevy_queries
from vitals.services.hevy import sync as hevy_sync
from vitals.analytics.progression import ADVANCE



class FakeHevyClient:
    """Duck-typed stand-in for HevyClient — returns canned workouts, no network."""

    def __init__(self, workouts):
        self._workouts = workouts
        self.is_configured = True

    async def fetch_workouts(self, *, max_pages: int = 50):
        return list(self._workouts)


def _set(index, weight, reps, type_="normal"):
    return {
        "index": index,
        "type": type_,
        "weight_kg": weight,
        "reps": reps,
        "distance_meters": None,
        "duration_seconds": None,
        "rpe": None,
    }


def _workout(wid, *, start, updated, title="Day A — Push", sets=None, template="BENCH"):
    return {
        "id": wid,
        "title": title,
        "description": "morning session",
        "start_time": start,
        "end_time": start.replace("T10", "T11"),
        "updated_at": updated,
        "exercises": [
            {
                "index": 0,
                "title": "Bench Press (Barbell)",
                "exercise_template_id": template,
                "notes": "elbows tucked",
                "superset_id": None,
                "sets": sets or [_set(0, 80.0, 10), _set(1, 80.0, 9)],
            }
        ],
    }


async def _second_hevy_scope(session):
    owner = User(
        username="hevy-query-other",
        normalized_username="hevy-query-other",
        password_hash="$synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    session.add(owner)
    await session.flush()
    subject = HealthSubject(owner_user_id=owner.id, timezone="UTC")
    session.add(subject)
    await session.flush()
    connection = IntegrationConnection(
        subject_id=subject.id,
        provider=IntegrationProvider.HEVY.value,
        connection_type=IntegrationConnectionType.ACCOUNT.value,
        external_account_discriminator="hevy-query-other",
        credential_ref="test:hevy-query-other",
        status=IntegrationConnectionStatus.ACTIVE.value,
    )
    session.add(connection)
    await session.flush()
    return subject, connection


async def test_sync_creates_workout_tree(db_session, *, hevy_owned_scope):
    client = FakeHevyClient(
        [_workout("w1", start="2026-06-10T10:00:00Z", updated="2026-06-10T11:00:00Z")]
    )
    summary = await hevy_sync.sync_owned(db_session, client, identity=hevy_owned_scope.identity, integration_connection_id=hevy_owned_scope.connection_id)
    await db_session.commit()

    assert summary == {"fetched": 1, "created": 1, "updated": 0, "skipped": 0}

    workouts = await hevy_queries.list_workouts(
        db_session, subject_id=hevy_owned_scope.subject_id
    )
    assert len(workouts) == 1
    w = workouts[0]
    assert w.external_id == "w1"
    assert w.program == "A"  # "Day A" → A
    assert w.title == "Bench Press (Barbell)" or w.title == "Day A — Push"
    assert len(w.exercises) == 1
    assert w.exercises[0].exercise_template_id == "BENCH"
    assert len(w.exercises[0].sets) == 2

    # Raw payload preserved + linked + marked processed.
    raw = (await db_session.execute(select(RawPayload))).scalars().all()
    assert len(raw) == 1
    assert raw[0].external_id == "w1"
    assert raw[0].processed_at is not None
    assert w.raw_payload_id == raw[0].id


async def test_sync_idempotent_skips_unchanged(db_session, *, hevy_owned_scope):
    wk = _workout("w1", start="2026-06-10T10:00:00Z", updated="2026-06-10T11:00:00Z")
    client = FakeHevyClient([wk])

    await hevy_sync.sync_owned(db_session, client, identity=hevy_owned_scope.identity, integration_connection_id=hevy_owned_scope.connection_id)
    await db_session.commit()
    summary2 = await hevy_sync.sync_owned(db_session, client, identity=hevy_owned_scope.identity, integration_connection_id=hevy_owned_scope.connection_id)
    await db_session.commit()

    assert summary2["skipped"] == 1
    assert summary2["created"] == 0
    assert await hevy_queries.workout_count(
        db_session, subject_id=hevy_owned_scope.subject_id
    ) == 1


async def test_public_workout_queries_are_subject_scoped(
    db_session, *, hevy_owned_scope
):
    db_session.add(
        HevyWorkout(
            subject_id=hevy_owned_scope.subject_id,
            integration_connection_id=hevy_owned_scope.connection_id,
            date=date(2026, 6, 1),
            domain=DOMAIN,
            source="hevy_api",
            external_id="query-mine",
            title="Mine",
        )
    )
    other_subject, other_connection = await _second_hevy_scope(db_session)
    db_session.add(
        HevyWorkout(
            subject_id=other_subject.id,
            integration_connection_id=other_connection.id,
            date=date(2026, 6, 20),
            domain=DOMAIN,
            source="hevy_api",
            external_id="query-theirs",
            title="Theirs",
        )
    )
    await db_session.flush()

    mine = await hevy_queries.list_workouts(
        db_session, subject_id=hevy_owned_scope.subject_id
    )
    assert [row.external_id for row in mine] == ["query-mine"]
    assert await hevy_queries.workout_count(
        db_session, subject_id=hevy_owned_scope.subject_id
    ) == 1
    assert await hevy_queries.latest_workout_date(
        db_session, subject_id=hevy_owned_scope.subject_id
    ) == date(2026, 6, 1)
    summary = await hevy_queries.workout_window_summary(
        db_session,
        subject_id=hevy_owned_scope.subject_id,
        start=date(2026, 6, 10),
        end=date(2026, 6, 30),
    )
    assert summary.current_count == 0
    assert summary.latest_date == date(2026, 6, 1)

    theirs = await hevy_queries.list_workouts(
        db_session, subject_id=other_subject.id
    )
    assert [row.external_id for row in theirs] == ["query-theirs"]


async def test_list_workouts_filters_before_order_and_limit(
    db_session, *, hevy_owned_scope
):
    db_session.add_all(
        HevyWorkout(
            subject_id=hevy_owned_scope.subject_id,
            integration_connection_id=hevy_owned_scope.connection_id,
            date=on_date,
            domain=DOMAIN,
            source="hevy_api",
            external_id=external_id,
            title=external_id,
        )
        for external_id, on_date in (
            ("before", date(2026, 5, 31)),
            ("older-in-range", date(2026, 6, 1)),
            ("newer-in-range", date(2026, 6, 2)),
            ("after", date(2026, 6, 3)),
        )
    )
    await db_session.flush()

    rows = await hevy_queries.list_workouts(
        db_session,
        subject_id=hevy_owned_scope.subject_id,
        start=date(2026, 6, 1),
        end=date(2026, 6, 2),
        limit=1,
    )

    assert [row.external_id for row in rows] == ["newer-in-range"]


async def test_sync_renormalises_changed_workout_without_orphans(db_session, *, hevy_owned_scope):
    wk = _workout(
        "w1", start="2026-06-10T10:00:00Z", updated="2026-06-10T11:00:00Z",
        sets=[_set(0, 80.0, 10), _set(1, 80.0, 10)],
    )
    await hevy_sync.sync_owned(db_session, FakeHevyClient([wk]), identity=hevy_owned_scope.identity, integration_connection_id=hevy_owned_scope.connection_id)
    await db_session.commit()

    # Same id, newer updated_at, different sets → re-normalised in place.
    wk2 = _workout(
        "w1", start="2026-06-10T10:00:00Z", updated="2026-06-10T12:30:00Z",
        sets=[_set(0, 82.5, 8)],
    )
    summary = await hevy_sync.sync_owned(db_session, FakeHevyClient([wk2]), identity=hevy_owned_scope.identity, integration_connection_id=hevy_owned_scope.connection_id)
    await db_session.commit()

    assert summary["updated"] == 1
    assert await hevy_queries.workout_count(
        db_session, subject_id=hevy_owned_scope.subject_id
    ) == 1
    # No orphaned exercises/sets — exactly one exercise + one set remain.
    n_ex = (await db_session.execute(select(func.count()).select_from(HevyExercise))).scalar()
    n_set = (await db_session.execute(select(func.count()).select_from(HevySet))).scalar()
    assert n_ex == 1
    assert n_set == 1
    # One raw payload, refreshed in place (not duplicated).
    n_raw = (await db_session.execute(select(func.count()).select_from(RawPayload))).scalar()
    assert n_raw == 1


async def test_working_weight_series_and_catalog(
    db_session, owned_by_legacy_subject
, *, hevy_owned_scope):
    client = FakeHevyClient(
        [
            _workout("w1", start="2026-06-01T10:00:00Z", updated="2026-06-01T11:00:00Z",
                     sets=[_set(0, 80.0, 10)]),
            _workout("w2", start="2026-06-08T10:00:00Z", updated="2026-06-08T11:00:00Z",
                     sets=[_set(0, 82.5, 8)]),
        ]
    )
    await hevy_sync.sync_owned(db_session, client, identity=hevy_owned_scope.identity, integration_connection_id=hevy_owned_scope.connection_id)
    await db_session.commit()

    series = await hevy_queries.working_weight_series(
        db_session, "BENCH", subject_id=owned_by_legacy_subject.subject_id
    )
    assert [p["weight_kg"] for p in series] == [80.0, 82.5]
    assert series[0]["date"] == "2026-06-01"

    catalog = await hevy_queries.exercise_catalog(
        db_session, subject_id=owned_by_legacy_subject.subject_id
    )
    assert len(catalog) == 1
    assert catalog[0]["exercise_template_id"] == "BENCH"
    assert catalog[0]["sessions"] == 2


async def test_progression_advance_when_top_of_range_hit(
    db_session, owned_by_legacy_subject
, *, hevy_owned_scope):
    from vitals.analytics.progression import ProgressionConfig

    client = FakeHevyClient(
        [
            _workout("w1", start="2026-06-08T10:00:00Z", updated="2026-06-08T11:00:00Z",
                     sets=[_set(0, 80.0, 12), _set(1, 80.0, 12), _set(2, 80.0, 12)]),
        ]
    )
    await hevy_sync.sync_owned(db_session, client, identity=hevy_owned_scope.identity, integration_connection_id=hevy_owned_scope.connection_id)
    await db_session.commit()

    verdict = await hevy_queries.progression_for_exercise(
        db_session,
        "BENCH",
        ProgressionConfig(rep_min=8, rep_max=12, increment_kg=2.5),
        subject_id=owned_by_legacy_subject.subject_id,
    )
    assert verdict is not None
    assert verdict.status == ADVANCE
    assert verdict.suggested_weight_kg == 82.5


async def test_workout_without_id_is_skipped(db_session, *, hevy_owned_scope):
    client = FakeHevyClient([{"id": "", "exercises": []}])
    summary = await hevy_sync.sync_owned(db_session, client, identity=hevy_owned_scope.identity, integration_connection_id=hevy_owned_scope.connection_id)
    await db_session.commit()
    assert summary["skipped"] == 1
    assert await hevy_queries.workout_count(
        db_session, subject_id=hevy_owned_scope.subject_id
    ) == 0
