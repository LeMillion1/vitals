"""The one-shot account operator uses production-safe subject authority."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import vitals.models  # noqa: F401 -- register the complete schema
from scripts import provision_account as cli
from vitals.models.base import Base
from vitals.models.identity import HealthSubject, User, UserRole
from vitals.models.tenancy import IntegrationConnection
from vitals.persistence.rls import bound_subject
from vitals.services.authentication import provisioning


@pytest.fixture
async def an_installation(tmp_path, monkeypatch):
    url = f"sqlite+aiosqlite:///{tmp_path / 'installation.db'}"
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await engine.dispose()
    monkeypatch.setenv("VITALS_DATABASE_URL", url)
    return url


@pytest.mark.parametrize(
    ("argv", "expected_roles"),
    [
        (["--username", "cli-member"], ("member",)),
        (
            ["--username", "cli-doctor-record", "--role", "doctor", "--own-record"],
            ("doctor",),
        ),
    ],
)
async def test_record_owning_cli_uses_the_bound_runtime_path(
    an_installation,
    monkeypatch,
    capsys,
    argv,
    expected_roles,
):
    monkeypatch.setenv("VITALS_TIMEZONE", "Asia/Almaty")
    original = provisioning.provision_bound_account
    observed: list[tuple[object, object]] = []

    async def observe(session, **kwargs):
        result = await original(session, **kwargs)
        observed.append((bound_subject(session), result.subject_id))
        return result

    monkeypatch.setattr(provisioning, "provision_bound_account", observe)

    assert await cli._run(cli._parse_args(argv)) == 0
    assert len(observed) == 1
    assert observed[0][0] == observed[0][1]
    printed = capsys.readouterr().out
    assert "subject_id=-" not in printed
    assert f"roles={','.join(expected_roles)}" in printed

    engine = create_async_engine(an_installation)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            user_id = await session.scalar(
                select(User.id).where(User.username == argv[1])
            )
            subject_id = await session.scalar(
                select(HealthSubject.id).where(HealthSubject.owner_user_id == user_id)
            )
            assert subject_id is not None
            assert await session.scalar(
                select(HealthSubject.timezone).where(HealthSubject.id == subject_id)
            ) == "Asia/Almaty"
            assert tuple(
                await session.scalars(
                    select(UserRole.role)
                    .where(UserRole.user_id == user_id)
                    .order_by(UserRole.role)
                )
            ) == expected_roles
            assert await session.scalar(
                select(func.count()).select_from(IntegrationConnection).where(
                    IntegrationConnection.subject_id == subject_id
                )
            ) == 4
    finally:
        await engine.dispose()


async def test_professional_without_record_stays_on_the_unbound_path(
    an_installation,
    monkeypatch,
    capsys,
):
    original = provisioning.provision_account
    observed: list[tuple[object, bool]] = []

    async def observe(session, **kwargs):
        result = await original(session, **kwargs)
        observed.append((bound_subject(session), kwargs["with_health_record"]))
        return result

    monkeypatch.setattr(provisioning, "provision_account", observe)

    assert await cli._run(
        cli._parse_args(["--username", "cli-doctor", "--role", "doctor"])
    ) == 0
    assert observed == [(None, False)]
    printed = capsys.readouterr().out
    assert "subject_id=-" in printed
    assert "roles=doctor" in printed


async def test_cli_without_database_url_refuses_before_connecting(monkeypatch, capsys):
    monkeypatch.delenv("VITALS_DATABASE_URL", raising=False)
    assert await cli._run(cli._parse_args(["--username", "nobody"])) == 2
    assert "VITALS_DATABASE_URL" in capsys.readouterr().err
