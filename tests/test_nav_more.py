"""The August 2026 nav handoff: the /more screen and the rail's status card.

Both surfaces read the same two services (modules_service for what to list,
nav_status_service for today's numbers), so these cover the pieces the static
template contracts in test_ui_static_contracts.py can't see.
"""
from __future__ import annotations

from datetime import timedelta

from vitals.enums import Source
from vitals.models.garmin import DOMAIN as GARMIN_DOMAIN, GarminDaily
from vitals.models.weight import DOMAIN as WEIGHT_DOMAIN, WeightLog
from vitals.services import nav_status_service
from vitals.utils.timeutils import today_local


# ── /more ────────────────────────────────────────────────────────────────────

async def test_more_screen_lists_every_visible_section(auth_client):
    """Not only the rubrics without a bottom-bar column. The bar's three slots
    reach their siblings through the masthead chips, which is a fine way to
    switch and a terrible way to find — half the app looked missing on a phone.
    """
    from vitals.services.modules_service import nav_modules

    response = await auth_client.get("/more", headers={"Accept": "text/html"})
    assert response.status_code == 200
    enabled = {"labs": True, "weight": True, "garmin": True, "reports": True, "charts": True}
    for spec in nav_modules(enabled):
        assert f'href="{spec.route}"' in response.text, spec.key
    assert 'href="/messages"' in response.text
    assert 'href="/settings"' in response.text
    # It is a page, not an overlay — nothing to close, and Back leaves it.
    assert "mobileMenuOpen" not in response.text


async def test_more_screen_has_one_account_row_not_a_second_settings_link(auth_client):
    """The "Modules" row pointed at the same page as "Settings"; its count moved
    onto the Settings row instead of standing as a second destination."""
    response = await auth_client.get("/more", headers={"Accept": "text/html"})
    # Scoped to the screen itself — the rail (hidden on a phone, still rendered)
    # carries its own Settings link.
    screen = response.text.split('class="v-page v-page-more')[1].split("</form>")[0]
    assert screen.count('href="/settings"') == 1


async def test_more_screen_reports_how_many_modules_are_on(auth_client):
    from vitals.services.modules_service import MODULE_REGISTRY

    response = await auth_client.get("/more", headers={"Accept": "text/html"})
    assert f"из {len(MODULE_REGISTRY)}" in response.text


# ── The rail's status card ───────────────────────────────────────────────────

async def test_status_card_reports_todays_weight_and_the_weeks_direction(db_session, owner_write, owned_by_legacy_subject):
    """The card exists to say where he is, not how the plumbing is doing — the
    first version reported "labs · 99 days ago" every single day."""
    for days_ago, kg in ((9, 87.0), (0, 86.1)):
        db_session.add(
            WeightLog(subject_id=owner_write.subject_id,
                date=today_local() - timedelta(days=days_ago),
                domain=WEIGHT_DOMAIN,
                source=Source.MANUAL.value,
                weight_kg=kg,
            )
        )
    await db_session.flush()

    rows = {r.key: r for r in await nav_status_service.rail_stats(
        db_session, subject_id=owner_write.subject_id
    )}
    assert rows["weight"].value.startswith("86.1")
    assert rows["weight"].sub == "−0.9"
    assert rows["weight"].tone == "good"


async def test_a_source_that_went_quiet_says_so_instead_of_a_number(db_session, owner_write, owned_by_legacy_subject, *, garmin_connection_id):
    db_session.add(
        GarminDaily(subject_id=owner_write.subject_id, integration_connection_id=garmin_connection_id,
            date=today_local() - timedelta(days=6),
            domain=GARMIN_DOMAIN,
            source=Source.GARMIN_API.value,
            sleep_seconds=7 * 3600,
        )
    )
    await db_session.flush()

    rows = {r.key: r for r in await nav_status_service.rail_stats(
        db_session, subject_id=owner_write.subject_id
    )}
    assert rows["recovery"].tone == "warn"
    assert "6" in rows["recovery"].value


async def test_last_nights_sleep_reads_as_hours_and_minutes(db_session, owner_write, owned_by_legacy_subject, *, garmin_connection_id):
    db_session.add(
        GarminDaily(subject_id=owner_write.subject_id, integration_connection_id=garmin_connection_id,
            date=today_local(),
            domain=GARMIN_DOMAIN,
            source=Source.GARMIN_API.value,
            sleep_seconds=7 * 3600 + 20 * 60,
            training_readiness=62,
        )
    )
    await db_session.flush()

    rows = {r.key: r for r in await nav_status_service.rail_stats(
        db_session, subject_id=owner_write.subject_id
    )}
    assert rows["recovery"].value == "7:20"
    assert "62" in rows["recovery"].sub


async def test_a_domain_with_nothing_logged_yet_gets_no_row(db_session, owner_write, owned_by_legacy_subject):
    assert await nav_status_service.rail_stats(
        db_session, subject_id=owner_write.subject_id
    ) == []


async def test_a_disabled_module_gets_no_row(db_session, owner_write, owned_by_legacy_subject, *, garmin_connection_id):
    db_session.add(
        GarminDaily(integration_connection_id=garmin_connection_id,
            subject_id=owner_write.subject_id,
            date=today_local(),
            domain=GARMIN_DOMAIN,
            source=Source.GARMIN_API.value,
            sleep_seconds=7 * 3600,
        )
    )
    await db_session.flush()

    enabled = {"garmin": False, "weight": True}
    assert await nav_status_service.rail_stats(
        db_session, enabled, subject_id=owner_write.subject_id
    ) == []


async def test_status_rows_never_raise_on_a_broken_session():
    """The rail is chrome: an unreadable domain must lose its row, not 500 the
    page it is drawn on."""
    import uuid as _uuid

    class Boom:
        async def execute(self, *a, **kw):
            raise RuntimeError("db is down")

    assert await nav_status_service.rail_stats(
        Boom(), subject_id=_uuid.uuid4()
    ) == []


# ── The card has to survive a boosted navigation ─────────────────────────────

async def test_status_card_survives_a_boosted_navigation(
    auth_client, db_session, legacy_owner_roots
, *, garmin_connection_id):
    """The regression: hx-boost sends no Accept header at all, so a guard that
    required "text/html" skipped the reads on exactly the requests that
    re-render the whole rail — the card vanished on every click and came back on
    every reload."""
    db_session.add(
        GarminDaily(integration_connection_id=garmin_connection_id,
            subject_id=legacy_owner_roots.subject_id,
            date=today_local(),
            domain=GARMIN_DOMAIN,
            source=Source.GARMIN_API.value,
            sleep_seconds=7 * 3600,
        )
    )
    await db_session.commit()

    full = await auth_client.get("/weight", headers={"Accept": "text/html"})
    # What hx-boost actually puts on the wire: HX-Request, and XHR's default
    # Accept — which htmx never touches, so it is either absent or "*/*".
    absent = await auth_client.get("/weight", headers={"HX-Request": "true", "Accept": ""})
    wildcard = await auth_client.get("/weight", headers={"HX-Request": "true", "Accept": "*/*"})
    for name, response in (("full", full), ("absent", absent), ("wildcard", wildcard)):
        assert 'class="mh-rail-stats"' in response.text, name


async def test_a_json_client_still_pays_nothing_for_the_card(auth_client):
    """The guard exists to keep MCP and API reads off these four queries."""
    from web.deps import load_nav_status

    class Req:
        method = "GET"
        headers = {"accept": "application/json"}
        state = type("S", (), {})()

    req = Req()
    await load_nav_status(req, db=None)
    assert req.state.nav_status == []
