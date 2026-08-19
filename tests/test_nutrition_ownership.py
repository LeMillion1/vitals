"""Subject/actor contracts for the Stage-2 nutrition compatibility path."""
from __future__ import annotations

from datetime import date

from sqlalchemy import select

from vitals.enums import UserStatus
from vitals.models.identity import HealthSubject, User
from vitals.models.nutrition import MealLog
from vitals.ownership import WriteIdentity
from vitals.services import nutrition_service


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


async def test_owned_create_and_reads_are_subject_isolated(db_session):
    first = await _identity(db_session, "nutrition-first")
    second = await _identity(db_session, "nutrition-second")
    on_date = date(2026, 8, 19)

    first_row = await nutrition_service.log_meal(
        db_session,
        on_date=on_date,
        name="first breakfast",
        identity=first,
    )
    second_row = await nutrition_service.log_meal(
        db_session,
        on_date=on_date,
        name="second breakfast",
        identity=second,
    )

    assert first_row.subject_id == first.subject_id
    assert first_row.actor_user_id == first.actor_user_id
    assert second_row.subject_id == second.subject_id
    assert second_row.actor_user_id == second.actor_user_id
    assert list(
        await nutrition_service.list_meals_for_date(
            db_session,
            on_date,
            subject_id=first.subject_id,
        )
    ) == [first_row]
    assert list(
        await nutrition_service.list_meals_for_date(
            db_session,
            on_date,
            subject_id=second.subject_id,
        )
    ) == [second_row]


async def test_owned_update_and_delete_reject_cross_subject_ids(db_session):
    first = await _identity(db_session, "nutrition-first")
    second = await _identity(db_session, "nutrition-second")
    on_date = date(2026, 8, 19)
    row = await nutrition_service.log_meal(
        db_session,
        on_date=on_date,
        name="private meal",
        note="original",
        identity=first,
    )

    assert (
        await nutrition_service.update_meal(
            db_session,
            row.id,
            on_date=on_date,
            name="forged update",
            note="forged",
            identity=second,
        )
        is None
    )
    assert await nutrition_service.delete_meal(
        db_session,
        row.id,
        identity=second,
    ) is False
    assert row.name == "private meal"
    assert row.note == "original"

    updated = await nutrition_service.update_meal(
        db_session,
        row.id,
        on_date=on_date,
        name="owner update",
        note="changed",
        identity=first,
    )
    assert updated is row
    assert row.actor_user_id == first.actor_user_id
    assert await nutrition_service.delete_meal(
        db_session,
        row.id,
        identity=first,
    ) is True
    assert await db_session.get(MealLog, row.id) is None


async def test_unowned_legacy_rows_require_explicit_compatibility_read(db_session):
    identity = await _identity(db_session, "nutrition-legacy-owner")
    on_date = date(2026, 8, 19)
    legacy = await nutrition_service.log_meal(
        db_session,
        on_date=on_date,
        name="legacy meal",
    )

    assert list(
        await nutrition_service.list_meals_for_date(
            db_session,
            on_date,
            subject_id=identity.subject_id,
        )
    ) == []
    assert list(
        await nutrition_service.list_meals_for_date(
            db_session,
            on_date,
            subject_id=identity.subject_id,
            include_unowned_legacy=True,
        )
    ) == [legacy]


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
