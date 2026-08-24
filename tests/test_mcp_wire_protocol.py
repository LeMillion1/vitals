"""The endpoint, driven by the SDK's own client.

Every other MCP test in this suite calls a decorated Python function. That is
worth doing and it proves nothing about the wire: a tool can be correct while
the transport in front of it negotiates the wrong protocol, demands a handshake
the specification removed, or treats a session id as authorization. PR-10 asks
for this explicitly — *run wire-level compliance tests through the official
Python SDK v2 client, not only by calling decorated Python functions.*

Over an ASGI transport rather than a TCP socket, which is a deliberate
narrowing. A server on a thread has an event loop of its own, the suite's engine
is bound to the test's, and asyncpg connections do not survive being used across
the two — the symptom was three tests passing every assertion and then erroring
at teardown inside *the fixture that closes the session*, a long way from where
the mistake was. What the socket was for is the protocol, and none of the
protocol is in the socket: the client below negotiates a version, frames
JSON-RPC, sends the real headers and reads the real responses. Only the kernel
is missing.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

pytest.importorskip("mcp.client.session")

import httpx2  # noqa: E402
import mcp.types as mcp_types  # noqa: E402
from mcp.client.session import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402

mcp_router = pytest.importorskip("web.routers.mcp")

pytestmark = pytest.mark.usefixtures("owned_by_legacy_subject")

#: Any absolute URL would do — the ASGI transport routes by path. The host has
#: to be one the endpoint's DNS-rebinding allowlist accepts, and ``test`` is what
#: the suite calls itself; see ``VITALS_PUBLIC_URL`` in tests/conftest.py.
BASE = "http://test/"


def _token(username: str | None = "tester") -> str:
    from web.auth import _get_mcp_serializer

    payload = {"client_id": "vitals-claude-connector", "type": "mcp_access_token"}
    if username is not None:
        payload["username"] = username
    return _get_mcp_serializer().dumps(payload)


@pytest.fixture
def endpoint(session_factory, monkeypatch):
    """The application and the lifespan a mount will not run for it."""

    monkeypatch.setattr(mcp_router, "get_session_factory", lambda: session_factory)
    return mcp_router.get_mcp_app()


@asynccontextmanager
async def _serving(bundle):
    """Run the endpoint's lifespan around one test's requests.

    The streamable-HTTP session manager is built inside that lifespan and an
    ASGI transport does not run one — the same thing ``web/main.py`` does by
    hand when it mounts this app. Without it every request answers "Task group
    is not initialized".

    Entered in the test rather than in a fixture: the lifespan holds an anyio
    task group, and a fixture that yields across one exits it from a different
    task than entered it, which anyio refuses.
    """

    app, lifespan = bundle
    async with lifespan(app):
        yield app


def _http(app, token: str | None = None) -> httpx2.AsyncClient:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), headers=headers
    )


def _connect(app, token: str | None = None):
    return streamable_http_client(BASE, http_client=_http(app, token))


async def test_the_endpoint_speaks_the_current_protocol(endpoint):
    """``2026-07-28``, and not by our assertion of it — by the client's.

    The version the SDK client negotiates is the one a connector will, so this
    is the difference between shipping the revision and claiming to.
    """

    assert mcp_types.LATEST_PROTOCOL_VERSION == "2026-07-28"

    async with _serving(endpoint) as app:
        async with _connect(app, _token()) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                listed = await session.list_tools()
    assert listed.tools, "the endpoint offered no tools at all"


async def test_tools_are_listed_without_any_handshake(endpoint):
    """No ``initialize``, no ``Mcp-Session-Id``, and it still answers.

    The removed handshake is the point of the stateless contract: a request
    carries what it needs, and nothing about having completed a handshake can be
    mistaken for having been authorized. The client below never calls
    ``initialize`` — the old transport refused exactly this with "Missing
    session ID".
    """

    async with _serving(endpoint) as app:
        async with _connect(app, _token()) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                listed = await session.list_tools()
    assert "get_weight_logs" in {tool.name for tool in listed.tools}


async def test_a_tool_call_crosses_the_wire_and_comes_back(
    endpoint, db_session, legacy_owner_roots
):
    """One real call, end to end, with a real payload.

    Listing proves the surface exists; calling proves the whole path — auth,
    dispatch, the subject seam, a service, and serialization back out.
    """

    from datetime import date

    from vitals.enums import Domain, Source
    from vitals.models.weight import WeightLog

    db_session.add(
        WeightLog(
            subject_id=legacy_owner_roots.subject_id,
            domain=Domain.WEIGHT.value,
            source=Source.MANUAL.value,
            date=date.today(),
            weight_kg=72.5,
        )
    )
    await db_session.commit()

    async with _serving(endpoint) as app:
        async with _connect(app, _token()) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                answer = await session.call_tool("get_weight_logs", {"limit": 5})

    assert not answer.is_error, answer.content
    body = str(answer.structured_content or answer.content)
    assert "72.5" in body, body


async def test_an_unauthenticated_client_is_refused_at_the_transport(endpoint):
    """Before any tool, and with a challenge a fresh client can follow."""

    async with _http(endpoint[0]) as http:
        response = await http.post(
            BASE,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            headers={"Accept": "application/json, text/event-stream"},
        )
    assert response.status_code == 401
    challenge = response.headers.get("www-authenticate", "")
    assert challenge.startswith("Bearer "), challenge
    assert "resource_metadata=" in challenge, (
        "a 401 with no pointer leaves a fresh client guessing where tokens come from"
    )


async def test_a_forged_token_is_refused_at_the_transport(endpoint):
    """The signature is checked before anything reads a record."""

    async with _http(endpoint[0], "not-a-real-token") as http:
        response = await http.post(
            BASE,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            headers={"Accept": "application/json, text/event-stream"},
        )
    assert response.status_code == 401


async def test_a_token_that_names_nobody_is_refused_at_the_transport(endpoint):
    """It cannot be attributed, so it cannot be listed or taken back.

    A credential nobody can revoke is what ``mcp_access_tokens`` exists to stop
    existing, and one that names no account cannot have a row. Nothing real is
    lost: ``/oauth/token`` has carried the authorizing account's name since the
    flow was written, so a token without one is not something this application
    ever issued.
    """

    async with _http(endpoint[0], _token(None)) as http:
        response = await http.post(
            BASE,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            headers={"Accept": "application/json, text/event-stream"},
        )
    assert response.status_code == 401


# ── Protocol conformance ─────────────────────────────────────────────────────


async def test_the_server_answers_discover_with_the_versions_it_speaks(endpoint):
    """``server/discover`` is mandatory in ``2026-07-28``.

    It is how a client learns what this server supports without a handshake,
    which is the whole shape of the stateless contract. Asked through the SDK
    client rather than by hand: a raw POST without the ``_meta`` envelope is
    treated as a legacy peer, for which the method does not exist — a
    distinction easy to mistake for a missing feature.
    """

    async with _serving(endpoint) as app:
        async with _connect(app, _token()) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                discovered = await session.discover()
                negotiated = session.protocol_version

    assert mcp_types.LATEST_PROTOCOL_VERSION in discovered.supported_versions
    assert negotiated == mcp_types.LATEST_PROTOCOL_VERSION
    assert discovered.capabilities.tools is not None, "no tools capability advertised"


async def test_a_result_says_what_it_is_and_who_produced_it(endpoint):
    """``resultType`` and the server identity in ``_meta``.

    Both are ``2026-07-28`` requirements and both are how a client tells one
    server's answer from another's when several are connected at once.

    ``discover`` first, because that is what a real client does and because the
    identity depends on it: before discovery the server has no reason to think
    its peer speaks the modern protocol, and it does not stamp an envelope the
    peer might not understand. Reaching for ``call_tool`` alone and finding no
    identity looks like a missing feature and is a missing step.
    """

    async with _serving(endpoint) as app:
        async with _connect(app, _token()) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.discover()
                answer = await session.call_tool("get_user_profile", {})

    assert answer.result_type, "the result does not say what kind it is"
    identity = (answer.meta or {}).get("io.modelcontextprotocol/serverInfo")
    assert identity, "the result does not say which server produced it"
    assert identity["name"] == "Vitals"


async def test_a_health_answer_is_never_cacheable_by_a_shared_cache(endpoint):
    """Every one of these answers is one person's medical record.

    The SDK defaults to ``cache_scope="private"`` and ``ttl_ms=0``, which is the
    right default and exactly why this is asserted: a default is a thing
    somebody can change, and the change that matters here would let an
    intermediary serve one patient's weight to the next caller.
    """

    async with _serving(endpoint) as app:
        async with _connect(app, _token()) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                answer = await session.call_tool("get_user_profile", {})
                listed = await session.list_tools()

    for result in (answer, listed):
        assert getattr(result, "cache_scope", "private") == "private", (
            f"{type(result).__name__} may be cached by a shared cache"
        )
        assert getattr(result, "ttl_ms", 0) == 0, (
            f"{type(result).__name__} may be held past the moment it was true"
        )


async def test_a_tool_description_is_what_it_does_not_why_it_is_written_so(
    endpoint,
):
    """The docstrings here record decisions; a model needs the first line.

    Everything after the summary is engineering history — which field moved out
    of ``.env`` and why, which refusal to expect. A model pays for every token
    of it on every listing and cannot act on any of it, and it is internal
    commentary handed to a third party.
    """

    async with _serving(endpoint) as app:
        async with _connect(app, _token()) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                listed = await session.list_tools()

    profile = next(t for t in listed.tools if t.name == "get_user_profile")
    assert profile.description
    assert "\n\n" not in profile.description, (
        "more than the summary is being sent: " + profile.description[:200]
    )
    assert ".env" not in profile.description, (
        "internal commentary reached the model: " + profile.description[:200]
    )

    # And the source still explains itself to the next person who reads it.
    from web.routers import mcp as module

    assert ".env" in (module.get_user_profile.__doc__ or ""), (
        "the docstring was trimmed instead of the description"
    )


async def test_every_listed_tool_has_a_schema_a_client_can_read(endpoint):
    """Deterministic listings with usable input schemas.

    A tool the model cannot call correctly is worse than one that is missing:
    it produces a malformed call, an error, and a retry.
    """

    async with _serving(endpoint) as app:
        async with _connect(app, _token()) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                first = await session.list_tools()
                second = await session.list_tools()

    assert [t.name for t in first.tools] == [t.name for t in second.tools], (
        "two listings of the same surface disagreed on order"
    )
    for tool in first.tools:
        assert tool.description, f"{tool.name} has no description"
        assert tool.input_schema.get("type") == "object", (
            f"{tool.name} has no object input schema"
        )
