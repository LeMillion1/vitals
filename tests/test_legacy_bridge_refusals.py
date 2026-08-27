"""Every sole-subject refusal reaches the browser as a 409, not as a 500.

While this migration is unfinished, a shared installation meets routes that
still resolve their subject through a sole-owner bridge. Those routes refusing
is correct — nothing is written and nobody else's row is read. What is not
correct is the refusal arriving as an unhandled exception: a stack trace tells
whoever meets it to go looking for a bug, and the bug is not there.

The refusals grew one per compatibility bridge and share no base class, so the
set of them is a hand-kept list in ``web.main``. A hand-kept list drifts, and
this one did: ``AlertLegacyBridgeError`` was added to the tuple used at startup
but not to the decorators used per request, and four pages served a 500 with a
traceback for it. Both now come from the same tuple, and the first test here is
what keeps anything new from being half-added again.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil

import pytest

import vitals.services as services_package

main_module = pytest.importorskip("web.main")


#: Bridge refusals that no HTTP request can reach, with the reason each is out
#: of scope. Anything not here has to be handled, or the test below fails.
_NOT_REQUEST_REACHABLE = {
    # Raised while reconciling the ``.env`` owner during lifespan startup, which
    # has its own handling: the app logs and carries on without a legacy
    # identity rather than refusing to boot.
    "vitals.services.identity.bootstrap": "startup-only owner reconciliation",
    "vitals.services.tenancy.bootstrap": "startup-only resource-root creation",
}


def _candidate_refusals() -> dict[str, type[BaseException]]:
    """Every exception class that names itself part of a legacy bridge.

    Matched on the name rather than a marker base class, because the point is to
    catch a class somebody adds *without* thinking about this list. A marker
    they had to remember to apply would be forgotten in exactly the case the
    test exists for.
    """

    found: dict[str, type[BaseException]] = {}
    for module_info in pkgutil.walk_packages(
        services_package.__path__, services_package.__name__ + "."
    ):
        try:
            module = importlib.import_module(module_info.name)
        except Exception:  # pragma: no cover - an optional dependency is absent
            continue
        for name, obj in vars(module).items():
            if not inspect.isclass(obj) or not issubclass(obj, BaseException):
                continue
            if obj.__module__ != module.__name__:
                continue
            lowered = name.lower()
            if "legacybridge" in lowered or "bridgeclosed" in lowered:
                found[f"{obj.__module__}.{name}"] = obj
    return found


def test_every_legacy_bridge_refusal_is_handled_or_explained():
    handled = tuple(main_module._LEGACY_BOOTSTRAP_CLOSED)
    assert handled, "the refusal list is the porting backlog and is never empty"

    unhandled = []
    for path, error in sorted(_candidate_refusals().items()):
        module_name = path.rsplit(".", 1)[0]
        if module_name in _NOT_REQUEST_REACHABLE:
            continue
        if not issubclass(error, handled):
            unhandled.append(path)

    assert not unhandled, (
        "these sole-subject refusals would reach a browser as a 500: "
        + ", ".join(unhandled)
        + " — add them to web.main._LEGACY_BOOTSTRAP_CLOSED, or to "
        "_NOT_REQUEST_REACHABLE here with the reason they cannot be reached"
    )


def test_each_listed_refusal_has_a_registered_handler():
    """The tuple is the source, and registration actually consumed all of it."""

    registered = main_module.app.exception_handlers
    for error in main_module._LEGACY_BOOTSTRAP_CLOSED:
        assert registered.get(error) is main_module.legacy_ownership_handler, (
            f"{error.__name__} is listed as a bridge refusal but no handler is "
            "registered for it"
        )


def test_the_refusal_is_a_conflict_and_says_so_in_words():
    """Not 500, not 403, and not an empty body.

    409 rather than 403 because nothing was denied to this person: the page
    cannot answer for anybody yet. And the body carries the sentence, so the
    reader learns what happened without opening the network tab.
    """

    from fastapi import status
    from starlette.datastructures import Headers
    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/weight",
        "headers": Headers({"accept": "text/html"}).raw,
        "query_string": b"",
    }
    request = Request(scope)

    import asyncio

    response = asyncio.run(
        main_module.legacy_ownership_handler(request, RuntimeError("probe"))
    )
    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.body.decode("utf-8").strip()


def _render(handler, exc, *, path: str = "/weight", state: dict | None = None):
    """Run one refusal handler against a bare browser GET."""

    import asyncio

    from starlette.datastructures import Headers
    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": Headers({"accept": "text/html"}).raw,
        "query_string": b"",
        "state": dict(state or {}),
    }
    return asyncio.run(handler(Request(scope), exc))


@pytest.mark.parametrize(
    "handler_name, exc_factory",
    [
        ("legacy_ownership_handler", lambda: RuntimeError("probe")),
        ("no_personal_record_handler", lambda: Exception("probe")),
        ("access_denied_handler", lambda: Exception("probe")),
    ],
)
def test_a_refused_browser_navigation_gets_a_page_not_a_sentence(
    handler_name, exc_factory
):
    """Every browser-facing refusal has to be somewhere you can leave from.

    All three used to answer ``HTMLResponse(content=detail)``: one unstyled
    sentence on a white page, no masthead and no link anywhere. The sentence was
    correct, which is why the suites were happy — a status assertion cannot see
    that the page is a dead end. A superadmin on a shared installation can open
    exactly one address, ``/care``, and every other one left them stranded with
    no way to find it.

    Asserted on the anchor rather than on the styling: what makes it a page
    instead of an error string is that it offers somewhere to go.
    """

    body = _render(getattr(main_module, handler_name), exc_factory()).body.decode()
    assert "<html" in body.lower(), "the refusal is not a rendered page"
    assert 'href="/' in body, "the refusal offers nowhere to go"


def test_the_access_denial_page_still_says_nothing_about_the_record():
    """A denial and a miss have to look the same from outside.

    The page gained a headline and a button; it must not have gained a hint.
    Naming the subject, or wording that separates "you may not" from "it is not
    there", turns the refusal itself into a way to probe for who exists.
    """

    body = _render(
        main_module.access_denied_handler,
        Exception("probe"),
        path="/care/00000000-0000-0000-0000-000000000001",
    ).body.decode()
    assert "00000000-0000-0000-0000-000000000001" not in body
