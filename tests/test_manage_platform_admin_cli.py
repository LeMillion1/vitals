"""The host operator separates platform authority from every health record."""

from __future__ import annotations

import argparse

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import vitals.models  # noqa: F401 -- register the complete schema
from scripts import manage_platform_admin as cli
from vitals.enums import UserRoleName, UserStatus
from vitals.models.base import Base
from vitals.models.identity import (
    HealthSubject,
    User,
    UserFederatedIdentity,
    UserRole,
)
from vitals.services.authentication import federation
from vitals.services.authentication.provisioning import LOCKED_PASSWORD_HASH

ISSUER = "https://idp.example.test"


@pytest.fixture
async def installation(tmp_path, monkeypatch):
    url = f"sqlite+aiosqlite:///{tmp_path / 'installation.db'}"
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        owner = User(
            username="owner",
            normalized_username="owner",
            password_hash="!legacy",
            status=UserStatus.ACTIVE.value,
        )
        outsider = User(
            username="outsider",
            normalized_username="outsider",
            password_hash=LOCKED_PASSWORD_HASH,
            status=UserStatus.ACTIVE.value,
        )
        session.add_all((owner, outsider))
        await session.flush()
        session.add_all(
            (
                UserRole(user_id=owner.id, role=UserRoleName.MEMBER.value),
                UserRole(
                    user_id=owner.id,
                    role=UserRoleName.PLATFORM_SUPERADMIN.value,
                ),
                UserRole(user_id=outsider.id, role=UserRoleName.MEMBER.value),
                HealthSubject(
                    owner_user_id=owner.id,
                    display_name="Owner",
                    timezone="Asia/Almaty",
                ),
                UserFederatedIdentity(
                    user_id=owner.id,
                    issuer=ISSUER,
                    subject="provider-owner",
                ),
            )
        )
        await session.commit()
    await engine.dispose()
    monkeypatch.setenv("VITALS_DATABASE_URL", url)
    return url


def _provision_args(*, subject: str = "provider-operator") -> argparse.Namespace:
    return cli._parse_args(
        [
            "provision",
            "--actor-username",
            "owner",
            "--username",
            "platform-operator",
            "--issuer",
            ISSUER,
            "--subject",
            subject,
            "--confirm",
            cli.PROVISION_CONFIRMATION,
        ]
    )


async def _operator_graph(url: str):
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            user = await session.scalar(
                select(User).where(User.normalized_username == "platform-operator")
            )
            if user is None:
                return None
            roles = tuple(
                await session.scalars(
                    select(UserRole.role)
                    .where(UserRole.user_id == user.id)
                    .order_by(UserRole.role)
                )
            )
            subject_id = await session.scalar(
                select(HealthSubject.id).where(HealthSubject.owner_user_id == user.id)
            )
            link = await session.scalar(
                select(UserFederatedIdentity).where(
                    UserFederatedIdentity.user_id == user.id
                )
            )
            return user, roles, subject_id, link
    finally:
        await engine.dispose()


async def test_provision_creates_the_exact_recordless_linked_operator(
    installation,
    capsys,
):
    assert await cli._run(_provision_args()) == 0
    output = capsys.readouterr().out
    assert "platform_superadmin=provisioned" in output
    assert "changed=yes" in output

    user, roles, subject_id, link = await _operator_graph(installation)
    assert user.password_hash == LOCKED_PASSWORD_HASH
    assert user.status == UserStatus.ACTIVE.value
    assert roles == (UserRoleName.PLATFORM_SUPERADMIN.value,)
    assert subject_id is None
    assert (link.issuer, link.subject) == (ISSUER, "provider-operator")


async def test_provision_refuses_before_the_owner_oidc_binding(
    installation,
    capsys,
):
    engine = create_async_engine(installation)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await session.execute(UserFederatedIdentity.__table__.delete())
        await session.commit()
    await engine.dispose()

    assert await cli._run(_provision_args()) == 1
    assert "existing binding" in capsys.readouterr().err
    assert await _operator_graph(installation) is None


async def test_duplicate_provider_identity_rolls_back_the_new_user(
    installation,
    capsys,
):
    assert await cli._run(_provision_args(subject="provider-owner")) == 1
    assert "already linked" in capsys.readouterr().err
    assert await _operator_graph(installation) is None


async def _record_operator_login(url: str) -> None:
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await federation.resolve_federated_user(
                session,
                issuer=ISSUER,
                subject="provider-operator",
            )
            await session.commit()
    finally:
        await engine.dispose()


def _revoke_args(*, actor: str, target: str) -> argparse.Namespace:
    return cli._parse_args(
        [
            "revoke",
            "--actor-username",
            actor,
            "--target-username",
            target,
            "--issuer",
            ISSUER,
            "--confirm",
            cli.REVOKE_CONFIRMATION,
        ]
    )


async def test_owner_revoke_requires_and_then_accepts_operator_login_proof(
    installation,
    capsys,
):
    assert await cli._run(_provision_args()) == 0
    capsys.readouterr()
    revoke_owner = _revoke_args(actor="platform-operator", target="owner")

    assert await cli._run(revoke_owner) == 1
    assert "successful provider login after provisioning" in capsys.readouterr().err

    await _record_operator_login(installation)
    assert await cli._run(revoke_owner) == 0
    assert "platform_superadmin=revoked" in capsys.readouterr().out

    revoke_self = _revoke_args(
        actor="platform-operator",
        target="platform-operator",
    )
    assert await cli._run(revoke_self) == 1
    assert "active member who owns a health record" in capsys.readouterr().err


async def test_owner_revoke_refuses_an_operator_with_extra_authority(
    installation,
    capsys,
):
    assert await cli._run(_provision_args()) == 0
    capsys.readouterr()
    await _record_operator_login(installation)

    engine = create_async_engine(installation)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        operator_id = await session.scalar(
            select(User.id).where(User.normalized_username == "platform-operator")
        )
        session.add(
            UserRole(user_id=operator_id, role=UserRoleName.MEMBER.value)
        )
        await session.commit()
    await engine.dispose()

    revoke_owner = _revoke_args(actor="platform-operator", target="owner")
    assert await cli._run(revoke_owner) == 1
    assert "exact recordless OIDC platform operator" in capsys.readouterr().err


async def test_non_admin_actor_confirmation_and_missing_database_are_refused(
    installation,
    monkeypatch,
    capsys,
):
    unauthorized = _provision_args()
    unauthorized.actor_username = "outsider"
    assert await cli._run(unauthorized) == 1
    assert "authorization is required" in capsys.readouterr().err

    wrong_confirmation = _provision_args()
    wrong_confirmation.confirm = "yes"
    assert await cli._run(wrong_confirmation) == 2
    assert "exact confirmation" in capsys.readouterr().err

    monkeypatch.delenv("VITALS_DATABASE_URL", raising=False)
    assert await cli._run(_provision_args()) == 2
    assert "VITALS_DATABASE_URL" in capsys.readouterr().err

    engine = create_async_engine(installation)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(User)) == 2
    await engine.dispose()
