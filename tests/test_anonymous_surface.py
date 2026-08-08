"""The list of things a stranger may reach, kept honest by a test.

Every route in this app is meant to sit behind ``require_auth``. A handful can't:
the login pages, the published doctor document, the OAuth dance, the webhook, the
liveness probe. That handful is written out below, and the sweep fails the moment
anything else joins it — which is how ``GET /openapi.json`` (122 paths, including
``/glp1/injection`` and ``/hrt/*``, served to anyone who asked) stayed open for
months while ``/docs`` and ``/redoc`` were shut.
"""
from fastapi.routing import APIRoute, iter_route_contexts

from web.deps import require_auth
from web.main import app

# (method, path) pairs that answer without a session cookie, each with the reason
# it may. Anything not here MUST depend on require_auth.
ANONYMOUS_BY_DESIGN = {
    # Redirects to /today, which redirects to /login. Carries nothing.
    ("GET", "/"),
    ("GET", "/login"),
    ("POST", "/login"),
    ("GET", "/login/2fa"),
    ("POST", "/login/2fa"),
    # Nothing to authenticate — it only clears cookies.
    ("POST", "/logout"),
    # Liveness for external monitoring. Job names are owner-only — see web/main.py.
    ("GET", "/health"),
    # Bearer token, not a session.
    ("GET", "/external/summary"),
    # Secret path plus Telegram's own header.
    ("POST", "/tg/{secret_path}"),
    # The published doctor document: the visitor has no account, the link carries
    # a token and the page asks for a password.
    ("GET", "/r/{token}"),
    ("POST", "/r/{token}"),
    # The OAuth handshake Claude.ai runs before it holds any credential.
    ("GET", "/.well-known/oauth-authorization-server"),
    ("GET", "/.well-known/oauth-protected-resource"),
    ("GET", "/oauth/authorize"),
    ("POST", "/oauth/authorize/approve"),
    ("POST", "/oauth/token"),
}

# Mounts and plain Starlette routes, which carry no FastAPI dependency chain to
# inspect. ``/mcp`` authenticates every call itself (Bearer access token).
NON_API_BY_DESIGN = {"/static", "/mcp"}


def _dependencies(dependant):
    yield dependant
    for sub in dependant.dependencies:
        yield from _dependencies(sub)


def test_no_route_answers_anonymously_unless_it_is_on_the_list():
    unguarded = set()
    foreign = set()

    for context in iter_route_contexts(app.routes):
        route = context.route
        if not isinstance(route, APIRoute):
            if context.path not in NON_API_BY_DESIGN:
                foreign.add(context.path)
            continue
        if any(d.call is require_auth for d in _dependencies(route.dependant)):
            continue
        for method in route.methods or ():
            if (method, context.path) not in ANONYMOUS_BY_DESIGN:
                unguarded.add((method, context.path))

    assert not foreign, (
        f"mounted anonymously without a dependency chain to check: {sorted(foreign)} — "
        "either guard it or add it to NON_API_BY_DESIGN with the reason"
    )
    assert not unguarded, (
        f"reachable without a session: {sorted(unguarded)} — add require_auth, or "
        "add it to ANONYMOUS_BY_DESIGN with the reason it may stay open"
    )


async def test_the_schema_is_not_published(client):
    """The route that listed every path in the app is gone, not just hidden."""
    assert app.openapi_url is None
    for path in ("/openapi.json", "/docs", "/redoc"):
        assert (await client.get(path)).status_code == 404, path
