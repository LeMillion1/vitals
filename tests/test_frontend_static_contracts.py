"""Static contracts for the shipped frontend assets.

Same shape as ``test_design_handoff_vitals.py``: these assert facts about the
shipped static assets and templates, which is where this batch of bugs lived —
a service worker rule, an htmx error handler, four form attributes and two
misplaced ``<script src>`` tags. None of them is reachable from a request test.
"""

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "web/templates"
SW_JS = (ROOT / "web/static/sw.js").read_text(encoding="utf-8")
BASE_HTML = (TEMPLATES / "base.html").read_text(encoding="utf-8")
HEVY_JS = (ROOT / "web/static/hevy.js").read_text(encoding="utf-8")
APP_JS = (ROOT / "web/static/app.js").read_text(encoding="utf-8")

# Forms that POST a whole page navigation and must not strand a dead button
# when the server answers with an error.
DISABLED_ELT_FORMS = (
    "labs/index.html",
    "glp1/index.html",
    "charts/index.html",
)


def test_service_worker_never_caches_uploads():
    """/static/uploads/* is lab sheets, InBody printouts and body photos.

    Cache Storage outlives the session and logout doesn't clear it, so caching
    those would leave medical images on disk for anyone with the device.
    """
    assert "/static/uploads/" in SW_JS
    caching_rule = SW_JS[SW_JS.index("url.pathname.startsWith('/static/')"):]
    caching_rule = caching_rule[: caching_rule.index("{")]
    assert "!url.pathname.startsWith('/static/uploads/')" in caching_rule


def test_service_worker_is_registered_at_the_origin_root():
    """Offline navigation and notification clicks must control app routes."""

    registration = BASE_HTML[BASE_HTML.index("navigator.serviceWorker.getRegistrations") :]
    registration = registration[: registration.index("</script>")]
    assert "registration.unregister()" in registration
    assert "'/static/sw.js'" in registration
    assert "navigator.serviceWorker.register('/sw.js'" in registration
    assert "scope: '/'" in registration
    assert "updateViaCache: 'none'" in registration


def test_service_worker_push_never_accepts_notification_content_or_url():
    """The encrypted provider payload is a wakeup, never notification content."""

    push_handler = SW_JS[SW_JS.index("self.addEventListener('push'") :]
    push_handler = push_handler[: push_handler.index("self.addEventListener('notificationclick'")]
    assert "isCareWakeup(payload)" in push_handler
    assert "showCareNotification()" in push_handler
    assert "payload." not in push_handler
    assert "event.data.text" not in push_handler

    click_handler = SW_JS[SW_JS.index("self.addEventListener('notificationclick'") :]
    click_handler = click_handler[: click_handler.index("self.addEventListener('fetch'")]
    assert "isCareWakeup(event.notification.data)" in click_handler
    assert "openCareInbox()" in click_handler
    assert "event.notification.data.url" not in click_handler
    assert "New message" not in SW_JS
    assert "Новое сообщение" not in SW_JS


def test_response_error_handler_reads_every_error_shape():
    """Routers answer with ``error``/``message``, not only FastAPI's ``detail``."""
    handler = BASE_HTML[BASE_HTML.index("addEventListener('htmx:responseError'"):]
    handler = handler[: handler.index("});")]
    for field in ("data.detail", "data.error", "data.message"):
        assert field in handler, f"htmx error toast ignores {field}"


def test_no_template_disables_a_submit_button_by_hand():
    """Inline ``onsubmit`` disabling never re-enabled the button on an error."""
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in TEMPLATES.rglob("*.html")
        if "onsubmit=" in path.read_text(encoding="utf-8")
    ]
    assert not offenders, f"inline onsubmit still present in: {offenders}"


def test_navigating_forms_use_hx_disabled_elt():
    """htmx disables *and re-enables* the button on every response code."""
    for name in DISABLED_ELT_FORMS:
        text = (TEMPLATES / name).read_text(encoding="utf-8")
        assert "hx-disabled-elt=" in text, f"{name} has no double-submit guard"
    glp1 = (TEMPLATES / "glp1/index.html").read_text(encoding="utf-8")
    assert glp1.count("hx-disabled-elt=") == 2, "both glp1 forms need the guard"


def test_page_scripts_are_loaded_from_head_only():
    """B6 — a ``<script src>`` inside <body> is re-fetched on every boosted
    navigation, racing Alpine; that is why the labs/hevy charts sometimes only
    appeared after a manual reload."""
    src_re = re.compile(r"""<script[^>]*\ssrc=["'][^"']*/static/[^"']+["']""")
    offenders = []
    for path in TEMPLATES.rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        body_at = text.find("<body")
        if body_at == -1:  # page templates extend base.html — all of them are body
            body_at = 0
        if src_re.search(text[body_at:]):
            offenders.append(path.relative_to(ROOT).as_posix())
    assert not offenders, f"page scripts still loaded from <body>: {offenders}"

    for name in ("labs.js", "hevy.js"):
        assert f"/static/{name}" in BASE_HTML, f"{name} is not loaded from <head>"


def test_charts_js_loads_before_its_consumers():
    """B6 — deferred scripts run with readyState already "interactive", so the
    chart inits in app.js/hevy.js fire during evaluation. vitalsFormatDateStr
    must therefore already exist by then."""
    order = [
        BASE_HTML.index(f"/static/{name}") for name in ("charts.js", "app.js", "hevy.js")
    ]
    assert order == sorted(order), "charts.js must be loaded before app.js and hevy.js"


def test_date_formatter_has_one_definition():
    """B6 — hevy.js and app.js each carried a private copy of charts.js's helper."""
    assert "function formatDateStr" not in HEVY_JS
    assert "function formatDateStr" not in APP_JS
    assert "vitalsFormatDateStr" in HEVY_JS
    assert "vitalsFormatDateStr" in APP_JS


# ── What the browser is allowed to keep ──────────────────────────────────────
# htmx stores a snapshot of every boosted page in localStorage under
# ``htmx-history-cache``. On a shared machine that is somebody's medical record
# sitting in the browser after the session allowed to see it has ended.


def _template(name: str) -> str:
    from pathlib import Path

    return (
        Path(__file__).resolve().parent.parent / "web" / "templates" / name
    ).read_text()


def test_pages_showing_somebody_elses_record_are_not_cached():
    """A page costs one request to re-fetch. A cached copy costs a leak."""

    for name in (
        "care/patient.html",
        "care/roster.html",
        "care/accept.html",
        "settings/care.html",
    ):
        assert 'hx-history="false"' in _template(name), name


def test_the_login_page_drops_what_the_last_session_left():
    """Anybody looking at it is, by definition, not in a session.

    Covers logging out, a session expiring, and somebody else sitting down at
    the machine — three routes to the same page and one thing to do about them.
    """

    login = _template("login.html")
    assert "htmx-history-cache" in login
    assert "vitals_diag" in login
    assert "localStorage.removeItem" in login


def test_the_diagnostics_buffer_does_not_record_which_patients_were_opened():
    """It exists to diagnose render stalls, and a subject id does not help it.

    Recording one would put a list of who somebody looked at into localStorage,
    where it would outlive the session that was allowed to look.
    """

    base = _template("base.html")
    assert "vitals_diag" in base
    # The path is redacted before it is stored, so a UUID never reaches the buffer.
    assert "location.pathname.replace(" in base
    assert "':id'" in base
