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
import logging

import pytest

pytest.importorskip("mcp.client.session")

import httpx2  # noqa: E402
import mcp.types as mcp_types  # noqa: E402
from mcp.client.session import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402
from mcp.shared.exceptions import MCPError  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402

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


async def test_module_state_failure_is_fail_closed_on_the_wire(
    endpoint,
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    """The connector sees granted core schemas, never optional or wider ones."""
    from vitals.access import AccessScope, PolicyAction, PolicyResourceType
    from vitals.services.authentication import mcp_tokens
    from web.auth import _get_mcp_serializer
    from web.config import get_web_config

    async def unavailable(*_args, **_kwargs):
        raise RuntimeError("synthetic module-state outage")

    monkeypatch.setattr(
        mcp_router.modules_service,
        "get_enabled_modules",
        unavailable,
    )
    granted_scopes = frozenset(
        {
            AccessScope(
                PolicyResourceType.DOMAIN,
                "weight",
                PolicyAction.LIST,
            ),
            AccessScope(
                PolicyResourceType.DOMAIN,
                "nutrition",
                PolicyAction.LIST,
            ),
        }
    )
    config = get_web_config()
    payload, _record = await mcp_tokens.issue(
        db_session,
        username=config.auth_username,
        client_id=config.mcp_client_id,
        audience=mcp_tokens.audience_for(config.public_url),
        issuer=config.public_url,
        subject_id=legacy_owner_roots.subject_id,
        scopes=granted_scopes,
    )
    await db_session.commit()
    token = _get_mcp_serializer().dumps(payload)

    async with _serving(endpoint) as app:
        async with _connect(app, token) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                listed = await session.list_tools()

    names = {tool.name for tool in listed.tools}
    assert "get_weight_logs" in names
    assert "get_lab_results" not in names
    assert "log_weight" not in names
    assert "get_nutrition_summary" not in names
    assert "search_meals" not in names


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


async def test_a_tool_failure_returns_a_safe_visible_diagnostic(
    endpoint, monkeypatch, caplog
):
    """A connector must receive a reason even if it hides MCP error results."""

    secret = "PHI_SENTINEL token=SECRET_SENTINEL"

    async def fail_tool(name, arguments, context, convert_result=False):
        try:
            raise ConnectionError(secret)
        except ConnectionError as exc:
            raise ToolError(f"Error executing tool {name}: {secret}") from exc

    monkeypatch.setattr(mcp_router.mcp._tool_manager, "call_tool", fail_tool)
    caplog.set_level(logging.ERROR, logger=mcp_router.__name__)

    async with _serving(endpoint) as app:
        async with _connect(app, _token()) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                answer = await session.call_tool("get_weight_logs", {"limit": 5})

    assert not answer.is_error, "the client would replace this with generic copy"
    payload = answer.structured_content
    assert payload == {
        "error": "A required service is temporarily unavailable.",
        "code": "dependency_unavailable",
        "error_id": payload["error_id"],
        "retryable": False,
    }
    assert len(payload["error_id"]) == 32
    assert payload["error_id"] in caplog.text
    assert "exception=ConnectionError" in caplog.text
    assert "location=test_mcp_wire_protocol.py:" in caplog.text
    assert secret not in str(answer.content)
    assert secret not in caplog.text


async def test_a_visible_failure_respects_the_tool_output_schema(
    endpoint, monkeypatch
):
    """Structured diagnostics stay valid for list-returning tools."""

    async def fail_tool(name, arguments, context, convert_result=False):
        try:
            raise mcp_router.McpArgumentError(
                "from_date must be a YYYY-MM-DD date"
            )
        except mcp_router.McpArgumentError as exc:
            raise ToolError(f"Error executing tool {name}") from exc

    monkeypatch.setattr(mcp_router.mcp._tool_manager, "call_tool", fail_tool)

    async with _serving(endpoint) as app:
        async with _connect(app, _token()) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                answer = await session.call_tool("get_lab_results", {"limit": 5})

    assert not answer.is_error
    wrapped = answer.structured_content
    payload = wrapped["result"][0]
    assert payload["code"] == "invalid_request"
    assert payload["error"] == "from_date must be a YYYY-MM-DD date"
    assert payload["retryable"] is False
    tool = mcp_router.mcp._tool_manager.get_tool("get_lab_results")
    tool.fn_metadata.output_model.model_validate(wrapped)


async def test_an_internal_tool_failure_is_redacted(endpoint, monkeypatch, caplog):
    """Unknown exceptions expose a reference, never their arbitrary text."""

    secret = "postgresql://operator:SECRET@db patient_weight=72.5"

    async def fail_tool(name, arguments, context, convert_result=False):
        try:
            raise RuntimeError(secret)
        except RuntimeError as exc:
            raise ToolError(f"Error executing tool {name}: {secret}") from exc

    monkeypatch.setattr(mcp_router.mcp._tool_manager, "call_tool", fail_tool)
    caplog.set_level(logging.ERROR, logger=mcp_router.__name__)

    async with _serving(endpoint) as app:
        async with _connect(app, _token()) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                answer = await session.call_tool("get_weight_logs", {"limit": 5})

    assert not answer.is_error
    payload = answer.structured_content
    assert payload["code"] == "internal_error"
    assert payload["error"] == "The tool failed unexpectedly."
    assert payload["retryable"] is False
    assert payload["error_id"] in caplog.text
    assert "exception=RuntimeError" in caplog.text
    assert secret not in str(answer.content)
    assert secret not in caplog.text


async def test_a_nested_database_failure_keeps_its_database_category(
    endpoint, monkeypatch, caplog
):
    """A driver cause must not hide the enclosing SQLAlchemy failure."""

    secret = "SELECT patient_weight FROM health_data WHERE token='SECRET'"

    class Diagnostic:
        constraint_name = "uq_weight_subject_date"

    class DriverFailure(RuntimeError):
        sqlstate = "40001"
        diag = Diagnostic()

    async def fail_tool(name, arguments, context, convert_result=False):
        try:
            raise DriverFailure(secret)
        except DriverFailure as driver:
            try:
                raise OperationalError(secret, {"token": "SECRET"}, driver) from driver
            except OperationalError as database:
                raise ToolError("database operation failed") from database

    monkeypatch.setattr(mcp_router.mcp._tool_manager, "call_tool", fail_tool)
    caplog.set_level(logging.ERROR, logger=mcp_router.__name__)

    async with _serving(endpoint) as app:
        async with _connect(app, _token()) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                answer = await session.call_tool("get_weight_logs", {"limit": 5})

    payload = answer.structured_content
    assert payload["code"] == "database_error"
    assert payload["retryable"] is False
    assert "exception=OperationalError" in caplog.text
    assert "sqlstate=40001" in caplog.text
    assert "constraint=uq_weight_subject_date" in caplog.text
    assert secret not in str(answer.content)
    assert secret not in caplog.text


async def test_an_unknown_unscoped_tool_stays_an_mcp_error():
    """The diagnostic wrapper must not make an absent tool look successful."""

    with pytest.raises(ToolError, match="Unknown tool"):
        await mcp_router.mcp.call_tool("tool_that_does_not_exist", {})


async def test_an_unresolvable_grant_returns_a_visible_access_reason(
    endpoint, monkeypatch
):
    """Authorization resolution failures must not fall back to generic copy."""

    def fail_binding():
        raise mcp_router.McpActorUnresolved("unsafe identity detail")

    monkeypatch.setattr(mcp_router, "_current_grant_binding", fail_binding)

    async with _serving(endpoint) as app:
        async with _connect(app, _token()) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                answer = await session.call_tool("get_weight_logs", {"limit": 5})

    assert not answer.is_error
    payload = answer.structured_content
    assert payload["code"] == "access_denied"
    assert payload["error"] == "The connector is not authorized for this operation."
    assert "unsafe identity detail" not in str(answer.content)


async def test_a_protocol_error_keeps_the_standard_mcp_channel(
    endpoint, monkeypatch
):
    """Only execution failures use the visible Vitals error contract."""

    async def fail_tool(name, arguments, context, convert_result=False):
        raise MCPError(-32001, "synthetic protocol failure")

    monkeypatch.setattr(mcp_router.mcp._tool_manager, "call_tool", fail_tool)

    async with _serving(endpoint) as app:
        async with _connect(app, _token()) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                with pytest.raises(MCPError, match="synthetic protocol failure"):
                    await session.call_tool("get_weight_logs", {"limit": 5})


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
