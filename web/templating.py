"""Jinja2 environment configuration and custom filters for the web interface."""
from __future__ import annotations

import os
from typing import Any

from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from vitals.i18n import t, decimal, get_js_strings, plural
from vitals.services.modules_service import (
    MODULE_REGISTRY,
    NAV_RUBRICS,
    bottom_slots,
    more_rubrics,
    more_routes,
    nav_modules,
)
from vitals.services.supplements_service import timing_bucket

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

# Create templates object
templates = Jinja2Templates(directory=TEMPLATES_DIR)


def format_number(value: Any) -> Any:
    """Format numeric values with space groups and the locale's decimal mark:
    12345.67 -> "12 345.7" in English, "12 345,7" in Russian.

    The separator follows the platform rather than arguing with it. A number
    input is drawn by the browser in the *user's* locale — Chrome renders the
    value 86.1 as "86,1" under ru and no attribute on the element changes that
    — so on /weight the same weight appeared as "86,1" in the field and "86.1"
    in the table below it. One of the two had to move, and only this one can."""
    try:
        if isinstance(value, (int, float)):
            n = value
        else:
            n = float(value)
        # int has no .is_integer() before Python 3.12, and ``bool`` is an int
        # subclass we don't want to reformat — guard both before the float check.
        if isinstance(n, bool):
            return value
        if isinstance(n, int) or n.is_integer():
            return f"{int(n):,}".replace(",", " ")
        n = round(n, 1)
        if n.is_integer():
            return f"{int(n):,}".replace(",", " ")
        return decimal(f"{n:,.1f}".replace(",", " "))
    except (TypeError, ValueError):
        return value


def plural_ru(n: Any, one: str, few: str, many: str) -> str:
    """Pick the Russian plural form for ``n``: 1 → one, 2–4 → few, 0/5+ → many.
    e.g. ``{{ count | plural_ru('сессия', 'сессии', 'сессий') }}``."""
    try:
        n = abs(int(n))
    except (TypeError, ValueError):
        return many
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not (12 <= n % 100 <= 14):
        return few
    return many


def static_version(path: str) -> str:
    """Append a cache-busting timestamp param based on the file modification time."""
    try:
        if path.startswith("/static/"):
            rel_path = path[8:]
            file_path = os.path.join(STATIC_DIR, rel_path)
            if os.path.exists(file_path):
                return f"{path}?v={int(os.path.getmtime(file_path))}"
    except Exception:
        pass
    return path


def format_date(value: Any) -> str:
    """Format date values (string or datetime object) to DD-MM-YYYY format."""
    if not value:
        return ""
    import datetime
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.strftime("%d-%m-%Y")
    if isinstance(value, str):
        value_str = value.strip()
        import re
        if re.match(r"^\d{2}-\d{2}-\d{4}$", value_str):
            return value_str
        if re.match(r"^\d{2}\.\d{2}\.\d{4}$", value_str):
            return value_str.replace(".", "-")
        if re.match(r"^\d{4}-\d{2}-\d{2}$", value_str):
            parts = value_str.split("-")
            return f"{parts[2]}-{parts[1]}-{parts[0]}"
        if re.match(r"^\d{4}-\d{2}-\d{2}\s+.*$", value_str):
            date_part = value_str.split()[0]
            parts = date_part.split("-")
            return f"{parts[2]}-{parts[1]}-{parts[0]}"
    return str(value)


def format_hm(seconds: Any) -> str:
    """Format a duration in seconds as "7 ч 18 мин"; falsy input renders as an
    em dash. The abbreviations come from the catalogue: "8h 40m" sitting next to
    "Оценка сна" was the app speaking two languages on one line."""
    if not seconds:
        return "—"
    seconds = int(seconds)
    return f"{seconds // 3600} {t('common.hour_abbr')} {(seconds % 3600) // 60} {t('common.min_abbr')}"


def meal_word(n: Any) -> str:
    """Russian plural for meal count: 1 приём, 2 приёма, 5 приёмов."""
    return plural_ru(n, "приём", "приёма", "приёмов")


def days_word(n: Any) -> str:
    """The word for "day" agreeing with ``n``, in the catalogue's language.
    Words come from the catalogue rather than this file so the doctor document
    doesn't read "за 42 дней" — a counted noun in Russian has three forms and a
    hardcoded one is wrong for two thirds of the numbers."""
    return plural(n, t("common.day_one"), t("common.day_few"), t("common.day_many"))


def format_domain(value: Any) -> str:
    """A domain key as a section name a reader outside the app would use.

    Its own namespace rather than ``enum.domain.*``: that one is the charts
    vocabulary ("Signals", "All charts"), and this list is read by a doctor who
    has never seen the app.
    """
    return t("share.section." + str(value))


def format_unit(value: Any) -> Markup:
    """Escape a lab unit string, then render '10^9' as a safe superscript.
    Escaping happens first so any HTML that ended up in the raw value
    (e.g. via a mis-parsed lab-photo import) can't reach the page unescaped."""
    escaped = str(escape(value or ""))
    return Markup(escaped.replace("10^9", "10<sup>9</sup>"))


# Register filters and globals
templates.env.filters["format_number"] = format_number
templates.env.filters["format_date"] = format_date
templates.env.filters["format_hm"] = format_hm
templates.env.filters["plural_ru"] = plural_ru
templates.env.filters["plural"] = lambda n, *args: plural(n, *args)
templates.env.filters["meal_word"] = meal_word
templates.env.filters["days_word"] = days_word
templates.env.filters["format_unit"] = format_unit
templates.env.filters["format_domain"] = format_domain
templates.env.filters["timing_bucket"] = timing_bucket
templates.env.globals["static_version"] = static_version
templates.env.globals["t"] = t
templates.env.globals["get_js_strings"] = get_js_strings
templates.env.globals["plural"] = plural
# Navigation registry — the rail, the masthead tabs and the mobile nav all read
# these instead of keeping their own copy of the section list.
templates.env.globals["module_registry"] = MODULE_REGISTRY
templates.env.globals["module_rubrics"] = NAV_RUBRICS
templates.env.globals["nav_modules"] = nav_modules
# Phone bottom bar (five fixed columns) and the "More" screen read the same
# registry through these two derived views — see modules_service.
templates.env.globals["bottom_slots"] = bottom_slots
templates.env.globals["more_rubrics"] = more_rubrics
templates.env.globals["more_routes"] = more_routes
