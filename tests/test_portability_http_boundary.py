"""Security properties of the browser data-portability boundary."""

from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

import pytest

from web.auth import create_federated_session
from web.config import SESSION_COOKIE


@pytest.mark.parametrize(
    ("method", "path"),
    (
        ("get", "/settings/export"),
        ("get", "/settings/export-subject"),
        ("get", "/settings/export-llm"),
        ("post", "/settings/import"),
        ("post", "/settings/import-subject"),
    ),
)
async def test_every_portability_transfer_requires_recent_authentication(
    client, legacy_owner_roots, method, path
):
    """A live but old browser session cannot move medical data in or out."""

    client.cookies.set(
        SESSION_COOKIE,
        create_federated_session(
            username="tester",
            user_id=legacy_owner_roots.user_id,
            session_version=1,
            authenticated_at=int(datetime.now(timezone.utc).timestamp()) - 3600,
            subject_id=legacy_owner_roots.subject_id,
        ),
    )
    kwargs = {
        "headers": {
            "Accept": "text/html",
            "Referer": "http://test/settings/data",
        },
        "follow_redirects": False,
    }
    if method == "post":
        payload = json.dumps(
            {"metadata": {"version": "1.0", "kind": "subject_record"}}
        )
        kwargs["files"] = {
            "backup_file": (
                "record.json",
                io.BytesIO(payload.encode()),
                "application/json",
            )
        }

    response = await getattr(client, method)(path, **kwargs)

    assert response.status_code == 303
    target = urlsplit(response.headers["location"])
    assert target.path == "/login"
    assert parse_qs(target.query)["next"] == ["/settings/data"]


async def test_stale_htmx_import_navigates_to_step_up(
    client, legacy_owner_roots
):
    """The real upload form must navigate instead of failing as an inert XHR."""

    client.cookies.set(
        SESSION_COOKIE,
        create_federated_session(
            username="tester",
            user_id=legacy_owner_roots.user_id,
            session_version=1,
            authenticated_at=int(datetime.now(timezone.utc).timestamp()) - 3600,
            subject_id=legacy_owner_roots.subject_id,
        ),
    )
    response = await client.post(
        "/settings/import-subject",
        files={
            "backup_file": (
                "record.json",
                io.BytesIO(b"{}"),
                "application/json",
            )
        },
        headers={
            "Accept": "*/*",
            "HX-Request": "true",
            "Referer": "http://test/settings/data",
        },
        follow_redirects=False,
    )

    assert response.status_code == 401
    target = urlsplit(response.headers["hx-redirect"])
    assert target.path == "/login"
    assert parse_qs(target.query)["next"] == ["/settings/data"]


@pytest.mark.parametrize(
    ("oidc_enabled", "expected_path"),
    ((False, "/login"), (True, "/auth/start")),
)
async def test_stale_json_portability_names_the_local_reauthentication_route(
    client,
    legacy_owner_roots,
    monkeypatch,
    oidc_enabled,
    expected_path,
):
    """Fetch callers get the deployment's real step-up route, not a guess."""

    oidc_environment = {
        "VITALS_OIDC_ISSUER": "https://idp.example.test",
        "VITALS_OIDC_CLIENT_ID": "vitals-test",
        "VITALS_OIDC_CLIENT_SECRET": "synthetic-secret",
        "VITALS_OIDC_REDIRECT_URL": "https://vitals.example.test/auth/callback",
    }
    for name in oidc_environment:
        monkeypatch.delenv(name, raising=False)
    if oidc_enabled:
        monkeypatch.setenv("VITALS_PUBLIC_URL", "https://vitals.example.test")
        for name, value in oidc_environment.items():
            monkeypatch.setenv(name, value)

    client.cookies.set(
        SESSION_COOKIE,
        create_federated_session(
            username="tester",
            user_id=legacy_owner_roots.user_id,
            session_version=1,
            authenticated_at=int(datetime.now(timezone.utc).timestamp()) - 3600,
            subject_id=legacy_owner_roots.subject_id,
        ),
    )
    response = await client.post(
        "/settings/portability-v2/inspect",
        data={"passphrase": "synthetic-passphrase"},
        files={
            "archive_file": (
                "record.vitals",
                io.BytesIO(b"not-an-archive"),
                "application/octet-stream",
            )
        },
        headers={
            "Accept": "application/json",
            "Referer": "http://test/settings/data",
        },
        follow_redirects=False,
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Recent authentication required"}
    target = urlsplit(response.headers["x-vitals-reauthentication"])
    assert target.path == expected_path
    query = parse_qs(target.query)
    assert query["next"] == ["/settings/data"]
    assert query.get("step_up") == (["true"] if oidc_enabled else None)
