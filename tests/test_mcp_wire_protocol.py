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


async def test_a_token_that_names_nobody_still_connects(endpoint):
    """An old connector keeps working on a single-subject installation.

    Breaking every issued token on upgrade would be its own defect. What such a
    token cannot do is name a record, which is why it is refused once there is
    more than one — proved next door in ``test_mcp_actor_identity.py``.
    """

    async with _serving(endpoint) as app:
        async with _connect(app, _token(None)) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                listed = await session.list_tools()
    assert listed.tools
