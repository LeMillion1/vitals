"""Rows written through the connector say so.

Everything a conversation saved used to land as ``manual``, indistinguishable
from a row typed into the web form — so "where did this come from?" had no
answer once the chat was closed. ``Source.MCP`` gives it one, and ranks with
``manual`` in the weight priority table: it is still the owner talking, just
through another surface, so Garmin must not start winning over it.
"""
from __future__ import annotations

import pytest

from vitals.enums import Source
from vitals.ownership import WriteIdentity
from vitals.services import conflict_engine, weight_service

mcp_router = pytest.importorskip("web.routers.mcp")


@pytest.fixture(autouse=True)
async def _legacy_mcp_owner(legacy_owner_roots):
    """MCP v1 is attributed only after the sole owner roots exist."""


@pytest.fixture(autouse=True)
def _use_test_factory(session_factory, monkeypatch):
    monkeypatch.setattr(mcp_router, "get_session_factory", lambda: session_factory)


@pytest.fixture(autouse=True)
async def _optional_modules_on(session_factory, legacy_owner_roots):
    from vitals.services import modules_service

    async with session_factory() as session:
        for key in sorted(modules_service.OPTIONAL_KEYS):
            await modules_service.set_module_enabled(
                session,
                key=key,
                enabled=True,
                subject_id=legacy_owner_roots.subject_id,
            )
        await session.commit()


async def test_write_tools_stamp_mcp_source():
    written = [
        await mcp_router.log_meal(name="Ужин", calories=700, on_date="2026-07-01"),
        await mcp_router.log_weight(weight_kg=80.0, on_date="2026-07-01"),
        await mcp_router.log_lab_result(marker="ferritin", value=45.0, on_date="2026-07-01"),
        await mcp_router.upsert_genetic_variant(gene="MTHFR", rsid="rs1801133", genotype="TT"),
    ]
    assert [row.get("source") for row in written] == [Source.MCP.value] * len(written)


async def test_mcp_and_manual_weight_rank_equally():
    assert weight_service._source_priority(Source.MCP.value) == weight_service._source_priority(
        Source.MANUAL.value
    )


async def test_mcp_weight_outranks_garmin_both_ways(session_factory, owner_write):
    """Garmin never supersedes a weight he gave himself — the connector included."""
    from datetime import date

    from sqlalchemy import select

    from vitals.enums import (
        Domain,
        IntegrationConnectionStatus,
        IntegrationConnectionType,
        IntegrationProvider,
    )
    from vitals.models.tenancy import IntegrationConnection
    from vitals.services import raw_payload_service

    on_date = date(2026, 7, 1)
    async with session_factory() as session:
        # A Garmin fact is only valid alongside the account connection it
        # arrived through and the payload it arrived in.
        connection = IntegrationConnection(
            subject_id=owner_write.subject_id,
            provider=IntegrationProvider.GARMIN.value,
            connection_type=IntegrationConnectionType.ACCOUNT.value,
            external_account_discriminator="synthetic-garmin-mcp",
            status=IntegrationConnectionStatus.ACTIVE.value,
        )
        session.add(connection)
        await session.flush()
        raw = await raw_payload_service.upsert_owned_raw_payload(
            session,
            identity=WriteIdentity(owner_write.subject_id, None),
            integration_connection_id=connection.id,
            domain=Domain.GARMIN.value,
            source=Source.GARMIN_API.value,
            external_id=f"garmin:weight:{on_date.isoformat()}",
            payload={"date": on_date.isoformat(), "weight_kg": 81.0},
        )
        # The capability belongs to the session it locks in, so this one is
        # minted here rather than borrowed from the fixture's session.
        context = await conflict_engine.resolve_legacy_conflict_write_context(
            session,
            actor_username=None,
            evaluation_date=on_date,
        )
        await weight_service.log_weight(
            session,
            on_date=on_date,
            weight_kg=81.0,
            source=Source.GARMIN_API.value,
            raw_payload_id=raw.id,
            identity=context.identity,
            integration_connection_id=connection.id,
            prepared_weight_write=await weight_service.prepare_weight_write(
                session, context=context
            ),
        )
        await session.commit()

    await mcp_router.log_weight(weight_kg=80.0, on_date=on_date.isoformat())

    async with session_factory() as session:
        active = await weight_service.get_active_weight(
            session,
            on_date,
            subject_id=owner_write.subject_id,
        )
        assert (active.weight_kg, active.source) == (80.0, Source.MCP.value)

        # Garmin arriving afterwards is kept, but does not take over.
        later_connection = await session.scalar(
            select(IntegrationConnection).where(
                IntegrationConnection.subject_id == owner_write.subject_id,
                IntegrationConnection.provider == IntegrationProvider.GARMIN.value,
            )
        )
        later_raw = await raw_payload_service.upsert_owned_raw_payload(
            session,
            identity=WriteIdentity(owner_write.subject_id, None),
            integration_connection_id=later_connection.id,
            domain=Domain.GARMIN.value,
            source=Source.GARMIN_API.value,
            external_id=f"garmin:weight:late:{on_date.isoformat()}",
            payload={"date": on_date.isoformat(), "weight_kg": 82.0},
        )
        later_context = await conflict_engine.resolve_legacy_conflict_write_context(
            session,
            actor_username=None,
            evaluation_date=on_date,
        )
        await weight_service.log_weight(
            session,
            on_date=on_date,
            weight_kg=82.0,
            source=Source.GARMIN_API.value,
            raw_payload_id=later_raw.id,
            identity=later_context.identity,
            integration_connection_id=later_connection.id,
            prepared_weight_write=await weight_service.prepare_weight_write(
                session, context=later_context
            ),
        )
        await session.commit()
        active = await weight_service.get_active_weight(
            session,
            on_date,
            subject_id=owner_write.subject_id,
        )
        assert (active.weight_kg, active.source) == (80.0, Source.MCP.value)
