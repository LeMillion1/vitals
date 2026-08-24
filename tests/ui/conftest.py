"""A whole installation, started and thrown away around one browser run.

The scenarios need something no unit fixture provides: a real server, a real
browser, and a database holding more than one person. Every one of them is
built here so a run is one command — a browser suite that needs a README before
it works is one nobody runs, and these exist precisely because the cheap checks
were being trusted instead.
"""
from __future__ import annotations

import os
import pathlib
import re
import shutil
import socket
import subprocess
import sys
import time

import pytest

REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[2]

#: Where a failing test leaves what the browser was looking at. Gitignored:
#: these are evidence for the run that produced them and stale after the next.
DEFAULT_ARTIFACTS = REPOSITORY_ROOT / ".ui-failures"


def pytest_addoption(parser):
    parser.addoption(
        "--ui-artifacts",
        default=str(DEFAULT_ARTIFACTS),
        help="where to leave screenshots and HTML from failing browser tests",
    )


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    """Remember how each phase went, so a fixture teardown can ask.

    A fixture cannot otherwise tell a passing test from a failing one, and
    screenshotting every test would bury the one that matters in a hundred that
    do not.
    """

    outcome = yield
    setattr(item, f"_ui_report_{call.when}", outcome.get_result())

#: The seeder's fixed development key, so the server it starts can read what it
#: wrote. Not a secret in any sense: it exists to make a throwaway database
#: readable.
CREDENTIAL_KEY = "c2VlZC1jYXJlLWRlbW8ta2V5LTMyLWJ5dGVzLWFhYWE="
SESSION_SECRET = "local-secret-key-1234567890"
OWNER_USERNAME = "timur"
OWNER_PASSWORD_HASH = (
    "$2b$04$V2PTdRXGL2bhQbX8frCBeuQp8X01Cj84UQCRKDsVNGAOU/siMDlha"
)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _chromium() -> str | None:
    """The browser Playwright already downloaded, rather than another download."""

    override = os.environ.get("VITALS_CHROMIUM")
    if override:
        return override if pathlib.Path(override).exists() else None
    for cache in (
        pathlib.Path.home() / "Library/Caches/ms-playwright",
        pathlib.Path.home() / ".cache/ms-playwright",
    ):
        for build in sorted(cache.glob("chromium-*"), reverse=True):
            for relative in (
                "chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/"
                "Google Chrome for Testing",
                "chrome-mac/Chromium.app/Contents/MacOS/Chromium",
                "chrome-linux/chrome",
            ):
                found = build / relative
                if found.exists():
                    return str(found)
    return None


def _environment(database: pathlib.Path) -> dict[str, str]:
    """What both the seeder and the server need, and nothing from the developer.

    Built from scratch rather than inherited so a run cannot pick up the
    developer's own ``.env`` — the shared-installation sweep learned that the
    hard way by writing a real Garmin address into it.
    """

    environment = dict(os.environ)
    environment.update(
        {
            "VITALS_DATABASE_URL": f"sqlite+aiosqlite:///{database}",
            "VITALS_ENV_FILE": str(database.with_suffix(".env")),
            "VITALS_SESSION_SECRET": SESSION_SECRET,
            "VITALS_AUTH_USERNAME": OWNER_USERNAME,
            "VITALS_AUTH_PASSWORD_HASH": OWNER_PASSWORD_HASH,
            "VITALS_CREDENTIAL_KEY": CREDENTIAL_KEY,
            "VITALS_COOKIE_SECURE": "false",
            "VITALS_TIMEZONE": "Europe/Chisinau",
            "PYTHONPATH": str(REPOSITORY_ROOT),
        }
    )
    return environment


def _parse_accounts(output: str) -> dict[str, str]:
    """Read the session cookie the seeder prints for each account.

    The password login authenticates exactly one username from the environment,
    so a cookie is the only way to be somebody else in a browser here — and
    inventing a test-only way in for the app would be a worse idea than reading
    the one the seeder already prints.
    """

    cookies: dict[str, str] = {}
    pending = None
    for line in output.splitlines():
        match = re.match(r"^\s{4}(\S+)\s{2,}", line)
        if match:
            pending = match.group(1)
            continue
        stripped = line.strip()
        if pending and stripped.startswith("eyJ"):
            cookies[pending] = stripped
            pending = None
    return cookies


@pytest.fixture(scope="session")
def playwright_api():
    api = pytest.importorskip(
        "playwright.sync_api",
        reason="browser tests need Playwright: pip install playwright",
    )
    if _chromium() is None:
        pytest.skip("no Chromium under ms-playwright; python -m playwright install chromium")
    return api


@pytest.fixture(scope="session")
def installation(tmp_path_factory, playwright_api):
    """A seeded, running Vitals on its own port and its own database file."""

    del playwright_api
    workdir = tmp_path_factory.mktemp("ui")
    database = workdir / "ui_vitals.db"
    environment = _environment(database)

    seeded = subprocess.run(
        [sys.executable, "scripts/seed_care_demo.py"],
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        text=True,
    )
    if seeded.returncode != 0:
        pytest.fail(f"seeding failed:\n{seeded.stdout}\n{seeded.stderr}")
    accounts = _parse_accounts(seeded.stdout)
    if not accounts:
        pytest.fail(f"the seeder printed no session cookies:\n{seeded.stdout}")

    port = _free_port()
    server = subprocess.Popen(
        [sys.executable, "-m", "tests.ui._serve", str(port)],
        cwd=REPOSITORY_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_for(server, base_url)
        yield Installation(base_url, accounts, database)
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - a wedged server
            server.kill()
        shutil.rmtree(workdir, ignore_errors=True)


def _wait_for(server: subprocess.Popen, base_url: str, seconds: int = 40) -> None:
    import urllib.error
    import urllib.request

    deadline = time.time() + seconds
    while time.time() < deadline:
        if server.poll() is not None:
            pytest.fail(f"the server exited:\n{server.stdout.read() if server.stdout else ''}")
        try:
            with urllib.request.urlopen(f"{base_url}/login", timeout=2) as answer:
                if answer.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            time.sleep(0.4)
    pytest.fail("the server did not start in time")


class Installation:
    """Where it is running, who lives in it, and how to reach their records."""

    def __init__(self, base_url: str, accounts: dict[str, str], database: pathlib.Path):
        self.base_url = base_url
        self.accounts = accounts
        self.database = database
        self._subjects: dict[str, str] = {}

    def subject_of(self, username: str) -> str:
        """The record this account owns, read rather than pasted.

        Every id changes on a reseed, so a hardcoded one becomes a 404 that
        reads exactly like a regression.
        """

        if username not in self._subjects:
            import sqlite3

            with sqlite3.connect(self.database) as db:
                row = db.execute(
                    "SELECT hs.id FROM health_subjects hs "
                    "JOIN users u ON u.id = hs.owner_user_id "
                    "WHERE u.normalized_username = ?",
                    (username,),
                ).fetchone()
            assert row is not None, f"{username} owns no record in the seeded database"
            raw = row[0]
            self._subjects[username] = (
                f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"
            )
        return self._subjects[username]


#: Tables a scenario writes into, newest-first so a child is removed before the
#: parent it points at. Everything a test creates lives in one of these; the
#: seeded installation itself is never touched.
_SCENARIO_TABLES = (
    "care_messages",
    "care_thread_participants",
    "care_threads",
    "support_access_request_scopes",
    "support_access_requests",
    "support_access_scopes",
    "support_access_grants",
    "audit_events",
    "external_api_tokens",
)


@pytest.fixture(autouse=True)
def a_clean_installation(installation):
    """Leave the installation as the seeder built it.

    Starting and seeding a whole app costs about fifteen seconds, so the
    installation is shared and the tests are not isolated by construction. That
    is a real hazard rather than a saving to be quiet about: a pending support
    request one scenario leaves behind is one the next scenario finds and
    reasonably concludes something is broken. Four tests failed exactly that way
    before this existed, all of them correct about a product that was fine.

    So the ids present after seeding are remembered once, and everything a test
    adds is removed after it. Not a truncate: the seeded conversation and the
    seeded relationships are what several scenarios read, and a fixture that
    wiped them would trade one kind of order-dependence for another.
    """

    import sqlite3

    baseline = getattr(installation, "_baseline", None)
    if baseline is None:
        baseline = {}
        with sqlite3.connect(installation.database) as db:
            for table in _SCENARIO_TABLES:
                baseline[table] = {
                    row[0] for row in db.execute(f"SELECT id FROM {table}")
                }
        installation._baseline = baseline

    yield

    with sqlite3.connect(installation.database) as db:
        for table in _SCENARIO_TABLES:
            keep = baseline[table]
            added = [
                row[0]
                for row in db.execute(f"SELECT id FROM {table}")
                if row[0] not in keep
            ]
            for row_id in added:
                db.execute(f"DELETE FROM {table} WHERE id = ?", (row_id,))
        db.commit()


@pytest.fixture(scope="session")
def browser(playwright_api):
    with playwright_api.sync_playwright() as api:
        launched = api.chromium.launch(
            executable_path=_chromium(), args=["--no-sandbox"]
        )
        yield launched
        launched.close()


@pytest.fixture
def complaints():
    """Collected per test, and raised at the end of it.

    A fixture rather than a helper each test remembers to call: the one that
    forgets is the one that walks past a 500.
    """

    from tests.ui.console import Complaints

    gathered = Complaints()
    yield gathered
    gathered.raise_if_any()


@pytest.fixture
def sign_in(request, browser, installation, complaints):
    """Open a browser as one of the seeded accounts.

    Each call is its own context, so two roles in one test are two genuinely
    separate browsers rather than one session pretending.
    """

    from tests.ui.roles import Role

    opened = []
    pages: list[tuple[str, object]] = []

    def _sign_in(username: str, *, phone: bool = False) -> Role:
        assert username in installation.accounts, (
            f"{username} is not one of the seeded accounts: "
            f"{sorted(installation.accounts)}"
        )
        viewport = (
            {"width": 390, "height": 844} if phone else {"width": 1280, "height": 900}
        )
        extra = {"is_mobile": True, "has_touch": True} if phone else {}
        context = browser.new_context(viewport=viewport, **extra)
        context.add_cookies(
            [
                {
                    "name": "vitals_session",
                    "value": installation.accounts[username],
                    "domain": "127.0.0.1",
                    "path": "/",
                }
            ]
        )
        page = context.new_page()
        complaints.watch(page, username)
        opened.append(context)
        pages.append((username, page))
        return Role(username, page, installation, complaints)

    yield _sign_in

    # Photographed before anything is closed, and only when it went wrong: a
    # failure message says a screen was not what it should be, and this is the
    # screen. One per role, because a two-role flow fails on one of them and
    # which one is the question.
    failed = any(
        getattr(request.node, f"_ui_report_{phase}", None)
        and getattr(request.node, f"_ui_report_{phase}").failed
        for phase in ("setup", "call")
    )
    if failed:
        _photograph(request, pages)
    for context in opened:
        context.close()


def _photograph(request, pages) -> None:
    """Leave the screen and its markup beside the failure that produced them."""

    where = pathlib.Path(request.config.getoption("--ui-artifacts"))
    where.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", request.node.nodeid)[-120:]
    saved = []
    for username, page in pages:
        base = where / f"{stem}__{username}"
        try:
            page.screenshot(path=f"{base}.png", full_page=True)
            base.with_suffix(".html").write_text(page.content(), encoding="utf-8")
            saved.append(f"{base}.png")
        except Exception:  # pragma: no cover - a browser that already died
            continue
    if saved:
        # Printed rather than logged: the person reading a red test is looking
        # at the terminal, and a path they have to go find is a path nobody
        # opens.
        print("\n  the browser was looking at:")
        for path in saved:
            print(f"    {path}")
