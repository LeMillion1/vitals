"""The operator's handle on registration.

``authentication.registration`` has always said that opening registration is a
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
from vitals.services.authentication import registration as registration_service


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


def _runtime_env(tmp_path, *, database_url: str, unlocked: bool):
    runtime_dir = tmp_path / "runtime-config"
    runtime_dir.mkdir(mode=0o700)
    runtime_dir.chmod(0o700)
    runtime = runtime_dir / "vitals.env"
    runtime.write_text(
        f"VITALS_DATABASE_URL={database_url}\n"
        f"VITALS_REGISTRATION_UNLOCKED={'1' if unlocked else '0'}\n",
        encoding="utf-8",
    )
    runtime.chmod(0o600)
    return runtime


def _opening_args(mode: str, runtime) -> list[str]:
    return [
        "--set",
        mode,
        "--runtime-env",
        str(runtime),
        "--confirm-web-recreated",
        cli.WEB_RECREATED_CONFIRMATION,
    ]


async def test_an_installation_that_has_never_been_configured_reads_as_closed(
    an_installation, capsys, monkeypatch
):
    monkeypatch.delenv(registration_service.REGISTRATION_UNLOCK_ENV, raising=False)

    assert await cli._run(cli._parse_args([])) == 0
    printed = capsys.readouterr()
    assert "stored=disabled" in printed.out
    assert "effective=disabled" in printed.out


async def test_setting_the_mode_is_recorded(
    an_installation, capsys, monkeypatch, tmp_path
):
    monkeypatch.delenv(registration_service.REGISTRATION_UNLOCK_ENV, raising=False)
    runtime = _runtime_env(
        tmp_path,
        database_url=an_installation,
        unlocked=True,
    )

    assert await cli._run(cli._parse_args(_opening_args("open", runtime))) == 0
    printed = capsys.readouterr()
    assert "stored=open" in printed.out
    assert "effective=open" in printed.out
    assert "runtime_gate_readback=unlocked" in printed.out
    assert await _stored(an_installation) is registration_service.RegistrationMode.OPEN


async def test_status_uses_persisted_runtime_gate_not_the_operator_shell(
    an_installation, capsys, monkeypatch, tmp_path
):
    runtime = _runtime_env(
        tmp_path,
        database_url=an_installation,
        unlocked=True,
    )
    assert await cli._run(cli._parse_args(_opening_args("open", runtime))) == 0
    capsys.readouterr()

    # The opposite shell value must not override what the recreated web
    # service reads from its owner-only runtime file.
    monkeypatch.setenv(registration_service.REGISTRATION_UNLOCK_ENV, "0")
    assert (
        await cli._run(cli._parse_args(["--runtime-env", str(runtime)]))
        == 0
    )
    printed = capsys.readouterr()
    assert "stored=open" in printed.out
    assert "effective=open" in printed.out
    assert "runtime_gate_readback=unlocked" in printed.out

    runtime.write_text(
        runtime.read_text(encoding="utf-8").replace(
            "VITALS_REGISTRATION_UNLOCKED=1",
            "VITALS_REGISTRATION_UNLOCKED=0",
        ),
        encoding="utf-8",
    )
    runtime.chmod(0o600)
    monkeypatch.setenv(registration_service.REGISTRATION_UNLOCK_ENV, "1")
    assert (
        await cli._run(cli._parse_args(["--runtime-env", str(runtime)]))
        == 0
    )
    printed = capsys.readouterr()
    assert "stored=open" in printed.out
    assert "effective=disabled" in printed.out
    assert "runtime_gate_readback=locked" in printed.out


async def test_a_non_disabled_mode_is_refused_when_runtime_gate_is_locked(
    an_installation, capsys, monkeypatch, tmp_path
):
    monkeypatch.delenv(registration_service.REGISTRATION_UNLOCK_ENV, raising=False)
    runtime = _runtime_env(
        tmp_path,
        database_url=an_installation,
        unlocked=False,
    )

    assert await cli._run(cli._parse_args(_opening_args("open", runtime))) == 2
    printed = capsys.readouterr()
    assert "readback is locked" in printed.err
    assert (
        await _stored(an_installation)
        is registration_service.RegistrationMode.DISABLED
    )


async def test_a_non_disabled_mode_requires_exact_recreate_acknowledgement(
    an_installation, capsys, tmp_path
):
    runtime = _runtime_env(
        tmp_path,
        database_url=an_installation,
        unlocked=True,
    )

    args = [
        "--set",
        "invite_only",
        "--runtime-env",
        str(runtime),
        "--confirm-web-recreated",
        "web restarted",
    ]
    assert await cli._run(cli._parse_args(args)) == 2
    assert "exact --confirm-web-recreated" in capsys.readouterr().err
    assert (
        await _stored(an_installation)
        is registration_service.RegistrationMode.DISABLED
    )


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


async def test_proof_bound_mode_can_be_stored_and_refuses_generic_admission(
    an_installation, monkeypatch, tmp_path
):
    """Storing ``invite_only`` does not let generic federation bypass its proof."""

    monkeypatch.delenv(registration_service.REGISTRATION_UNLOCK_ENV, raising=False)
    runtime = _runtime_env(
        tmp_path,
        database_url=an_installation,
        unlocked=True,
    )
    assert await cli._run(cli._parse_args(_opening_args("invite_only", runtime))) == 0
    monkeypatch.setenv(registration_service.REGISTRATION_UNLOCK_ENV, "1")

    engine = create_async_engine(an_installation)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            with pytest.raises(registration_service.RegistrationClosed) as refusal:
                await registration_service.require_open_registration(session)
    finally:
        await engine.dispose()
    assert "requires its dedicated admission flow" in str(refusal.value)


async def test_without_a_database_url_it_refuses_rather_than_guessing(
    monkeypatch, capsys
):
    monkeypatch.delenv("VITALS_DATABASE_URL", raising=False)
    assert await cli._run(cli._parse_args([])) == 2
    assert "VITALS_DATABASE_URL" in capsys.readouterr().err
