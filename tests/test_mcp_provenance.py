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
from vitals.services import weight_service

mcp_router = pytest.importorskip("web.routers.mcp")


@pytest.fixture(autouse=True)
async def _legacy_mcp_owner(legacy_owner_roots):
    """MCP v1 is attributed only after the sole owner roots exist."""


@pytest.fixture(autouse=True)
def _use_test_factory(session_factory, monkeypatch):
    monkeypatch.setattr(mcp_router, "get_session_factory", lambda: session_factory)


@pytest.fixture(autouse=True)
async def _optional_modules_on(session_factory):
    from vitals.services import modules_service

    async with session_factory() as session:
        for key in sorted(modules_service.OPTIONAL_KEYS):
            await modules_service.set_module_enabled(session, key=key, enabled=True)
        await session.commit()


async def test_write_tools_stamp_mcp_source():
    written = [
        await mcp_router.log_meal(name="Ужин", calories=700, on_date="2026-07-01"),
        await mcp_router.log_weight(weight_kg=80.0, on_date="2026-07-01"),
        await mcp_router.log_lab_result(marker="ferritin", value=45.0, on_date="2026-07-01"),
        await mcp_router.log_signal(key="headache", kind="symptom", value_num=3, on_date="2026-07-01"),
        await mcp_router.upsert_genetic_variant(gene="MTHFR", rsid="rs1801133", genotype="TT"),
    ]
    assert [row.get("source") for row in written] == [Source.MCP.value] * len(written)


async def test_mcp_and_manual_weight_rank_equally():
    assert weight_service._source_priority(Source.MCP.value) == weight_service._source_priority(
        Source.MANUAL.value
    )


async def test_mcp_weight_outranks_garmin_both_ways(session_factory):
    """Garmin never supersedes a weight he gave himself — the connector included."""
    from datetime import date

    on_date = date(2026, 7, 1)
    async with session_factory() as session:
        await weight_service.log_weight(
            session, on_date=on_date, weight_kg=81.0, source=Source.GARMIN_API.value
        )
        await session.commit()

    await mcp_router.log_weight(weight_kg=80.0, on_date=on_date.isoformat())

    async with session_factory() as session:
        active = await weight_service.get_active_weight(session, on_date)
        assert (active.weight_kg, active.source) == (80.0, Source.MCP.value)

        # Garmin arriving afterwards is kept, but does not take over.
        await weight_service.log_weight(
            session, on_date=on_date, weight_kg=82.0, source=Source.GARMIN_API.value
        )
        await session.commit()
        active = await weight_service.get_active_weight(session, on_date)
        assert (active.weight_kg, active.source) == (80.0, Source.MCP.value)
