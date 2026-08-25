"""Security properties of the browser data-portability boundary."""

from __future__ import annotations

import io
import json
from datetime import datetime, timezone

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
            "Referer": "http://test/settings",
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
    assert response.headers["location"].startswith("/login?")


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
            "Referer": "http://test/settings",
        },
        follow_redirects=False,
    )

    assert response.status_code == 401
    assert response.headers["hx-redirect"].startswith("/login?")
