"""MCP resources + canonical prompt: vitals://profile,
vitals://digest/latest, and the weekly_review prompt."""
from __future__ import annotations

from datetime import date

import pytest


# These tests seed rows with no owner on purpose: they pin what a scoped
# reader does when the ownership backfill has not reached a row yet, which is
# a state the application itself can no longer create. The schema says so, so
# this module asks for the one that stood before the ownership contract.
pytestmark = pytest.mark.pre_ownership_contract

mcp_router = pytest.importorskip("web.routers.mcp")


@pytest.fixture(autouse=True)
def _use_test_factory(session_factory, monkeypatch):
    monkeypatch.setattr(mcp_router, "get_session_factory", lambda: session_factory)


async def test_profile_resource_returns_profile():
    prof = await mcp_router.profile_resource()
    assert "height_cm" in prof
    assert "goals" in prof


async def test_latest_digest_resource_empty_then_populated(
    db_session,
    legacy_owner_roots,
    openrouter_connection_id,
):
    assert await mcp_router.latest_digest_resource() == {"error": "No digests yet"}

    from vitals.models import WeeklyDigest
    from vitals.enums import Domain, Source

    db_session.add(
        WeeklyDigest(
            subject_id=legacy_owner_roots.subject_id,
            # A manual digest is something a person wrote, so the row has to
            # name which one — and it has to be the owner. A weekly narrative
            # also has to name the OpenRouter account it was generated through,
            # even when a human wrote this one by hand.
            actor_user_id=legacy_owner_roots.user_id,
            integration_connection_id=openrouter_connection_id,
            date=date(2026, 7, 5),
            domain=Domain.MILESTONES.value,
            source=Source.MANUAL.value,
            content="Weekly narrative.",
            model="test-model",
        )
    )
    await db_session.commit()

    latest = await mcp_router.latest_digest_resource()
    assert latest["date"] == "2026-07-05"
    assert latest["content"] == "Weekly narrative."


async def test_weekly_review_prompt():
    text = await mcp_router.weekly_review()
    assert isinstance(text, str)
    assert "get_full_snapshot" in text
