"""Fast contracts for the distinct web and worker PostgreSQL logins."""

from __future__ import annotations

import pytest
from sqlalchemy.engine import make_url

from scripts.provision_runtime_db_role import (
    PLATFORM_CAPABILITY_ROLE_PREFIX,
    RUNTIME_EXECUTE_ROUTINES,
    WORKER_EXECUTE_ROUTINES,
    platform_capability_role_name,
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

    async def fake_provision_capability(*, migration_url, web_role, worker_role):
        assert migration_url == migration
        assert web_role == "web"
        assert worker_role == "worker"
        return {"role": f"{PLATFORM_CAPABILITY_ROLE_PREFIX}42"}

    monkeypatch.setattr(
        provisioner,
        "provision_platform_scope_capability",
        fake_provision_capability,
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
    assert result["worker"]["role_memberships"] == 1
    assert result["platform_scope"]["role"].endswith("42")


@pytest.mark.parametrize("database_oid", (1, 16_384, 4_294_967_295))
def test_platform_capability_role_is_database_incarnation_specific(database_oid):
    assert platform_capability_role_name(database_oid) == (
        f"{PLATFORM_CAPABILITY_ROLE_PREFIX}{database_oid}"
    )


@pytest.mark.parametrize("database_oid", (0, -1, True, "42"))
def test_platform_capability_role_refuses_invalid_database_oid(database_oid):
    with pytest.raises(ValueError, match="database_oid"):
        platform_capability_role_name(database_oid)


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
