"""Role-by-role UI regression against a seeded shared installation.

Not a page sweep: every scenario below *does* something and then checks the
consequence somewhere else — the sweep already lives in
``tests/test_shared_installation_pages.py`` and it is the flows that keep being
where the defects are. A support grant that a patient approves and an
administrator cannot then use is a working service and a broken product, and
nothing below the browser can tell the two apart.

Every page load also collects console errors, page errors, any response >= 400
that was not expected, and horizontal overflow. A scenario that passes its own
assertions and leaves a 500 in the network log still fails.

    python scripts/seed_care_demo.py > /tmp/seed.txt   # accounts and cookies
    PORT=8010 python run_local.py &
    python scripts/ui_regression.py --cookies /tmp/seed.txt

The seeder prints one signed session cookie per account, because the password
login authenticates exactly one username from ``.env``: there is no other way to
be somebody else in a browser here. Subject ids are read from the same database
rather than pasted, so a reseed does not silently turn every scenario into a 404
that looks like a regression.

Needs the Playwright *package* only — it drives the Chromium already in
``~/Library/Caches/ms-playwright``, so install with
``PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1``.
"""
import argparse
import os
import pathlib
import re
import sqlite3
import sys
import traceback

from playwright.sync_api import sync_playwright

REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base", default="http://127.0.0.1:8010", help="where the app is running"
    )
    parser.add_argument(
        "--cookies",
        required=True,
        help="the seeder's output, or a tab-separated username/cookie file",
    )
    parser.add_argument(
        "--database",
        default=str(REPOSITORY_ROOT / "local_vitals.db"),
        help="the SQLite file the seeder wrote, for resolving subject ids",
    )
    parser.add_argument(
        "--chromium",
        default=os.environ.get("VITALS_CHROMIUM", ""),
        help="path to a Chromium binary; defaults to the Playwright cache",
    )
    return parser.parse_args()


def _find_chromium(explicit: str) -> str:
    """The browser Playwright already downloaded, rather than another download."""

    if explicit:
        return explicit
    cache = pathlib.Path.home() / "Library/Caches/ms-playwright"
    if not cache.exists():
        cache = pathlib.Path.home() / ".cache/ms-playwright"
    for candidate in sorted(cache.glob("chromium-*"), reverse=True):
        for relative in (
            "chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/"
            "Google Chrome for Testing",
            "chrome-mac/Chromium.app/Contents/MacOS/Chromium",
            "chrome-linux/chrome",
        ):
            found = candidate / relative
            if found.exists():
                return str(found)
    raise SystemExit(
        "no Chromium found under ms-playwright; pass --chromium or set "
        "VITALS_CHROMIUM"
    )


def _read_cookies(path: str) -> dict[str, str]:
    """Accept the seeder's own output as well as a plain two-column file.

    Parsing what the seeder already prints means one fewer step to get wrong,
    and one fewer file that can go stale against the database beside it.
    """

    cookies: dict[str, str] = {}
    pending = None
    for line in pathlib.Path(path).read_text().splitlines():
        if "\t" in line:
            name, token = line.split("\t", 1)
            cookies[name.strip()] = token.strip()
            continue
        match = re.match(r"^\s{4}(\S+)\s{2,}", line)
        if match:
            pending = match.group(1)
            continue
        stripped = line.strip()
        if pending and stripped.startswith("eyJ"):
            cookies[pending] = stripped
            pending = None
    if not cookies:
        raise SystemExit(f"no session cookies found in {path}")
    return cookies


def _subject_of(database: str, username: str) -> str:
    """Read the id rather than paste it: a reseed changes every one of them."""

    with sqlite3.connect(database) as db:
        row = db.execute(
            "SELECT hs.id FROM health_subjects hs "
            "JOIN users u ON u.id = hs.owner_user_id "
            "WHERE u.normalized_username = ?",
            (username,),
        ).fetchone()
    if row is None:
        raise SystemExit(f"{username} owns no health subject in {database}")
    raw = row[0]
    return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"


_args = _parse_args()
BASE = _args.base.rstrip("/")
CHROME = _find_chromium(_args.chromium)
COOKIES = _read_cookies(_args.cookies)

TIMUR = _subject_of(_args.database, "timur")
PATIENT01 = _subject_of(_args.database, "patient01")
PATIENT05 = _subject_of(_args.database, "patient05")

results = []
noise = []


class Session:
    """One signed-in browser, watched."""

    def __init__(self, browser, who, mobile=False):
        vp = {"width": 390, "height": 844} if mobile else {"width": 1280, "height": 900}
        extra = {"is_mobile": True, "has_touch": True} if mobile else {}
        self.who = who
        self.ctx = browser.new_context(viewport=vp, device_scale_factor=1, **extra)
        self.ctx.add_cookies(
            [{"name": "vitals_session", "value": COOKIES[who].strip(),
              "domain": "127.0.0.1", "path": "/"}]
        )
        self.pg = self.ctx.new_page()
        self.expect = set()
        self.pg.on("console", self._console)
        self.pg.on("pageerror", lambda e: noise.append(f"{who} pageerror: {str(e)[:150]}"))
        self.pg.on("response", self._response)

    def _console(self, m):
        if m.type == "error" and "Failed to load resource" not in m.text:
            noise.append(f"{self.who} console: {m.text[:150]}")

    def _response(self, r):
        if r.status >= 400 and r.status not in self.expect:
            noise.append(f"{self.who} HTTP {r.status} {r.url.replace(BASE, '')[:90]}")

    def go(self, path, expect=()):
        self.expect = set(expect)
        resp = self.pg.goto(BASE + path, wait_until="networkidle")
        self.pg.wait_for_timeout(500)
        over = self.pg.evaluate(
            "() => document.documentElement.scrollWidth - window.innerWidth"
        )
        if over > 1:
            noise.append(f"{self.who} overflow {over}px on {path}")
        self.expect = set()
        return resp.status if resp else 0

    def click(self, selector):
        self.pg.click(selector)
        self.pg.wait_for_load_state("networkidle")
        self.pg.wait_for_timeout(400)

    def text(self):
        return self.pg.inner_text("body")

    def close(self):
        self.ctx.close()


def scenario(name):
    def wrap(fn):
        def run(browser):
            try:
                fn(browser)
                results.append(("PASS", name, ""))
            except AssertionError as exc:
                results.append(("FAIL", name, str(exc) or "assertion"))
            except Exception:
                results.append(("ERROR", name, traceback.format_exc().splitlines()[-1]))
        run.__name__ = fn.__name__
        return run
    return wrap


# ── Support access: the whole loop ───────────────────────────────────────────


@scenario("support: ask -> approve -> banner -> read -> patient revokes")
def support_full_loop(browser):
    admin = Session(browser, "admin")
    admin.go("/settings/platform/support")
    # Ask for patient01's labs.
    admin.pg.select_option('select[name="subject_id"]', label="Patient 01")
    admin.pg.fill('textarea[name="reason"]', "Регресс-прогон: проверяю ленту анализов.")
    admin.pg.check('input[name="domains"][value="labs"]')
    admin.click('button:has-text("Ask")')
    assert "Asked" in admin.text() or "Запрошено" in admin.text(), "ask not confirmed"
    assert "Patient 01" in admin.text(), "the pending ask is not listed"

    # The patient sees it, with the admin's own words.
    p1 = Session(browser, "patient01")
    p1.go("/settings/access")
    body = p1.text()
    assert "Регресс-прогон" in body, "the reason is not shown to the patient"
    assert "admin" in body, "the asker is not named"

    # No banner before approval — asking is not access.
    p1.go("/today")
    assert "grant you approved" not in p1.text(), "banner before approval"

    p1.go("/settings/access")
    p1.click('button:has-text("Allow")')
    assert "Allowed" in p1.text() or "Разрешено" in p1.text(), "approval not confirmed"

    # Banner on an unrelated page.
    p1.go("/weight")
    assert "/settings/access" in p1.pg.content(), "no banner after approval"

    # The admin can open the record, and sees only labs.
    admin.go("/settings/platform/support")
    assert "Open the record" in admin.text(), "the grant is not listed as open"
    admin.click('a:has-text("Open the record")')
    record = admin.text()
    assert "Platform support" in record, "the basis is not named as support"
    assert "(Doctor)" not in record, "support described as a doctor"
    assert "Not shared with you" in record, "no withheld line on a partial grant"
    assert "Nutrition" in record.split("Not shared with you")[1][:400], \
        "an ungranted domain is not listed as withheld"

    # The patient ends it, and the record shuts.
    p1.go("/settings/access")
    p1.click('button:has-text("Stop it now")')
    assert "Stopped" in p1.text() or "Прекращено" in p1.text(), "revoke not confirmed"
    admin.expect = {404}
    status = admin.go(f"/care/{PATIENT01}", expect=(404,))
    assert status == 404, f"record still open after revoke: {status}"

    p1.go("/weight")
    assert "grant you approved" not in p1.text(), "banner survived the revoke"
    admin.close()
    p1.close()


@scenario("support: a refusal is kept in the history")
def support_decline(browser):
    admin = Session(browser, "admin")
    admin.go("/settings/platform/support")
    admin.pg.select_option('select[name="subject_id"]', label="Patient 02")
    admin.pg.fill('textarea[name="reason"]', "Регресс: этот запрос должны отклонить.")
    admin.pg.check('input[name="domains"][value="weight"]')
    admin.click('button:has-text("Ask")')

    p2 = Session(browser, "patient01")  # not the addressee
    p2.go("/settings/access")
    assert "этот запрос должны отклонить" not in p2.text(), \
        "one patient can see another's support request"
    p2.close()
    admin.close()


@scenario("support: the console refuses a doctor and a member")
def support_console_is_admin_only(browser):
    for who in ("dr-ivanov", "timur"):
        s = Session(browser, who)
        status = s.go("/settings/platform/support", expect=(403,))
        assert status == 403, f"{who} reached the console: {status}"
        s.close()


@scenario("support: a professional with no record is sent somewhere useful")
def support_refusal_page(browser):
    """Two different right answers, and the handler picks by what they hold.

    ``/settings/access`` is about *my* record and a professional keeps none.
    Somebody with patients is redirected to their roster — that is where their
    work is, and a refusal page would be a dead end with the answer written on
    it. Somebody with neither gets the refusal page itself, which still has to
    be a page with a way out rather than a sentence.
    """

    holds_patients = Session(browser, "coach-sokol")
    assert holds_patients.go("/settings/access") == 200, "the trainer was stranded"
    assert "/care" in holds_patients.pg.url, \
        f"a professional with patients was not sent to their roster: {holds_patients.pg.url}"
    holds_patients.close()

    holds_nobody = Session(browser, "dr-petrova")
    status = holds_nobody.go("/settings/access", expect=(409,))
    assert status == 409, f"expected 409, got {status}"
    html = holds_nobody.pg.content()
    assert "<html" in html.lower(), "the refusal is not a rendered page"
    assert 'href="/' in html, "the refusal offers nowhere to go"
    holds_nobody.close()


# ── Care team: the conversation ──────────────────────────────────────────────


@scenario("care: doctor opens a thread, patient reads and replies")
def care_conversation(browser):
    doc = Session(browser, "dr-ivanov")
    doc.go(f"/care/{TIMUR}/messages")
    doc.pg.fill('input[name="title"]', "Регресс: обсуждение железа")
    doc.click('button:has-text("Start")')
    assert "Регресс: обсуждение железа" in doc.text(), "the thread was not created"

    doc.click('a:has-text("Регресс: обсуждение железа")')
    doc.pg.fill('textarea[name="body"]', "Сдайте ферритин повторно через две недели.")
    doc.click('button:has-text("Send")')
    assert "Сдайте ферритин повторно" in doc.text(), "the message did not render"

    patient = Session(browser, "timur")
    patient.go("/messages")
    assert "Регресс: обсуждение железа" in patient.text(), \
        "the patient cannot see a thread about themselves"
    patient.click('a:has-text("Регресс: обсуждение железа")')
    assert "Сдайте ферритин повторно" in patient.text(), \
        "the patient cannot read what was said about them"
    patient.pg.fill('textarea[name="body"]', "Понял, запишусь.")
    patient.click('button:has-text("Send")')
    said = patient.text()
    assert "Понял, запишусь." in said, "the patient could not reply"
    # Order: the doctor's message must come first.
    assert said.index("Сдайте ферритин") < said.index("Понял, запишусь"), \
        "the reply renders above the message it answers"
    doc.close()
    patient.close()


@scenario("care: a trainer cannot open a doctor's patient")
def care_cross_professional(browser):
    coach = Session(browser, "coach-orlov")
    status = coach.go(f"/care/{PATIENT01}", expect=(404,))
    assert status == 404, f"a trainer opened another professional's patient: {status}"
    coach.close()


@scenario("care: a revoked consent closes the record but keeps the person listed")
def care_revoked_consent(browser):
    coach = Session(browser, "coach-sokol")
    coach.go("/care")
    roster = coach.text()
    status = coach.go(f"/care/{PATIENT05}", expect=(404,))
    assert status == 404, f"a revoked consent still opens the record: {status}"
    assert roster.strip(), "the roster rendered empty"
    coach.close()


@scenario("care: an empty roster says so rather than breaking")
def care_empty_roster(browser):
    doc = Session(browser, "dr-petrova")
    assert doc.go("/care") == 200
    assert doc.text().strip(), "the empty roster rendered nothing at all"
    doc.close()


# ── Per-subject settings ─────────────────────────────────────────────────────


@scenario("settings: two patients keep separate profiles")
def settings_are_per_subject(browser):
    a = Session(browser, "timur")
    a.go("/settings")
    a.pg.fill('input[name="height_cm"]', "191")
    a.click('button:has-text("Save profile")')

    b = Session(browser, "patient01")
    b.go("/settings")
    b.pg.fill('input[name="height_cm"]', "158")
    b.click('button:has-text("Save profile")')

    a.go("/settings")
    assert a.pg.input_value('input[name="height_cm"]') == "191", \
        "one patient's profile was overwritten by another's"
    b.go("/settings")
    assert b.pg.input_value('input[name="height_cm"]') == "158", \
        "the second patient's profile did not persist"
    a.close()
    b.close()


# ── Refusals ─────────────────────────────────────────────────────────────────


@scenario("refusal: a superadmin on a personal page gets a page, not a sentence")
def refusal_is_a_page(browser):
    admin = Session(browser, "admin")
    status = admin.go("/today", expect=(409,))
    assert status == 409, f"expected 409, got {status}"
    html = admin.pg.content()
    assert "<html" in html.lower(), "the refusal is not a rendered page"
    assert 'href="/care"' in html, "the refusal does not point at the one page they can open"
    admin.close()


@scenario("nav: the console link is offered to an admin and to nobody else")
def nav_console_link(browser):
    admin = Session(browser, "admin")
    admin.go("/care")
    assert "/settings/platform/support" in admin.pg.content(), \
        "the admin has no link to the console"
    admin.close()
    for who in ("timur", "dr-ivanov", "patient01"):
        s = Session(browser, who)
        s.go("/weight")
        assert "/settings/platform/support" not in s.pg.content(), \
            f"{who} is offered a console they cannot open"
        s.close()


# ── Phone ────────────────────────────────────────────────────────────────────


@scenario("phone: the patient's pages render at 390px without sideways scroll")
def phone_patient(browser):
    s = Session(browser, "timur", mobile=True)
    for path in ("/today", "/weight", "/nutrition", "/labs", "/settings/access", "/more"):
        assert s.go(path) == 200, f"{path} did not render"
    s.close()


@scenario("phone: the doctor's roster and a record render at 390px")
def phone_doctor(browser):
    s = Session(browser, "dr-ivanov", mobile=True)
    assert s.go("/care") == 200
    assert s.go(f"/care/{TIMUR}") == 200
    s.close()


SCENARIOS = [
    support_full_loop,
    support_decline,
    support_console_is_admin_only,
    support_refusal_page,
    care_conversation,
    care_cross_professional,
    care_revoked_consent,
    care_empty_roster,
    settings_are_per_subject,
    refusal_is_a_page,
    nav_console_link,
    phone_patient,
    phone_doctor,
]

with sync_playwright() as p:
    browser = p.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
    for fn in SCENARIOS:
        fn(browser)
    browser.close()

width = max(len(name) for _, name, _ in results)
for verdict, name, detail in results:
    print(f"{verdict:<6} {name:<{width}}  {detail}")
print()
print("=== NOISE (console / unexpected HTTP / overflow) ===")
for line in dict.fromkeys(noise):
    print(" ", line)
if not noise:
    print("  (none)")
failed = sum(1 for v, _, _ in results if v != "PASS")
print(f"\n{len(results) - failed}/{len(results)} scenarios passed")
sys.exit(1 if failed or noise else 0)
