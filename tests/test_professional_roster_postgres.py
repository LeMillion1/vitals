"""PostgreSQL proof for the professional roster's pre-bind RLS boundary."""

from __future__ import annotations

import os

import pytest
import sqlalchemy as sa
from alembic.config import Config as AlembicConfig
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.test_row_level_security import (
    REPOSITORY_ROOT,
    RESTRICTED_ROLE,
    _migrated_engine,
    restricted_engine,
)
from vitals.services.care import workspace as care_workspace
from vitals.services.care.relationships import POSTGRES_ROSTER_ROUTINE


pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_web_role_gets_only_its_bounded_roster_before_subject_binding(
    db_session,
    monkeypatch,
):
    """The projection is useful pre-bind without becoming a subject-data door."""

    database_url = os.environ["VITALS_TEST_DATABASE_URL"]
    assert database_url.startswith("postgresql")
    monkeypatch.setenv("VITALS_DATABASE_URL", database_url)
    await db_session.close()

    admin = await _migrated_engine(
        database_url,
        AlembicConfig(str(REPOSITORY_ROOT / "alembic.ini")),
    )
    restricted = await restricted_engine(database_url)
    try:
        async with admin.begin() as connection:
            ids = (
                await connection.execute(
                    sa.text(
                        "INSERT INTO users "
                        "(id, username, normalized_username, status, "
                        "created_at, updated_at) VALUES "
                        "(gen_random_uuid(), 'roster-reviewer', "
                        "'roster-reviewer', 'active', now(), now()), "
                        "(gen_random_uuid(), 'roster-doctor', "
                        "'roster-doctor', 'active', now(), now()), "
                        "(gen_random_uuid(), 'roster-other-professional', "
                        "'roster-other-professional', 'active', now(), now()), "
                        "(gen_random_uuid(), 'roster-owner-open', "
                        "'roster-owner-open', 'active', now(), now()), "
                        "(gen_random_uuid(), 'roster-owner-paused', "
                        "'roster-owner-paused', 'active', now(), now()), "
                        "(gen_random_uuid(), 'roster-owner-expired', "
                        "'roster-owner-expired', 'active', now(), now()), "
                        "(gen_random_uuid(), 'roster-owner-scope-only', "
                        "'roster-owner-scope-only', 'active', now(), now()), "
                        "(gen_random_uuid(), 'roster-owner-consent-paused', "
                        "'roster-owner-consent-paused', 'active', now(), now()), "
                        "(gen_random_uuid(), 'roster-owner-revoked', "
                        "'roster-owner-revoked', 'active', now(), now()), "
                        "(gen_random_uuid(), 'roster-owner-other', "
                        "'roster-owner-other', 'active', now(), now()) "
                        "RETURNING id, normalized_username"
                    )
                )
            ).all()
            users = {username: user_id for user_id, username in ids}
            reviewer_id = users["roster-reviewer"]
            doctor_id = users["roster-doctor"]
            other_professional_id = users["roster-other-professional"]

            await connection.execute(
                sa.text(
                    "INSERT INTO user_roles (id, user_id, role, assigned_at) "
                    "VALUES (gen_random_uuid(), :doctor, 'doctor', now()), "
                    "(gen_random_uuid(), :other_professional, 'doctor', now())"
                ),
                {
                    "doctor": doctor_id,
                    "other_professional": other_professional_id,
                },
            )
            await connection.execute(
                sa.text(
                    "INSERT INTO professional_profiles "
                    "(id, user_id, kind, verification_status, display_name, "
                    "verified_at, verified_by_user_id, created_at, updated_at) "
                    "VALUES "
                    "(gen_random_uuid(), :doctor, 'doctor', 'verified', "
                    "'Roster Doctor', now(), :reviewer, now(), now()), "
                    "(gen_random_uuid(), :other_professional, 'doctor', "
                    "'verified', 'Other Doctor', now(), :reviewer, now(), now())"
                ),
                {
                    "doctor": doctor_id,
                    "other_professional": other_professional_id,
                    "reviewer": reviewer_id,
                },
            )

            subject_rows = (
                await connection.execute(
                    sa.text(
                        "INSERT INTO health_subjects "
                        "(id, owner_user_id, display_name, timezone, "
                        "created_at, updated_at) VALUES "
                        "(gen_random_uuid(), :open_owner, 'Open Patient', "
                        "'Asia/Almaty', now(), now()), "
                        "(gen_random_uuid(), :paused_owner, 'Paused Patient', "
                        "'Asia/Almaty', now(), now()), "
                        "(gen_random_uuid(), :expired_owner, 'Expired Patient', "
                        "'Asia/Almaty', now(), now()), "
                        "(gen_random_uuid(), :scope_only_owner, "
                        "'Scope-only Patient', 'Asia/Almaty', now(), now()), "
                        "(gen_random_uuid(), :consent_paused_owner, "
                        "'Consent-paused Patient', 'Asia/Almaty', now(), now()), "
                        "(gen_random_uuid(), :revoked_owner, 'Revoked Patient', "
                        "'Asia/Almaty', now(), now()), "
                        "(gen_random_uuid(), :other_owner, 'Other Patient', "
                        "'Asia/Almaty', now(), now()) "
                        "RETURNING id, display_name"
                    ),
                    {
                        "open_owner": users["roster-owner-open"],
                        "paused_owner": users["roster-owner-paused"],
                        "expired_owner": users["roster-owner-expired"],
                        "scope_only_owner": users["roster-owner-scope-only"],
                        "consent_paused_owner": users[
                            "roster-owner-consent-paused"
                        ],
                        "revoked_owner": users["roster-owner-revoked"],
                        "other_owner": users["roster-owner-other"],
                    },
                )
            ).all()
            subjects = {name: subject_id for subject_id, name in subject_rows}

            relationship_rows = (
                await connection.execute(
                    sa.text(
                        "INSERT INTO care_relationships "
                        "(id, subject_id, subject_owner_user_id, "
                        "professional_user_id, kind, status, established_at, "
                        "created_at, updated_at) VALUES "
                        "(gen_random_uuid(), :open_subject, :open_owner, :doctor, "
                        "'doctor', 'active', now(), now(), now()), "
                        "(gen_random_uuid(), :paused_subject, :paused_owner, "
                        ":doctor, 'doctor', 'paused', now(), now(), now()), "
                        "(gen_random_uuid(), :expired_subject, :expired_owner, "
                        ":doctor, 'doctor', 'active', now(), now(), now()), "
                        "(gen_random_uuid(), :scope_only_subject, "
                        ":scope_only_owner, :doctor, 'doctor', 'active', now(), "
                        "now(), now()), "
                        "(gen_random_uuid(), :consent_paused_subject, "
                        ":consent_paused_owner, :doctor, 'doctor', 'active', "
                        "now(), now(), now()), "
                        "(gen_random_uuid(), :revoked_subject, :revoked_owner, "
                        ":doctor, 'doctor', 'active', now(), now(), now()), "
                        "(gen_random_uuid(), :other_subject, :other_owner, "
                        ":other_professional, 'doctor', 'active', now(), now(), "
                        "now()) RETURNING id, subject_id"
                    ),
                    {
                        "doctor": doctor_id,
                        "other_professional": other_professional_id,
                        "open_subject": subjects["Open Patient"],
                        "open_owner": users["roster-owner-open"],
                        "paused_subject": subjects["Paused Patient"],
                        "paused_owner": users["roster-owner-paused"],
                        "expired_subject": subjects["Expired Patient"],
                        "expired_owner": users["roster-owner-expired"],
                        "scope_only_subject": subjects["Scope-only Patient"],
                        "scope_only_owner": users["roster-owner-scope-only"],
                        "consent_paused_subject": subjects[
                            "Consent-paused Patient"
                        ],
                        "consent_paused_owner": users[
                            "roster-owner-consent-paused"
                        ],
                        "revoked_subject": subjects["Revoked Patient"],
                        "revoked_owner": users["roster-owner-revoked"],
                        "other_subject": subjects["Other Patient"],
                        "other_owner": users["roster-owner-other"],
                    },
                )
            ).all()
            relationships = {
                subject_id: relationship_id
                for relationship_id, subject_id in relationship_rows
            }

            await connection.execute(
                sa.text(
                    "INSERT INTO consent_grants "
                    "(id, relationship_id, subject_id, version, status, "
                    "granted_at, expires_at, created_at, updated_at) VALUES "
                    "(gen_random_uuid(), :open_relationship, :open_subject, 1, "
                    "'active', now(), now() + interval '1 day', now(), now()), "
                    "(gen_random_uuid(), :paused_relationship, :paused_subject, "
                    "1, 'active', now(), now() + interval '1 day', now(), now()), "
                    "(gen_random_uuid(), :expired_relationship, :expired_subject, "
                    "1, 'active', now() - interval '2 days', "
                    "now() - interval '1 day', now(), now()), "
                    "(gen_random_uuid(), :scope_only_relationship, "
                    ":scope_only_subject, 1, 'active', now(), "
                    "now() + interval '1 day', now(), now()), "
                    "(gen_random_uuid(), :other_relationship, :other_subject, 1, "
                    "'active', now(), now() + interval '1 day', now(), now())"
                ),
                {
                    "open_relationship": relationships[subjects["Open Patient"]],
                    "open_subject": subjects["Open Patient"],
                    "paused_relationship": relationships[
                        subjects["Paused Patient"]
                    ],
                    "paused_subject": subjects["Paused Patient"],
                    "expired_relationship": relationships[
                        subjects["Expired Patient"]
                    ],
                    "expired_subject": subjects["Expired Patient"],
                    "scope_only_relationship": relationships[
                        subjects["Scope-only Patient"]
                    ],
                    "scope_only_subject": subjects["Scope-only Patient"],
                    "other_relationship": relationships[subjects["Other Patient"]],
                    "other_subject": subjects["Other Patient"],
                },
            )

            await connection.execute(
                sa.text(
                    "INSERT INTO consent_grants "
                    "(id, relationship_id, subject_id, version, status, "
                    "granted_at, expires_at, paused_at, created_at, updated_at) "
                    "VALUES (gen_random_uuid(), :relationship, :subject, 1, "
                    "'paused', now(), now() + interval '1 day', now(), now(), "
                    "now())"
                ),
                {
                    "relationship": relationships[
                        subjects["Consent-paused Patient"]
                    ],
                    "subject": subjects["Consent-paused Patient"],
                },
            )
            await connection.execute(
                sa.text(
                    "INSERT INTO consent_grants "
                    "(id, relationship_id, subject_id, version, status, "
                    "granted_at, expires_at, revoked_at, created_at, updated_at) "
                    "VALUES (gen_random_uuid(), :relationship, :subject, 1, "
                    "'revoked', now() - interval '1 day', "
                    "now() + interval '1 day', now(), now(), now())"
                ),
                {
                    "relationship": relationships[subjects["Revoked Patient"]],
                    "subject": subjects["Revoked Patient"],
                },
            )

            scope_by_patient = {
                "Open Patient": ("operation", "care_team.message", "read"),
                "Paused Patient": ("operation", "care_team.message", "read"),
                "Expired Patient": ("operation", "care_team.message", "read"),
                "Scope-only Patient": ("domain", "weight", "read"),
                "Consent-paused Patient": (
                    "operation",
                    "care_team.message",
                    "read",
                ),
                "Revoked Patient": ("operation", "care_team.message", "read"),
            }
            for label, (resource_type, resource_key, action) in scope_by_patient.items():
                await connection.execute(
                    sa.text(
                        "INSERT INTO consent_scopes "
                        "(id, consent_grant_id, subject_id, resource_type, "
                        "resource_key, action, created_at) "
                        "SELECT gen_random_uuid(), consent.id, consent.subject_id, "
                        ":resource_type, :resource_key, :action, now() "
                        "FROM consent_grants AS consent "
                        "WHERE consent.relationship_id=:relationship"
                    ),
                    {
                        "action": action,
                        "relationship": relationships[subjects[label]],
                        "resource_key": resource_key,
                        "resource_type": resource_type,
                    },
                )

            for label in scope_by_patient:
                thread_id = await connection.scalar(
                    sa.text(
                        "INSERT INTO care_threads "
                        "(id, subject_id, title, opened_by_user_id, status, "
                        "created_at, updated_at) VALUES "
                        "(gen_random_uuid(), :subject, :title, :owner, 'open', "
                        "now() - interval '3 hours', now()) RETURNING id"
                    ),
                    {
                        "owner": users[
                            f"roster-owner-{label.split()[0].lower()}"
                        ],
                        "subject": subjects[label],
                        "title": f"Synthetic {label}",
                    },
                )
                await connection.execute(
                    sa.text(
                        "INSERT INTO care_thread_participants "
                        "(id, thread_id, subject_id, user_id, relationship_id, "
                        "joined_at, last_read_at, created_at, updated_at) VALUES "
                        "(gen_random_uuid(), :thread, :subject, :doctor, "
                        ":relationship, now() - interval '3 hours', "
                        "now() - interval '2 hours', now(), now())"
                    ),
                    {
                        "doctor": doctor_id,
                        "relationship": relationships[subjects[label]],
                        "subject": subjects[label],
                        "thread": thread_id,
                    },
                )
                await connection.execute(
                    sa.text(
                        "INSERT INTO care_messages "
                        "(id, thread_id, subject_id, actor_user_id, body, "
                        "created_at, updated_at) VALUES "
                        "(gen_random_uuid(), :thread, :subject, :owner, "
                        ":body, now() - interval '1 hour', now())"
                    ),
                    {
                        "body": f"private synthetic message for {label}",
                        "owner": users[
                            f"roster-owner-{label.split()[0].lower()}"
                        ],
                        "subject": subjects[label],
                        "thread": thread_id,
                    },
                )

            catalog = (
                await connection.execute(
                    sa.text(
                        "SELECT owner.rolname, language.lanname, "
                        "routine.prosecdef, routine.provolatile, routine.prokind, "
                        "routine.proleakproof, routine.proconfig, "
                        "NOT EXISTS (SELECT 1 FROM aclexplode(COALESCE("
                        "routine.proacl, acldefault('f', routine.proowner))) acl "
                        "WHERE acl.grantee=0 "
                        "AND upper(acl.privilege_type)='EXECUTE') AS no_public "
                        "FROM pg_proc routine "
                        "JOIN pg_roles owner ON owner.oid=routine.proowner "
                        "JOIN pg_language language ON language.oid=routine.prolang "
                        "WHERE routine.oid=to_regprocedure(:signature)"
                    ),
                    {"signature": POSTGRES_ROSTER_ROUTINE},
                )
            ).one()
            assert catalog.rolname == sa.engine.make_url(database_url).username
            assert catalog.lanname == "plpgsql"
            assert catalog.prosecdef
            assert catalog.provolatile in ("v", b"v")
            assert catalog.prokind in ("f", b"f")
            assert not catalog.proleakproof
            assert set(catalog.proconfig) == {
                "search_path=pg_catalog, pg_temp",
                "row_security=off",
            }
            assert catalog.no_public
            assert not await connection.scalar(
                sa.text(
                    "SELECT has_function_privilege("
                    ":role, :signature, 'EXECUTE')"
                ),
                {"role": RESTRICTED_ROLE, "signature": POSTGRES_ROSTER_ROUTINE},
            )
            await connection.exec_driver_sql(
                f"GRANT EXECUTE ON FUNCTION {POSTGRES_ROSTER_ROUTINE} "
                f"TO {RESTRICTED_ROLE}"
            )

        factory = async_sessionmaker(
            restricted,
            expire_on_commit=False,
            class_=AsyncSession,
        )
        async with factory() as unbound:
            assert await unbound.scalar(
                sa.text("SELECT count(*) FROM public.care_relationships")
            ) == 0
            direct = (
                await unbound.execute(
                    sa.text(
                        "SELECT * FROM public.project_professional_roster("
                        "CAST(:actor AS uuid))"
                    ),
                    {"actor": doctor_id},
                )
            ).mappings().all()
            assert set(direct[0]) == {
                "id",
                "subject_id",
                "kind",
                "status",
                "display_name",
                "consent_status",
                "expires_at",
                "unread_threads",
                "last_message_at",
            }
            assert {row["subject_id"] for row in direct} == {
                subjects["Open Patient"],
                subjects["Paused Patient"],
                subjects["Expired Patient"],
                subjects["Scope-only Patient"],
                subjects["Consent-paused Patient"],
                subjects["Revoked Patient"],
            }
            assert all("private synthetic message" not in str(row) for row in direct)
            assert await unbound.scalar(
                sa.text("SELECT count(*) FROM public.care_relationships")
            ) == 0

            roster = await care_workspace.load_professional_workspace(
                unbound,
                user_id=doctor_id,
            )
            assert await care_workspace.has_live_professional_relationship(
                unbound,
                professional_user_id=doctor_id,
            )
            by_name = {row.display_name: row for row in roster.patients}
            assert set(by_name) == {
                "Open Patient",
                "Paused Patient",
                "Expired Patient",
                "Scope-only Patient",
                "Consent-paused Patient",
                "Revoked Patient",
            }
            assert by_name["Open Patient"].open
            assert by_name["Open Patient"].unread_threads == 1
            assert by_name["Open Patient"].last_message_at is not None
            assert not by_name["Paused Patient"].open
            assert by_name["Paused Patient"].unread_threads == 0
            assert by_name["Paused Patient"].last_message_at is None
            assert not by_name["Expired Patient"].open
            assert by_name["Expired Patient"].consent_expired
            assert by_name["Expired Patient"].unread_threads == 0
            assert by_name["Expired Patient"].last_message_at is None
            assert by_name["Scope-only Patient"].open
            assert by_name["Scope-only Patient"].unread_threads == 0
            assert by_name["Scope-only Patient"].last_message_at is None
            assert not by_name["Consent-paused Patient"].open
            assert by_name["Consent-paused Patient"].consent_status == "paused"
            assert by_name["Consent-paused Patient"].unread_threads == 0
            assert by_name["Consent-paused Patient"].last_message_at is None
            assert not by_name["Revoked Patient"].open
            assert by_name["Revoked Patient"].consent_status is None
            assert by_name["Revoked Patient"].unread_threads == 0
            assert by_name["Revoked Patient"].last_message_at is None

            other = await care_workspace.load_professional_workspace(
                unbound,
                user_id=other_professional_id,
            )
            assert [row.subject_id for row in other.patients] == [
                subjects["Other Patient"]
            ]

        async with admin.begin() as connection:
            await connection.execute(
                sa.text("UPDATE users SET status='suspended' WHERE id=:doctor"),
                {"doctor": doctor_id},
            )
        async with factory() as inactive:
            inactive_workspace = await care_workspace.load_professional_workspace(
                inactive,
                user_id=doctor_id,
            )
            assert inactive_workspace.patients == ()
            assert not await care_workspace.has_live_professional_relationship(
                inactive,
                professional_user_id=doctor_id,
            )

        async with admin.begin() as connection:
            await connection.execute(
                sa.text("UPDATE users SET status='active' WHERE id=:doctor"),
                {"doctor": doctor_id},
            )
            await connection.execute(
                sa.text(
                    "DELETE FROM user_roles "
                    "WHERE user_id=:doctor AND role='doctor'"
                ),
                {"doctor": doctor_id},
            )
        async with factory() as role_revoked:
            revoked = await care_workspace.load_professional_workspace(
                role_revoked,
                user_id=doctor_id,
            )
            assert revoked.patients == ()

        async with admin.begin() as connection:
            await connection.execute(
                sa.text(
                    "INSERT INTO user_roles (id, user_id, role, assigned_at) "
                    "VALUES (gen_random_uuid(), :doctor, 'doctor', now())"
                ),
                {"doctor": doctor_id},
            )
            await connection.execute(
                sa.text(
                    "UPDATE professional_profiles "
                    "SET verification_status='suspended', verified_at=NULL, "
                    "verified_by_user_id=NULL, review_note='synthetic suspension' "
                    "WHERE user_id=:doctor"
                ),
                {"doctor": doctor_id},
            )
        async with factory() as profile_suspended:
            suspended = await care_workspace.load_professional_workspace(
                profile_suspended,
                user_id=doctor_id,
            )
            assert suspended.patients == ()
    finally:
        await restricted.dispose()
        await admin.dispose()
