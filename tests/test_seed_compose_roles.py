"""Safety contracts for the disposable PostgreSQL role-smoke seeder."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from scripts import seed_care_demo


async def test_patient_seed_can_omit_every_provider_credential(db_session, monkeypatch):
    async def forbidden(*_args, **_kwargs):
        raise AssertionError("provider credentials must not be seeded")

    monkeypatch.setattr(
        seed_care_demo.provider_credentials_service,
        "set_garmin_credentials",
        forbidden,
    )
    monkeypatch.setattr(
        seed_care_demo.provider_credentials_service,
        "set_hevy_credentials",
        forbidden,
    )

    user, subject = await seed_care_demo._patient(
        db_session,
        "compose-smoke-patient",
        "Compose Smoke Patient",
        include_provider_credentials=False,
    )
    assert user.id is not None
    assert subject.owner_user_id == user.id


@pytest.mark.parametrize(
    ("extra_args", "allow", "message"),
    [
        ([], "1", "--confirm-empty-database is required"),
        (["--confirm-empty-database"], "0", "VITALS_ALLOW_SYNTHETIC_ROLE_SEED=1"),
        (
            ["--confirm-empty-database"],
            "1",
            "requires a PostgreSQL asyncpg URL",
        ),
    ],
)
def test_compose_seeder_requires_both_guards_and_postgres(
    extra_args,
    allow,
    message,
    tmp_path,
):
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": os.path.dirname(os.path.dirname(__file__)),
        "PYTHON_DOTENV_DISABLED": "1",
        "VITALS_ALLOW_SYNTHETIC_ROLE_SEED": allow,
        "VITALS_DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path / 'unsafe.db'}",
    }
    result = subprocess.run(
        [sys.executable, "scripts/seed_compose_roles.py", *extra_args],
        cwd=os.path.dirname(os.path.dirname(__file__)),
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode != 0
    assert message in result.stderr + result.stdout

