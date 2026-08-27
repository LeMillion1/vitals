"""Stateless streamable-HTTP transport construction for MCP."""

from __future__ import annotations

from urllib.parse import urlparse

from mcp.server.transport_security import TransportSecuritySettings


def build_transport(server, *, public_url: str) -> tuple[object, object]:
    """Build the MCP ASGI app and the lifespan a parent mount must enter."""

    public = urlparse(public_url)
    hosts = {public.netloc}
    hosts.update(
        {
            "127.0.0.1:*",
            "127.0.0.1",
            "[::1]:*",
            "localhost:8000",
            "localhost:8010",
            "localhost",
        }
    )
    hosts.discard("")

    app = server.streamable_http_app(
        streamable_http_path="/",
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            allowed_hosts=sorted(hosts),
            allowed_origins=sorted(
                f"{scheme}://{host}"
                for scheme in ("http", "https")
                for host in hosts
            ),
        ),
    )
    return app, app.router.lifespan_context


__all__ = ["build_transport"]
