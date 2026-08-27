"""Subject/actor contracts for the Stage-2 nutrition compatibility path."""
from __future__ import annotations

from vitals.services.nutrition import queries as nutrition_queries
from vitals.services.nutrition import writes as nutrition_writes

from datetime import date

from sqlalchemy import select

from vitals.enums import Domain, Source, UserStatus
from vitals.models.identity import HealthSubject, User
from vitals.models.nutrition import MealLog
from vitals.ownership import WriteIdentity

from vitals.services.conflicts import engine


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


async def _prepared(session, identity: WriteIdentity, on_date: date):
    context = engine.ConflictWriteContext(
        identity=identity,
        evaluation_date=on_date,
    )
    return await engine.prepare_scoped_write(session, context=context)


async def test_owned_create_and_reads_are_subject_isolated(db_session):
    first = await _identity(db_session, "nutrition-first")
    second = await _identity(db_session, "nutrition-second")
    on_date = date(2026, 8, 19)
    first_prepared = await _prepared(db_session, first, on_date)
    second_prepared = await _prepared(db_session, second, on_date)

    first_row = await nutrition_writes.log_meal(
        db_session,
        on_date=on_date,
        name="first breakfast",
        identity=first,
        prepared_conflict_write=first_prepared,
    )
    second_row = await nutrition_writes.log_meal(
        db_session,
        on_date=on_date,
        name="second breakfast",
        identity=second,
        prepared_conflict_write=second_prepared,
    )

    assert first_row.subject_id == first.subject_id
    assert first_row.actor_user_id == first.actor_user_id
    assert second_row.subject_id == second.subject_id
    assert second_row.actor_user_id == second.actor_user_id
    assert list(
        await nutrition_queries.list_meals_for_date(
            db_session,
            on_date,
            subject_id=first.subject_id,
        )
    ) == [first_row]
    assert list(
        await nutrition_queries.list_meals_for_date(
            db_session,
            on_date,
            subject_id=second.subject_id,
        )
    ) == [second_row]


async def test_owned_update_and_delete_reject_cross_subject_ids(db_session):
    first = await _identity(db_session, "nutrition-first")
    second = await _identity(db_session, "nutrition-second")
    on_date = date(2026, 8, 19)
    first_prepared = await _prepared(db_session, first, on_date)
    second_prepared = await _prepared(db_session, second, on_date)
    row = await nutrition_writes.log_meal(
        db_session,
        on_date=on_date,
        name="private meal",
        note="original",
        identity=first,
        prepared_conflict_write=first_prepared,
    )

    assert (
        await nutrition_writes.update_meal(
            db_session,
            row.id,
            on_date=on_date,
            name="forged update",
            note="forged",
            identity=second,
            prepared_conflict_write=second_prepared,
        )
        is None
    )
    assert await nutrition_writes.delete_meal(
        db_session,
        row.id,
        identity=second,
        prepared_conflict_write=second_prepared,
    ) is False
    assert row.name == "private meal"
    assert row.note == "original"

    updated = await nutrition_writes.update_meal(
        db_session,
        row.id,
        on_date=on_date,
        name="owner update",
        note="changed",
        identity=first,
        prepared_conflict_write=first_prepared,
    )
    assert updated is row
    assert row.actor_user_id == first.actor_user_id
    assert await nutrition_writes.delete_meal(
        db_session,
        row.id,
        identity=first,
        prepared_conflict_write=first_prepared,
    ) is True
    assert await db_session.get(MealLog, row.id) is None


async def test_unowned_legacy_rows_are_outside_every_scope(db_session, *, legacy_owner_roots):
    """The compatibility read is gone, so a meal belonging to nobody is nobody's.

    While nutrition still had a bridge, the sole owner could ask for unowned
    rows explicitly. Closing the domain removes the question: the subject is the
    whole scope, and a row without one is invisible to every reader.
    """
    identity = await _identity(db_session, "nutrition-legacy-owner")
    on_date = date(2026, 8, 19)
    legacy = MealLog(subject_id=legacy_owner_roots.subject_id,
        domain=Domain.NUTRITION.value,
        source=Source.MANUAL.value,
        date=on_date,
        name="legacy meal",
    )
    db_session.add(legacy)
    await db_session.flush()

    assert list(
        await nutrition_queries.list_meals_for_date(
            db_session,
            on_date,
            subject_id=identity.subject_id,
        )
    ) == []


async def test_web_create_uses_authenticated_legacy_owner_context(
    auth_client,
    db_session,
):
    response = await auth_client.post(
        "/nutrition/meal",
        data={
            "date": "2026-08-19",
            "name": "owned web meal",
            "calories": "450",
        },
    )

    assert response.status_code == 303
    row = await db_session.scalar(
        select(MealLog).where(MealLog.name == "owned web meal")
    )
    subject = await db_session.scalar(select(HealthSubject))
    assert row is not None and subject is not None
    assert row.subject_id == subject.id
    assert row.actor_user_id == subject.owner_user_id
