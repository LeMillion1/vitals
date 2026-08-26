"""A client id is a URL somebody else chose, and this server fetches it.

That sentence is the whole risk. Client ID Metadata Documents replace Dynamic
Client Registration in the MCP profile, and the mechanism is that an
unauthenticated caller hands this server a URL and it makes a request to it —
from inside whatever network the installation runs in. The tests below are
mostly about the requests it must refuse to make.

The rest is about believing the document only as far as it proves itself: its
``client_id`` has to be the URL it came from, its redirect URIs have to be
complete https URLs, and the callback in an authorization request has to be one
of them exactly.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from sqlalchemy import select

from vitals.models.identity import McpAccessToken
from vitals.services.authentication import oauth_clients as client_metadata
from web.auth import _get_mcp_serializer

DOCUMENT_URL = "https://apps.example.test/connector.json"
CODE_VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"


@pytest.fixture(autouse=True)
def _no_cached_documents():
    client_metadata.forget()
    yield
    client_metadata.forget()


@pytest.fixture
def public_dns(monkeypatch):
    """Every hostname resolves to a public address unless a test says otherwise.

    A genuinely routable one. ``203.0.113.0/24`` is the documentation range and
    Python classifies it as private, which is correct of Python and made the
    first version of this fixture refuse its own happy path.
    """

    def _resolve(host, port, *args, **kwargs):
        return [(2, 1, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(client_metadata.socket, "getaddrinfo", _resolve)


def _serve(document, *, status_code: int = 200, body: bytes | None = None):
    """A transport that answers every request with this document."""

    def _handler(request: httpx.Request) -> httpx.Response:
        if body is not None:
            return httpx.Response(status_code, content=body)
        return httpx.Response(status_code, json=document)

    return httpx.MockTransport(_handler)


@pytest.fixture
def answering(monkeypatch):
    """Point the fetcher's client at a transport this test controls."""

    def _install(document, *, status_code: int = 200, body: bytes | None = None):
        original = httpx.AsyncClient

        def _client(*args, **kwargs):
            kwargs["transport"] = _serve(document, status_code=status_code, body=body)
            return original(*args, **kwargs)

        monkeypatch.setattr(client_metadata.httpx, "AsyncClient", _client)

    return _install


def _document(**overrides):
    base = {
        "client_id": DOCUMENT_URL,
        "client_name": "Kitchen Dashboard",
        "redirect_uris": ["https://apps.example.test/callback"],
        "token_endpoint_auth_method": "none",
    }
    base.update(overrides)
    return base


# ── The requests it must refuse to make ──────────────────────────────────────


@pytest.mark.parametrize(
    "client_id",
    [
        "http://apps.example.test/connector.json",
        "file:///etc/passwd",
        "ftp://apps.example.test/connector.json",
        "vitals-claude-connector",
        "https://apps.example.test",
        "https://user@apps.example.test/connector.json",
        "https://apps.example.test/a/../connector.json",
        "https://apps.example.test/a/%2e/connector.json",
        "https://apps.example.test/connector.json#fragment",
        "https://[not-an-ipv6-address/connector.json",
        "",
    ],
)
async def test_only_an_https_url_is_a_metadata_document(client_id):
    """A ``file://`` client id is not a client id.

    Refused before any resolution or request: the scheme decides whether this
    is a document at all, and reading a local file because somebody asked
    nicely is the first thing this must not do.
    """

    assert not client_metadata.looks_like_a_metadata_url(client_id)
    with pytest.raises(client_metadata.UnsafeMetadataUrl):
        await client_metadata.fetch(client_id)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",       # this process
        "10.0.0.5",        # a private network
        "192.168.1.10",    # a home network
        "172.16.0.5",      # the other private range
        "169.254.169.254", # cloud instance metadata, the classic target
        "0.0.0.0",
        "224.0.0.1",       # multicast
    ],
)
async def test_a_name_resolving_inside_the_network_is_refused(monkeypatch, address):
    """Checked after resolution, because a hostname says nothing.

    ``internal.example.com`` pointing at ``10.0.0.5`` is the entire trick, and
    inspecting the name would miss every one of these.
    """

    monkeypatch.setattr(
        client_metadata.socket,
        "getaddrinfo",
        lambda host, port, *a, **k: [(2, 1, 6, "", (address, port))],
    )
    with pytest.raises(client_metadata.UnsafeMetadataUrl):
        await client_metadata.fetch(DOCUMENT_URL)


async def test_one_private_answer_among_public_ones_is_still_refused(monkeypatch):
    """Every address, not the first.

    A name with one public and one private record would otherwise pass a check
    on the first answer and then connect to whichever the socket layer picked.
    """

    monkeypatch.setattr(
        client_metadata.socket,
        "getaddrinfo",
        lambda host, port, *a, **k: [
            (2, 1, 6, "", ("93.184.216.34", port)),
            (2, 1, 6, "", ("10.0.0.5", port)),
        ],
    )
    with pytest.raises(client_metadata.UnsafeMetadataUrl):
        await client_metadata.fetch(DOCUMENT_URL)


async def test_a_name_that_does_not_resolve_is_refused(monkeypatch):
    import socket as socket_module

    def _fail(*args, **kwargs):
        raise socket_module.gaierror("no such name")

    monkeypatch.setattr(client_metadata.socket, "getaddrinfo", _fail)
    with pytest.raises(client_metadata.UnsafeMetadataUrl):
        await client_metadata.fetch(DOCUMENT_URL)


async def test_a_redirect_is_not_followed(public_dns, answering):
    """Not one.

    Following a redirect means validating a second destination against
    everything above, and public-host-redirects-to-private is the second half
    of the same trick. The document lives at its client id or it does not exist.
    """

    answering(None, status_code=302)
    with pytest.raises(client_metadata.ClientMetadataError):
        await client_metadata.fetch(DOCUMENT_URL)


async def test_an_enormous_document_is_refused(public_dns, answering):
    """A client metadata document is a handful of fields."""

    answering(None, body=b"{" + b"x" * (client_metadata.MAX_DOCUMENT_BYTES + 10))
    with pytest.raises(client_metadata.ClientMetadataError):
        await client_metadata.fetch(DOCUMENT_URL)


# ── Believing it only as far as it proves itself ─────────────────────────────


async def test_a_well_formed_document_is_accepted(public_dns, answering):
    answering(_document())
    metadata = await client_metadata.fetch(DOCUMENT_URL)
    assert metadata.client_id == DOCUMENT_URL
    assert metadata.client_name == "Kitchen Dashboard"
    assert metadata.allows("https://apps.example.test/callback")


async def test_a_document_claiming_another_identity_is_refused(public_dns, answering):
    """Without this a client could name somebody else's document as its own.

    It would then inherit their redirect URIs, which is an authorization code
    delivered to an address its owner never approved.
    """

    answering(_document(client_id="https://apps.example.test/somebody-else.json"))
    with pytest.raises(client_metadata.ClientMetadataError):
        await client_metadata.fetch(DOCUMENT_URL)


@pytest.mark.parametrize(
    "overrides",
    [
        {"client_name": ""},
        {"client_name": "   "},
        {"client_name": "x" * 256},
        {"client_name": None},
        {"token_endpoint_auth_method": None},
        {"token_endpoint_auth_method": "client_secret_basic"},
        {"client_secret": "a-document-must-never-publish-this"},
        {"client_secret_expires_at": 0},
    ],
)
async def test_mcp_metadata_must_describe_a_named_public_client(
    public_dns, answering, overrides
):
    """The token endpoint implements ``none``, so the document must say that."""

    answering(_document(**overrides))
    with pytest.raises(client_metadata.ClientMetadataError):
        await client_metadata.fetch(DOCUMENT_URL)


@pytest.mark.parametrize(
    "redirect_uris",
    [
        [],
        ["http://apps.example.test/callback"],
        ["myapp://callback"],
        ["https://apps.example.test/callback#fragment"],
        [42],
        "https://apps.example.test/callback",
    ],
)
async def test_a_document_with_unusable_redirect_uris_is_refused(
    public_dns, answering, redirect_uris
):
    """``http`` and custom schemes are where an authorization code is intercepted."""

    answering(_document(redirect_uris=redirect_uris))
    with pytest.raises(client_metadata.ClientMetadataError):
        await client_metadata.fetch(DOCUMENT_URL)


async def test_a_body_that_is_not_json_is_refused(public_dns, answering):
    answering(None, body=b"<html>not a document</html>")
    with pytest.raises(client_metadata.ClientMetadataError):
        await client_metadata.fetch(DOCUMENT_URL)


async def test_a_callback_the_document_does_not_declare_is_not_allowed(
    public_dns, answering
):
    """Exact strings, not prefixes.

    Prefix matching on redirect URIs is a well-worn way to lose a code to an
    open redirect, and the document is specific by construction — the client
    wrote it — so there is no reason to be lenient.
    """

    answering(_document())
    metadata = await client_metadata.fetch(DOCUMENT_URL)
    for callback in (
        "https://apps.example.test/callback/../elsewhere",
        "https://apps.example.test/callback2",
        "https://evil.test/callback",
        "https://apps.example.test/",
    ):
        assert not metadata.allows(callback), callback


# ── Cached, but not indefinitely ─────────────────────────────────────────────


async def test_a_document_is_fetched_once_and_then_remembered(public_dns, monkeypatch):
    """An authorization flow fetches this on every attempt."""

    fetches = []
    original = httpx.AsyncClient

    def _client(*args, **kwargs):
        def _handler(request: httpx.Request) -> httpx.Response:
            fetches.append(request.url)
            return httpx.Response(200, json=_document())

        kwargs["transport"] = httpx.MockTransport(_handler)
        return original(*args, **kwargs)

    monkeypatch.setattr(client_metadata.httpx, "AsyncClient", _client)

    await client_metadata.fetch(DOCUMENT_URL, now=1000.0)
    await client_metadata.fetch(DOCUMENT_URL, now=1000.0 + 60)
    assert len(fetches) == 1, "the document was fetched again inside its lifetime"


async def test_a_remembered_document_stops_being_believed(public_dns, monkeypatch):
    """A client whose redirect URIs change — or whose document is taken over —
    must stop being believed without anybody restarting anything."""

    served = [_document()]
    original = httpx.AsyncClient

    def _client(*args, **kwargs):
        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=served[0])

        kwargs["transport"] = httpx.MockTransport(_handler)
        return original(*args, **kwargs)

    monkeypatch.setattr(client_metadata.httpx, "AsyncClient", _client)

    first = await client_metadata.fetch(DOCUMENT_URL, now=1000.0)
    assert first.allows("https://apps.example.test/callback")

    served[0] = _document(redirect_uris=["https://apps.example.test/moved"])
    later = await client_metadata.fetch(
        DOCUMENT_URL, now=1000.0 + client_metadata.CACHE_TTL_SECONDS + 1
    )
    assert not later.allows("https://apps.example.test/callback")
    assert later.allows("https://apps.example.test/moved")


# ── The authorization flow believes the document ─────────────────────────────


@pytest.fixture
def a_registered_client(public_dns, answering):
    """A client that identifies itself by document rather than by name."""

    answering(_document())
    return DOCUMENT_URL


def _sign_in(client):
    from web.auth import create_session
    from web.config import SESSION_COOKIE

    client.cookies.set(SESSION_COOKIE, create_session("tester"))


def _consent(client_id: str, redirect_uri: str) -> dict[str, str]:
    return {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
        "code_challenge_method": "S256",
    }


async def test_a_document_client_reaches_the_consent_screen(
    client, a_registered_client, legacy_owner_roots
):
    """The replacement for a pre-registered id, working end to end."""

    _sign_in(client)
    response = await client.get(
        "/oauth/authorize",
        params=_consent(a_registered_client, "https://apps.example.test/callback"),
    )
    assert response.status_code == 200
    # And the person deciding sees what the client calls itself, rather than a
    # URL they would have to parse in their head.
    assert "Kitchen Dashboard" in response.text


async def test_a_callback_outside_the_document_is_refused(client, a_registered_client):
    """The document names its redirect URIs in full; the allowlist named hosts.

    This is the case the old check could not make: same host, different path,
    and an authorization code delivered somewhere the client never declared.
    """

    _sign_in(client)
    response = await client.get(
        "/oauth/authorize",
        params=_consent(a_registered_client, "https://apps.example.test/elsewhere"),
    )
    assert response.status_code == 200
    assert "code=" not in response.text


async def test_approve_re_resolves_the_client_rather_than_trusting_the_form(
    client, a_registered_client
):
    """This endpoint is reachable on its own, and reads a form the caller wrote.

    A document checked while the consent page rendered proves nothing about the
    client id that arrives in the POST.
    """

    _sign_in(client)
    response = await client.post(
        "/oauth/authorize/approve",
        data={
            "client_id": a_registered_client,
            "redirect_uri": "https://apps.example.test/elsewhere",
            "code_challenge": "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
            "code_challenge_method": "S256",
        },
        follow_redirects=False,
    )
    assert response.status_code == 400


async def test_a_client_id_pointing_inside_the_network_is_refused_at_the_screen(
    client, monkeypatch
):
    """The SSRF refusal, reached the way an attacker would reach it.

    Nothing about the answer says whether the address was private, unreachable
    or simply wrong — a caller who could tell those apart could map the network
    this runs in, one client id at a time.
    """

    monkeypatch.setattr(
        client_metadata.socket,
        "getaddrinfo",
        lambda host, port, *a, **k: [(2, 1, 6, "", ("169.254.169.254", port))],
    )
    _sign_in(client)
    response = await client.get(
        "/oauth/authorize",
        params=_consent(
            "https://metadata.example.test/doc.json",
            "https://metadata.example.test/callback",
        ),
    )
    assert response.status_code == 200
    assert "code=" not in response.text


async def test_the_pre_registered_connector_still_works(client, legacy_owner_roots):
    """Claude.ai's connector uses a plain client id today.

    Breaking a working connection to adopt a newer identifier would be a change
    nobody asked for, so both shapes are accepted and the client id decides
    which path applies.
    """

    _sign_in(client)
    response = await client.get(
        "/oauth/authorize",
        params=_consent("vitals-claude-connector", "https://claude.ai/api/mcp/callback"),
    )
    assert response.status_code == 200


async def test_the_authorization_response_names_the_issuer(
    client, legacy_owner_roots
):
    """RFC 9207, which the MCP profile requires a client to validate.

    A client talking to several authorization servers cannot otherwise tell
    which one answered, and an attacker who can put a response in front of it
    relies on exactly that.
    """

    _sign_in(client)
    response = await client.post(
        "/oauth/authorize/approve",
        data={
            "client_id": "vitals-claude-connector",
            "redirect_uri": "https://claude.ai/api/mcp/callback",
            "code_challenge": "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
            "code_challenge_method": "S256",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert "iss=" in response.headers["location"]

    # And discovery advertises it, so a client knows to look.
    metadata = (await client.get("/.well-known/oauth-authorization-server")).json()
    assert metadata["authorization_response_iss_parameter_supported"] is True
    assert metadata["issuer"] in response.headers["location"].replace("%3A", ":").replace("%2F", "/")


# ── A metadata client can redeem what it was shown ──────────────────────────


async def _approve_metadata_client(client, client_id: str) -> str:
    response = await client.post(
        "/oauth/authorize/approve",
        data={
            "client_id": client_id,
            "redirect_uri": "https://apps.example.test/callback",
            "code_challenge": "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
            "code_challenge_method": "S256",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302
    return parse_qs(urlsplit(response.headers["location"]).query)["code"][0]


async def test_metadata_client_exchanges_without_a_static_secret_and_verifies_on_mcp(
    client,
    a_registered_client,
    legacy_owner_roots,
    redis,
    db_session,
    monkeypatch,
):
    """The URL client is public, but its grant is still fully bound.

    This crosses authorize, the one-use code, token persistence/signing and the
    SDK verifier seam.  A 200 from ``/oauth/token`` alone would miss the prior
    defect where the token was minted but the MCP verifier rejected every URL
    client because it only recognized the configured static identifier.
    """

    _sign_in(client)
    code = await _approve_metadata_client(client, a_registered_client)
    stored = json.loads(await redis.get(f"oauth_code:{code}"))
    assert stored["client_id"] == DOCUMENT_URL
    assert stored["redirect_uri"] == "https://apps.example.test/callback"
    assert stored["client_name"] == "Kitchen Dashboard"

    exchanged = await client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://apps.example.test/callback",
            "client_id": a_registered_client,
            "code_verifier": CODE_VERIFIER,
        },
    )
    assert exchanged.status_code == 200, exchanged.text
    access_token = exchanged.json()["access_token"]
    payload = _get_mcp_serializer().loads(access_token)
    assert payload["client_id"] == DOCUMENT_URL
    assert payload["health_subject"] == str(legacy_owner_roots.subject_id)

    persisted = await db_session.scalar(
        select(McpAccessToken).where(McpAccessToken.client_id == DOCUMENT_URL)
    )
    assert persisted is not None
    assert persisted.client_id == DOCUMENT_URL
    assert persisted.client_name == "Kitchen Dashboard"

    from web.routers import mcp as mcp_router
    from vitals.persistence import rls as rls_session

    class _VerifierSession:
        async def __aenter__(self):
            db_session.info.pop(rls_session._SUBJECT_KEY, None)
            return db_session

        async def __aexit__(self, *_args):
            return None

    class _VerifierFactory:
        def __call__(self):
            return _VerifierSession()

    await db_session.commit()
    monkeypatch.setattr(
        mcp_router,
        "get_session_factory",
        lambda: _VerifierFactory(),
    )
    granted = await mcp_router._ConnectorTokenVerifier().verify_token(access_token)
    assert granted is not None
    assert granted.client_id == DOCUMENT_URL
    assert granted.subject == "tester"


async def test_metadata_client_token_redirect_must_still_be_the_exact_document_uri(
    client,
    a_registered_client,
    legacy_owner_roots,
    redis,
):
    """Same host and an almost-identical path are not the registered callback."""

    _sign_in(client)
    code = await _approve_metadata_client(client, a_registered_client)
    exchanged = await client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://apps.example.test/callback/elsewhere",
            "client_id": a_registered_client,
            "code_verifier": CODE_VERIFIER,
        },
    )
    assert exchanged.status_code == 400
    assert exchanged.json()["error"] == "invalid_grant"
    # Rejection happened before consuming a valid grant, so the real client can
    # still redeem the code with the exact callback it originally supplied.
    assert await redis.get(f"oauth_code:{code}") is not None


async def test_static_client_cannot_exchange_a_metadata_clients_code(
    client,
    a_registered_client,
    legacy_owner_roots,
):
    """Knowing the installation secret grants no authority over another code."""

    _sign_in(client)
    code = await _approve_metadata_client(client, a_registered_client)
    exchanged = await client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://apps.example.test/callback",
            "client_id": "vitals-claude-connector",
            "client_secret": "test-mcp-secret",
            "code_verifier": CODE_VERIFIER,
        },
    )
    assert exchanged.status_code == 400
    assert exchanged.json() == {
        "error": "invalid_grant",
        "error_description": "Client ID mismatch",
    }


async def test_metadata_client_cannot_mix_public_and_basic_authentication(
    client,
    a_registered_client,
    legacy_owner_roots,
):
    """A request cannot borrow confidential credentials for a public client."""

    _sign_in(client)
    code = await _approve_metadata_client(client, a_registered_client)
    exchanged = await client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://apps.example.test/callback",
            "client_id": a_registered_client,
            "code_verifier": CODE_VERIFIER,
        },
        auth=(a_registered_client, "not-a-real-secret"),
    )
    assert exchanged.status_code == 400
    assert exchanged.json()["error"] == "invalid_client"


async def test_discovery_advertises_both_public_and_confidential_token_auth(client):
    metadata = (
        await client.get("/.well-known/oauth-authorization-server")
    ).json()
    assert metadata["client_id_metadata_document_supported"] is True
    methods = metadata["token_endpoint_auth_methods_supported"]
    assert methods == ["none", "client_secret_post", "client_secret_basic"]
