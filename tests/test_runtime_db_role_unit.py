"""Fast contracts for the distinct web and worker PostgreSQL logins."""

from __future__ import annotations

import pytest
from sqlalchemy.engine import make_url

from scripts.provision_runtime_db_role import (
    RUNTIME_EXECUTE_ROUTINES,
    WORKER_EXECUTE_ROUTINES,
    provision_runtime_roles,
)


async def test_web_and_worker_roles_receive_exact_capabilities(monkeypatch):
    from scripts import provision_runtime_db_role as provisioner

    migration = make_url("postgresql+asyncpg://owner:secret@db/vitals")
    web = make_url("postgresql+asyncpg://web:secret@db/vitals")
    worker = make_url("postgresql+asyncpg://worker:secret@db/vitals")
    calls: list[tuple[str, tuple[str, ...]]] = []

    async def fake_provision_runtime_role(
        *, migration_url, runtime_url, execute_routines
    ):
        assert migration_url == migration
        calls.append((runtime_url.username, execute_routines))
        return {"runtime_role": runtime_url.username}

    monkeypatch.setattr(
        provisioner,
        "provision_runtime_role",
        fake_provision_runtime_role,
    )

    result = await provision_runtime_roles(
        migration_url=migration,
        web_url=web,
        worker_url=worker,
    )

    assert calls == [
        ("web", RUNTIME_EXECUTE_ROUTINES),
        ("worker", WORKER_EXECUTE_ROUTINES),
    ]
    assert result["web"]["runtime_role"] == "web"
    assert result["worker"]["runtime_role"] == "worker"


@pytest.mark.parametrize(
    ("web", "worker", "message"),
    (
        (
            "postgresql+asyncpg://owner:secret@db/vitals",
            "postgresql+asyncpg://worker:secret@db/vitals",
            "must be distinct",
        ),
        (
            "postgresql+asyncpg://web:secret@db/vitals",
            "postgresql+asyncpg://web:secret@db/vitals",
            "must be distinct",
        ),
        (
            "postgresql+asyncpg://web:secret@other/vitals",
            "postgresql+asyncpg://worker:secret@db/vitals",
            "same database",
        ),
    ),
)
async def test_web_worker_provisioning_refuses_aliases(web, worker, message):
    migration = make_url("postgresql+asyncpg://owner:secret@db/vitals")

    with pytest.raises(RuntimeError, match=message):
        await provision_runtime_roles(
            migration_url=migration,
            web_url=make_url(web),
            worker_url=make_url(worker),
        )
