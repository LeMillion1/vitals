"""The patient's conversation door under production-shaped forced RLS."""

from __future__ import annotations

import os
import uuid
from datetime import timedelta

import pytest
import sqlalchemy as sa
from alembic.config import Config as AlembicConfig
from sqlalchemy.ext.asyncio import async_sessionmaker

from tests.test_row_level_security import (
    REPOSITORY_ROOT,
    RESTRICTED_ROLE,
    _migrated_engine,
    restricted_engine,
)
from vitals.models.care_thread import CareMessage, CareThread, CareThreadParticipant
from vitals.models.identity import HealthSubject, User, UserRole
from vitals.models.professional import (
    CareRelationship,
    ConsentGrant,
    ConsentScope,
    ProfessionalProfile,
)
from vitals.services.care.threads import MESSAGE_OPERATION
from vitals.services.care.relationships import POSTGRES_ROSTER_ROUTINE


pytestmark = pytest.mark.integration


async def test_patient_opens_only_their_own_live_conversation_with_forced_rls(
    db_session, client, monkeypatch
):
    from web.auth import create_session
    from web.config import SESSION_COOKIE
    from web.deps import get_session
    from web.main import app

    database_url = os.environ["VITALS_TEST_DATABASE_URL"]
    assert database_url.startswith("postgresql")
    monkeypatch.setenv("VITALS_DATABASE_URL", database_url)
    await db_session.close()
    admin = await _migrated_engine(
        database_url,
        AlembicConfig(str(REPOSITORY_ROOT / "alembic.ini")),
    )
    restricted = await restricted_engine(database_url)
    previous_override = app.dependency_overrides[get_session]
    try:
        async with admin.begin() as connection:
            # The ordinary web login receives this exact non-PHI projection,
            # but not the worker's installation-wide capability.
            await connection.execute(sa.text(
                f"GRANT EXECUTE ON FUNCTION {POSTGRES_ROSTER_ROUTINE} "
                f"TO {RESTRICTED_ROLE}"
            ))
        admin_sessions = async_sessionmaker(admin, expire_on_commit=False)
        async with admin_sessions() as seed:
            now = await seed.scalar(sa.select(sa.func.now()))
            owner, other_owner, doctor, reviewer = [
                User(username=name, normalized_username=name, status="active")
                for name in (
                    "conversation-owner", "conversation-other-owner",
                    "conversation-doctor", "conversation-reviewer",
                )
            ]
            seed.add_all([owner, other_owner, doctor, reviewer])
            await seed.flush()
            seed.add(UserRole(user_id=doctor.id, role="doctor"))
            seed.add(ProfessionalProfile(
                user_id=doctor.id, kind="doctor", verification_status="verified",
                display_name="Synthetic Doctor", verified_at=now,
                verified_by_user_id=reviewer.id,
            ))
            subject, other_subject = [
                HealthSubject(
                    owner_user_id=person.id, display_name=person.username,
                    timezone="Asia/Almaty",
                )
                for person in (owner, other_owner)
            ]
            seed.add_all([subject, other_subject])
            await seed.flush()
            relationship, other_relationship = [
                CareRelationship(
                    subject_id=record.id, subject_owner_user_id=person.id,
                    professional_user_id=doctor.id, kind="doctor", status="active",
                    established_at=now,
                )
                for record, person in ((subject, owner), (other_subject, other_owner))
            ]
            seed.add_all([relationship, other_relationship])
            await seed.flush()
            grant = ConsentGrant(
                relationship_id=relationship.id, subject_id=subject.id,
                version=1, status="active", granted_at=now,
                expires_at=now + timedelta(days=1),
            )
            seed.add(grant)
            await seed.flush()
            seed.add_all([
                ConsentScope(
                    consent_grant_id=grant.id, subject_id=subject.id,
                    resource_type="operation", resource_key=MESSAGE_OPERATION,
                    action=action,
                )
                for action in ("read", "message")
            ])
            await seed.commit()

        web_sessions = async_sessionmaker(restricted, expire_on_commit=False)

        async def restricted_session():
            async with web_sessions() as session:
                try:
                    yield session
                    await session.commit()
                except Exception:
                    await session.rollback()
                    raise

        app.dependency_overrides[get_session] = restricted_session
        client.cookies.set(SESSION_COOKIE, create_session(owner.username))
        async with web_sessions() as unbound:
            assert await unbound.scalar(
                sa.select(sa.func.count()).select_from(CareRelationship)
            ) == 0

        path = f"/messages/relationship/{relationship.id}"
        opened = await client.post(path)
        assert opened.status_code == 303
        assert opened.headers["location"].startswith(f"/care/{subject.id}/messages/")
        reopened = await client.post(path)
        assert reopened.status_code == 303
        assert reopened.headers["location"] == opened.headers["location"]
        thread_path = opened.headers["location"]
        thread_id = uuid.UUID(thread_path.rsplit("/", 1)[1])
        sent = await client.post(
            thread_path,
            data={"body": "Patient-owned PostgreSQL history."},
        )
        assert sent.status_code == 303
        async with admin_sessions() as inspect:
            patient_message = await inspect.scalar(
                sa.select(CareMessage).where(
                    CareMessage.thread_id == thread_id,
                    CareMessage.actor_user_id == owner.id,
                )
            )
            assert patient_message is not None
            patient_message_id = patient_message.id

        foreign = await client.post(f"/messages/relationship/{other_relationship.id}")
        missing = await client.post(f"/messages/relationship/{uuid.uuid4()}")
        assert foreign.status_code == missing.status_code == 404
        assert foreign.text == missing.text
        client.cookies.set(SESSION_COOKIE, create_session(other_owner.username))
        assert (await client.post(path)).status_code == 404

        client.cookies.set(SESSION_COOKIE, create_session(owner.username))
        async with admin_sessions() as change:
            await change.execute(
                sa.update(ConsentGrant).where(ConsentGrant.id == grant.id)
                .values(status="paused", paused_at=now)
            )
            await change.commit()
        assert (await client.post(path)).status_code == 404

        # The owner still reads the durable history, but the restricted web role
        # applies the same exact-recipient rule to every stale mutation.
        history = await client.get(thread_path, headers={"Accept": "text/html"})
        assert history.status_code == 200
        assert "Patient-owned PostgreSQL history." in history.text
        assert f'action="{thread_path}"' not in history.text
        assert f'action="{thread_path}/close"' not in history.text
        assert f"/messages/{patient_message_id}/revise" not in history.text
        stale_responses = [
            await client.post(
                thread_path,
                data={"body": "UNSENT-POSTGRES-PATIENT-TEXT"},
            ),
            await client.post(
                f"{thread_path}/messages/{patient_message_id}/revise",
                data={"body": "UNCHANGED-POSTGRES-PATIENT-TEXT"},
            ),
            await client.post(f"{thread_path}/close"),
        ]
        assert all(response.status_code == 404 for response in stale_responses)
        assert all(
            response.content == stale_responses[0].content
            for response in stale_responses
        )

        async with admin_sessions() as change:
            await change.execute(
                sa.update(CareThread)
                .where(CareThread.id == thread_id)
                .values(status="closed")
            )
            await change.commit()
        stale_reopen = await client.post(f"{thread_path}/reopen")
        assert stale_reopen.status_code == 404
        assert stale_reopen.content == stale_responses[0].content

        async with admin_sessions() as verify:
            threads = list(await verify.scalars(sa.select(CareThread)))
            assert len(threads) == 1
            assert threads[0].subject_id == subject.id
            assert threads[0].canonical_relationship_id == relationship.id
            assert await verify.scalar(
                sa.select(sa.func.count()).select_from(CareThreadParticipant)
            ) == 2
            assert await verify.scalar(
                sa.select(sa.func.count()).select_from(CareMessage)
            ) == 1
            stored_message = await verify.scalar(
                sa.select(CareMessage).where(CareMessage.id == patient_message_id)
            )
            assert stored_message is not None
            assert stored_message.body == "Patient-owned PostgreSQL history."
            assert stored_message.edited_at is None
    finally:
        app.dependency_overrides[get_session] = previous_override
        await restricted.dispose()
        await admin.dispose()
