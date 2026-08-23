"""Where a mutating request is allowed to come from.

Three barriers guard this, and the tests are about the seam between them. The
``SameSite`` cookie is the browser's business. The ``Origin`` check has a real
gap — a request carrying no ``Origin`` at all passes it, because "absent" and
"same-origin" look the same from the server. Fetch Metadata closes that: current
browsers send ``Sec-Fetch-Site`` on every request, and it names the relationship
rather than leaving it to be inferred.
"""

from __future__ import annotations

import pytest


async def test_a_same_origin_mutation_is_allowed(auth_client):
    response = await auth_client.post(
        "/settings/language",
        data={"language": "ru"},
        headers={"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors"},
    )
    assert response.status_code != 403


async def test_a_same_site_mutation_is_allowed(auth_client):
    """A self-hosted deployment may legitimately serve from a subdomain."""

    response = await auth_client.post(
        "/settings/language",
        data={"language": "ru"},
        headers={"Sec-Fetch-Site": "same-site"},
    )
    assert response.status_code != 403


@pytest.mark.parametrize("fetch_site", ["cross-site", "none"])
async def test_a_cross_site_mutation_is_refused(auth_client, fetch_site):
    """``none`` too: no browser issues a mutating request from a typed address.

    A form on an attacker's page posting here is ``cross-site``; anything
    claiming ``none`` for a POST is not a browser doing what browsers do.
    """

    response = await auth_client.post(
        "/settings/language",
        data={"language": "ru"},
        headers={"Sec-Fetch-Site": fetch_site},
    )
    assert response.status_code == 403


async def test_fetch_metadata_refuses_even_with_a_matching_origin(auth_client):
    """The header an attacker cannot set wins over the one they might.

    ``Sec-Fetch-*`` is forbidden to scripts, so a page cannot forge it. If the
    two disagree, the one that cannot be forged decides.
    """

    response = await auth_client.post(
        "/settings/language",
        data={"language": "ru"},
        headers={"Sec-Fetch-Site": "cross-site", "Origin": "http://testserver"},
    )
    assert response.status_code == 403


async def test_a_cross_origin_mutation_is_still_refused_without_fetch_metadata(
    auth_client,
):
    """The older barrier still stands for clients that send no metadata."""

    response = await auth_client.post(
        "/settings/language",
        data={"language": "ru"},
        headers={"Origin": "https://attacker.example.test"},
    )
    assert response.status_code == 403


async def test_reads_are_never_refused_by_either_check(auth_client):
    """A cross-site GET is how the provider's redirect comes back to us.

    Refusing it would break the OIDC callback, which is a top-level navigation
    from the provider's origin and therefore ``cross-site`` by definition.
    """

    response = await auth_client.get(
        "/today",
        headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"},
    )
    assert response.status_code != 403


async def test_the_oidc_callback_survives_a_cross_site_navigation(client):
    """The concrete case: what the provider's redirect looks like on the wire."""

    response = await client.get(
        "/auth/callback?code=c&state=s",
        headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"},
        follow_redirects=False,
    )
    # 404 because no provider is configured in this test — the point is that it
    # reached the route rather than being refused by the CSRF middleware.
    assert response.status_code == 404


@pytest.mark.parametrize(
    "path", ["/mcp", "/tg/whatever-secret", "/oauth/token"]
)
async def test_secret_bearing_callers_are_exempt(client, path):
    """They authenticate with their own credential, so there is nothing to forge.

    A forged cross-site request to these carries no session and gets nowhere;
    refusing it on CSRF grounds would 403 the Telegram webhook for sending a
    header it has no reason to send.
    """

    response = await client.post(
        path, headers={"Sec-Fetch-Site": "cross-site"}, json={}
    )
    assert response.status_code != 403
