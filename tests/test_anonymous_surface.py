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


async def test_an_uploaded_file_is_not_public(client, an_uploaded_file):
    """No session, no file — whoever holds the link included."""
    r = await client.get(f"/static/uploads/{an_uploaded_file}")
    assert r.status_code == 401
    assert b"lab sheet bytes" not in r.content

    # Typed into a browser it lands on the login form, like any other page.
    r = await client.get(
        f"/static/uploads/{an_uploaded_file}", headers={"Accept": "text/html"}
    )
    assert r.status_code == 302
    assert r.headers["location"].startswith("/login")


async def test_the_owner_still_gets_the_file(
    auth_client,
    db_session,
    an_uploaded_file,
    legacy_owner_roots,
    owner_write,
):
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

    r = await auth_client.get(f"/static/uploads/{an_uploaded_file}")
    assert r.status_code == 200
    assert r.content == b"lab sheet bytes"
    # Nothing left in the disk cache for the next person holding the device.
    assert "no-store" in r.headers["cache-control"]


async def test_unregistered_file_fallback_closes_when_a_second_subject_exists(
    auth_client, db_session, an_uploaded_file
, *, legacy_file_asset_id, legacy_owner_roots):
    """A legacy photo fact is authorized only by the exact-one-subject bridge."""
    from datetime import date

    from vitals.enums import Domain, Source, UserStatus
    from vitals.models.identity import HealthSubject, User
    from vitals.models.weight import ProgressPhoto

    db_session.add(
        ProgressPhoto(subject_id=legacy_owner_roots.subject_id, file_asset_id=legacy_file_asset_id, 
            date=date(2026, 8, 20),
            domain=Domain.WEIGHT.value,
            source=Source.MANUAL.value,
            file_key=f"uploads/{an_uploaded_file}",
        )
    )
    await db_session.flush()

    other = User(
        username="other-file-owner",
        normalized_username="other-file-owner",
        password_hash="$synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    db_session.add(other)
    await db_session.flush()
    db_session.add(
        HealthSubject(
            owner_user_id=other.id,
            display_name="Other file owner",
            timezone="Asia/Almaty",
        )
    )
    await db_session.commit()

    response = await auth_client.get(f"/static/uploads/{an_uploaded_file}")
    assert response.status_code == 404
    assert b"lab sheet bytes" not in response.content


async def test_the_route_is_matched_before_the_static_mount():
    """Routes match in registration order: below the mount this guard is dead code."""
    paths = [getattr(route, "path", None) for route in app.routes]
    assert paths.index("/static/uploads/{key:path}") < paths.index("/static")


async def test_climbing_out_of_the_uploads_tree_is_a_miss():
    """``..`` in the key must not reach the rest of the filesystem."""
    for key in ("../app.js", "../../main.py", "body/../../app.js"):
        with pytest.raises(HTTPException) as excinfo:
            await serve_upload(key)
        assert excinfo.value.status_code == 404, key
