"""The August 2026 nav handoff: the /more screen and the rail's sync card.

Both surfaces read the same two services (modules_service for what to list,
nav_status_service for how fresh each source is), so these cover the pieces the
static template contracts in test_review_run3.py can't see.
"""
from __future__ import annotations

from datetime import timedelta

from vitals.enums import Source
from vitals.models.garmin import DOMAIN as GARMIN_DOMAIN, GarminDaily
from vitals.services import nav_status_service
from vitals.utils.timeutils import today_local


# ── /more ────────────────────────────────────────────────────────────────────

async def test_more_screen_lists_the_sections_the_bottom_bar_cannot_hold(auth_client):
    """Markers gets no bottom-bar column, so it has to be reachable here."""
    response = await auth_client.get("/more", headers={"Accept": "text/html"})
    assert response.status_code == 200
    assert 'href="/labs"' in response.text
    assert 'href="/settings"' in response.text
    # It is a page, not an overlay — nothing to close, and Back leaves it.
    assert "mobileMenuOpen" not in response.text


async def test_more_screen_reports_how_many_modules_are_on(auth_client):
    from vitals.services.modules_service import MODULE_REGISTRY

    response = await auth_client.get("/more", headers={"Accept": "text/html"})
    assert f"из {len(MODULE_REGISTRY)}" in response.text


# ── Source freshness ─────────────────────────────────────────────────────────

async def test_sync_rows_report_days_since_the_newest_row(db_session):
    on_date = today_local() - timedelta(days=3)
    db_session.add(
        GarminDaily(date=on_date, domain=GARMIN_DOMAIN, source=Source.GARMIN_API.value)
    )
    await db_session.flush()

    rows = {r.key: r for r in await nav_status_service.sync_rows(db_session)}
    assert rows["garmin"].days == 3
    # Garmin syncs nightly, so three days without one is worth flagging.
    assert rows["garmin"].stale is True


async def test_a_source_with_no_data_at_all_reads_as_stale(db_session):
    rows = {r.key: r for r in await nav_status_service.sync_rows(db_session)}
    assert rows["garmin"].days is None
    assert rows["garmin"].stale is True


async def test_a_disabled_module_gets_no_row(db_session):
    enabled = {"garmin": True, "hevy": False, "labs": True}
    keys = {r.key for r in await nav_status_service.sync_rows(db_session, enabled)}
    assert keys == {"garmin", "labs"}


async def test_sync_rows_never_raise_on_a_broken_session():
    """The rail is chrome: an unreadable source must hide a row, not 500 the
    page it is drawn on."""

    class Boom:
        async def execute(self, *a, **kw):
            raise RuntimeError("db is down")

    assert await nav_status_service.sync_rows(Boom()) == []


async def test_more_cell_stays_lit_inside_the_sections_it_holds(auth_client):
    """Labs has no column of its own, so without this the phone bar would go
    completely dark the moment you opened it."""
    for path in ("/more", "/labs", "/settings"):
        response = await auth_client.get(path, headers={"Accept": "text/html"})
        bar = response.text.split('id="mobile-bottom-nav"')[1].split("</nav>")[0]
        more_cell = bar.split('href="/more"')[1].split(">")[0]
        assert "is-active" in more_cell, path
