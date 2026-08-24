"""The operator's handle on registration.

``registration_service`` has always said that opening registration is a
deployment decision rather than a settings screen. What it did not have was
anything that could make the decision: no caller of ``set_stored_mode``
existed, so an installation that had been cleared and unlocked still had no way
off ``disabled``.

These check the handle, and particularly the distinction it exists to keep
visible: what an operator stored is not the same as what the installation acts
on.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import vitals.models  # noqa: F401  -- register the metadata graph
from scripts import registration_mode as cli
from vitals.models.base import Base
from vitals.services import registration_service


@pytest.fixture
async def an_installation(tmp_path, monkeypatch):
    """A database of its own, because the CLI opens its own engine.

    It is a script an operator runs against a URL, not something handed a
    session — so a test that passed it the suite's session would be testing
    something else.
    """

    url = f"sqlite+aiosqlite:///{tmp_path / 'installation.db'}"
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await engine.dispose()
    monkeypatch.setenv("VITALS_DATABASE_URL", url)
    yield url


async def _stored(url: str) -> registration_service.RegistrationMode:
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            return await registration_service.get_stored_mode(session)
    finally:
        await engine.dispose()


async def test_an_installation_that_has_never_been_configured_reads_as_closed(
    an_installation, capsys, monkeypatch
):
    monkeypatch.delenv(registration_service.REGISTRATION_UNLOCK_ENV, raising=False)

    assert await cli._run(cli._parse_args([])) == 0
    printed = capsys.readouterr()
    assert "stored=disabled" in printed.out
    assert "effective=disabled" in printed.out


async def test_setting_the_mode_is_recorded(an_installation, capsys, monkeypatch):
    monkeypatch.setenv(registration_service.REGISTRATION_UNLOCK_ENV, "1")

    assert await cli._run(cli._parse_args(["--set", "open"])) == 0
    printed = capsys.readouterr()
    assert "stored=open" in printed.out
    assert "effective=open" in printed.out
    assert await _stored(an_installation) is registration_service.RegistrationMode.OPEN


async def test_a_stored_mode_without_the_deployment_gate_says_it_does_not_apply(
    an_installation, capsys, monkeypatch
):
    """The whole point of the two-line output.

    "I set it to open and nothing happened" is otherwise a puzzle whose answer
    is one environment variable nobody printed.
    """

    monkeypatch.delenv(registration_service.REGISTRATION_UNLOCK_ENV, raising=False)

    assert await cli._run(cli._parse_args(["--set", "open"])) == 0
    printed = capsys.readouterr()
    assert "stored=open" in printed.out
    assert "effective=disabled" in printed.out
    assert registration_service.REGISTRATION_UNLOCK_ENV in printed.err
    assert await _stored(an_installation) is registration_service.RegistrationMode.OPEN


async def test_a_mode_that_is_not_one_of_the_four_is_refused_before_the_database(
    an_installation, monkeypatch
):
    """argparse refuses it, which is the earliest anything can."""

    monkeypatch.setenv(registration_service.REGISTRATION_UNLOCK_ENV, "1")
    with pytest.raises(SystemExit):
        cli._parse_args(["--set", "everybody"])
    assert (
        await _stored(an_installation) is registration_service.RegistrationMode.DISABLED
    )


async def test_a_half_built_mode_can_be_stored_and_still_refuses_at_the_door(
    an_installation, monkeypatch
):
    """Storing ``invite_only`` is not the same as it working, and the refusal
    names itself rather than falling through to ``open``."""

    monkeypatch.setenv(registration_service.REGISTRATION_UNLOCK_ENV, "1")
    assert await cli._run(cli._parse_args(["--set", "invite_only"])) == 0

    engine = create_async_engine(an_installation)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            with pytest.raises(registration_service.RegistrationClosed) as refusal:
                await registration_service.require_open_registration(session)
    finally:
        await engine.dispose()
    assert "not implemented" in str(refusal.value)


async def test_without_a_database_url_it_refuses_rather_than_guessing(
    monkeypatch, capsys
):
    monkeypatch.delenv("VITALS_DATABASE_URL", raising=False)
    assert await cli._run(cli._parse_args([])) == 2
    assert "VITALS_DATABASE_URL" in capsys.readouterr().err
