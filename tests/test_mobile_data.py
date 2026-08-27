"""Contracts for mobile pass 4 — "the screen shows data, or says why not".

Two kinds of check, same as ``tests/test_mobile_shell.py``: CSS/JS/templates read
as text where the invariant is about which rule exists where, and one real
service call where the invariant is about what the database hands back.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path

import pytest

from vitals.models.garmin import GarminDaily
from vitals.services.garmin import queries as garmin_queries


ROOT = Path(__file__).resolve().parents[1]
APP_CSS = (ROOT / "web/static/vitals.css").read_text(encoding="utf-8")
APP_JS = (ROOT / "web/static/app.js").read_text(encoding="utf-8")
CHARTS_JS = (ROOT / "web/static/charts.js").read_text(encoding="utf-8")
TODAY_HTML = (ROOT / "web/templates/today/index.html").read_text(encoding="utf-8")
TEMPLATES = ROOT / "web/templates"

# The one capped box that must keep its cap on a phone: it is a modal's viewport
# limit, not a list — without it the photo runs off the screen.
LIGHTBOX = "web/templates/weight/measures.html"


@pytest.mark.asyncio
async def test_a_placeholder_row_is_not_the_latest_day(db_session, legacy_owner_roots, *, garmin_connection_id):
    """The sync writes a row when the date turns, hours before the watch reports
    anything. Returned as "the latest day" that empty row drew a whole screen of
    dashes with yesterday's complete row sitting right behind it."""
    db_session.add_all([
        GarminDaily(integration_connection_id=garmin_connection_id,
            subject_id=legacy_owner_roots.subject_id,
            date=date(2026, 8, 1), domain="garmin", sleep_score=82, steps=9000,
        ),
        # today: the row exists, the watch has reported nothing onto it yet
        GarminDaily(integration_connection_id=garmin_connection_id,
            subject_id=legacy_owner_roots.subject_id,
            date=date(2026, 8, 2), domain="garmin",
        ),
    ])
    await db_session.flush()

    latest = await garmin_queries.latest_daily(
        db_session, subject_id=legacy_owner_roots.subject_id
    )
    assert latest is not None
    assert latest.date == date(2026, 8, 1)
    assert latest.sleep_score == 82


@pytest.mark.asyncio
async def test_the_day_strip_names_the_day_when_it_is_not_today(db_session, auth_client, *, garmin_connection_id, legacy_owner_roots):
    """Showing yesterday's numbers silently is worse than showing none."""
    from vitals.utils.timeutils import today_local

    day = today_local().replace(day=1) - timedelta(days=1)
    db_session.add(GarminDaily(subject_id=legacy_owner_roots.subject_id, integration_connection_id=garmin_connection_id, date=day, domain="garmin", sleep_score=71, steps=4200))
    await db_session.flush()

    r = await auth_client.get("/garmin")
    assert r.status_code == 200
    assert day.strftime("%d-%m-%Y") in r.text


def test_capped_lists_release_the_cap_on_a_phone():
    """A `max-h-… overflow-y-auto` list on a phone is a scroll trap: the finger
    lands in the list and the page stands still. Every one of them carries
    `.v-scroll-cap`, which the phone block unsets."""
    assert ".v-scroll-cap" in APP_CSS
    for path in sorted(TEMPLATES.rglob("*.html")):
        text = path.read_text(encoding="utf-8")
        for tag in re.findall(r"<[a-z]+ [^>]*max-h-\[[^\]]+\][^>]*>", text):
            if "overflow-y-auto" not in tag:
                continue
            rel = path.relative_to(ROOT).as_posix()
            assert "v-scroll-cap" in tag, f"{rel}: capped list without v-scroll-cap"
        if path.relative_to(ROOT).as_posix() == LIGHTBOX:
            assert "max-h-[80vh]" in text  # the lightbox keeps its own cap


def test_empty_today_cards_collapse_to_one_line():
    """Four cards that each cost ~100px of chrome to say one grey sentence."""
    for collection in ("changes", "feed", "attention", "goal"):
        assert "{%% if not %s %%} is-empty{%% endif %%}" % collection in TODAY_HTML
    assert ".v-card.is-empty {" in APP_CSS


def test_the_phone_gets_its_own_chart_configuration():
    """Eight date ticks, four series and a label on every dose phase are a
    desktop chart's decisions; on 332px they land on top of each other."""
    for js in (APP_JS, CHARTS_JS):
        assert "matchMedia('(max-width: 767px)')" in js
        assert "maxTicksLimit: phone ? 4 : 8" in js
    # Lean mass sits ~30kg below the scale weight: drawn, it stretches the Y axis
    # over a range half of which has no data in it.
    assert "hidden: phone" in APP_JS
    assert "hidden: phone || biaLbm.length === 0" in APP_JS
    # Only the phase he is on now is named.
    assert "display: !phone || idx === blocks.length - 1" in APP_JS


def test_empty_selects_offer_a_choice_rather_than_a_dash():
    """A "—" in a select reads as a broken widget, not as "nothing picked"."""
    charts_html = (TEMPLATES / "charts/index.html").read_text(encoding="utf-8")
    assert "|| '—'" not in charts_html
    for key in ("charts.pick_domain", "charts.pick_metric", "charts.pick_param"):
        assert key in charts_html
