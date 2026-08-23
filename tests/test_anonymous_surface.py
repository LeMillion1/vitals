"""The list of things a stranger may reach, kept honest by a test.

Every route in this app is meant to sit behind ``require_auth``. A handful can't:
the login pages, the published doctor document, the OAuth dance, the webhook, the
liveness probe. That handful is written out below, and the sweep fails the moment
anything else joins it — which is how ``GET /openapi.json`` (122 paths, including
``/glp1/injection`` and ``/hrt/*``, served to anyone who asked) stayed open for
months while ``/docs`` and ``/redoc`` were shut.
"""
import os

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute, iter_route_contexts

from web.deps import require_auth
from web.main import UPLOADS_DIR, app, serve_upload

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
    # The federated login handshake. Both run before a session can exist, which
    # is the point of them: /auth/start has nothing to carry but a redirect to
    # the provider, and /auth/callback is where a session is created rather than
    # required. Both are 404 until a provider is configured, and the callback is
    # rate-limited by IP because it is the pre-auth entry point.
    ("GET", "/auth/start"),
    ("GET", "/auth/callback"),
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
    # The seal over the private uploads tree. It answers 404 to everybody and
    # serves nothing at all — see web/main.py. Deliberately without a session
    # dependency: requiring one would answer 401 to a stranger and 404 to the
    # owner, and that difference tells the stranger the path was real.
    ("GET", "/static/uploads/{key:path}"),
    # The OAuth handshake Claude.ai runs before it holds any credential.
    ("GET", "/.well-known/oauth-authorization-server"),
    ("GET", "/.well-known/oauth-protected-resource"),
    ("GET", "/oauth/authorize"),
    ("POST", "/oauth/authorize/approve"),
    ("POST", "/oauth/token"),
}

# Mounts and plain Starlette routes, which carry no FastAPI dependency chain to
# inspect. ``/mcp`` authenticates every call itself (Bearer access token).
# ``/static`` is site furniture the login page needs before anyone has a session;
# the uploaded medical files that used to sit inside it are carved out by a real
# guarded route above the mount — see ``serve_upload`` below.
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


# ── Uploaded files ────────────────────────────────────────────────────────────
# Lab sheets, InBody printouts and progress photos live under ``static/uploads``.
# A random file name is not an access control, and the URL outlives the session.
# They are addressed by ``FileAsset.opaque_key`` now — a rotatable UUID with no
# relationship to the bytes — and the path they sit at serves nothing at all.


@pytest.fixture
def an_uploaded_file():
    """A real file in the uploads tree, removed again afterwards."""
    os.makedirs(UPLOADS_DIR, exist_ok=True)
    name = "test_anonymous_surface_probe.txt"
    path = os.path.join(UPLOADS_DIR, name)
    with open(path, "wb") as fh:
        fh.write(b"lab sheet bytes")
    yield name
    os.remove(path)


@pytest.fixture
async def an_owned_asset(db_session, owner_write, an_uploaded_file):
    """One progress photo with its file, reachable by its opaque key."""
    from datetime import date

    from vitals.enums import FileAssetPurpose
    from vitals.services import file_asset_service, weight_service

    file_key = f"uploads/{an_uploaded_file}"
    asset = await file_asset_service.register_legacy_local(
        db_session,
        subject_id=owner_write.subject_id,
        uploaded_by_user_id=owner_write.identity.actor_user_id,
        purpose=FileAssetPurpose.PROGRESS_PHOTO,
        storage_ref=file_key,
        media_type="image/png",
        size_bytes=15,
        content_sha256="8" * 64,
    )
    await weight_service.add_progress_photo(
        db_session,
        on_date=date(2026, 8, 20),
        file_key=file_key,
        identity=owner_write.identity,
        file_asset_id=asset.id,
        prepared_conflict_write=await owner_write.write(date(2026, 8, 20)),
    )
    await db_session.commit()
    return asset


async def test_the_owner_gets_the_file_by_its_opaque_key(auth_client, an_owned_asset):
    r = await auth_client.get(f"/files/{an_owned_asset.opaque_key}")
    assert r.status_code == 200
    assert r.content == b"lab sheet bytes"
    # Nothing left in the disk cache for the next person holding the device.
    assert "no-store" in r.headers["cache-control"]


async def test_a_download_needs_a_session(client, an_owned_asset):
    """No session, no file — whoever holds the link included."""
    r = await client.get(f"/files/{an_owned_asset.opaque_key}")
    assert r.status_code == 401
    assert b"lab sheet bytes" not in r.content

    # Typed into a browser it lands on the login form, like any other page.
    r = await client.get(
        f"/files/{an_owned_asset.opaque_key}", headers={"Accept": "text/html"}
    )
    assert r.status_code == 302
    assert r.headers["location"].startswith("/login")


async def test_a_key_nobody_owns_is_a_miss(auth_client, an_owned_asset):
    """Unknown, malformed and not-a-UUID all answer the same thing.

    Three different facts about a key, and any difference between the answers is
    the oracle somebody guessing URLs would use to tell them apart.
    """
    import uuid

    for key in (str(uuid.uuid4()), "not-a-uuid", "../../app.js", ""):
        r = await auth_client.get(f"/files/{key}")
        assert r.status_code == 404, key
        assert b"lab sheet bytes" not in r.content


async def test_a_deleted_asset_stops_being_downloadable(
    auth_client, db_session, an_owned_asset
):
    """Lifecycle decides, and it decides in the same voice as a missing key."""
    from vitals.services import file_asset_service

    await file_asset_service.mark_legacy_local_deleted(
        db_session,
        file_asset_id=an_owned_asset.id,
        subject_id=an_owned_asset.subject_id,
        purged=False,
    )
    await db_session.commit()

    r = await auth_client.get(f"/files/{an_owned_asset.opaque_key}")
    assert r.status_code == 404
    assert b"lab sheet bytes" not in r.content


async def test_the_uploads_path_serves_nothing_to_anybody(
    client, auth_client, an_uploaded_file
):
    """The prefix is sealed: not for strangers, and not for the owner either.

    While the bytes live inside the static tree, the only thing standing between
    the mount and a medical record is this route claiming the prefix first. It
    is easier to be sure of that when the route has no success case at all.
    """
    path = f"/static/uploads/{an_uploaded_file}"
    for c in (client, auth_client):
        r = await c.get(path)
        assert r.status_code == 404
        assert b"lab sheet bytes" not in r.content


async def test_the_seal_is_matched_before_the_static_mount():
    """Routes match in registration order: below the mount this is dead code."""
    paths = [getattr(route, "path", None) for route in app.routes]
    assert paths.index("/static/uploads/{key:path}") < paths.index("/static")


async def test_the_seal_answers_nothing_whatever_the_key():
    """Including the shapes that used to need their own containment check."""
    for key in ("app.js", "../app.js", "../../main.py", "body/../../app.js", ""):
        with pytest.raises(HTTPException) as excinfo:
            await serve_upload(key)
        assert excinfo.value.status_code == 404, key
