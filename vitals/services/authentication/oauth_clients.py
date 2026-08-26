"""Client ID Metadata Documents, fetched as though the URL were an attack.

The MCP authorization profile lets a client identify itself by an HTTPS URL
instead of by a pre-registered id: the URL *is* the ``client_id``, and it
resolves to a JSON document declaring the client's name and its redirect URIs.
It replaces Dynamic Client Registration, which the profile deprecates, and it
replaces the hand-kept host allowlist this installation used before — a list
somebody had to edit for every new connector, and one that could only ever be
as precise as a hostname.

**The URL comes from whoever is authorizing.** That makes this a server-side
request the caller chooses the target of, which is the definition of SSRF, and
the target is reachable from inside whatever network this installation runs in.
So every one of the following is load-bearing rather than defensive style:

* HTTPS only. A ``file://`` or ``http://`` client id is not a client id.
* The destination must be a public address, checked after DNS resolution rather
  than by inspecting the hostname — ``internal.example.com`` resolving to
  ``10.0.0.5`` is the whole trick, and a name tells you nothing about it.
* No redirects at all. Following one means validating a second destination, and
  a redirect from a public host to a private one is the second half of the same
  trick. The document lives at its client id or it does not exist.
* Bounded time and bounded body, so a slow or endless response cannot hold a
  request worker or fill memory.
* The document's own ``client_id`` must equal the URL it was fetched from,
  exactly. Without that check a client could name any document as its identity
  and inherit its redirect URIs.

Cached, because an authorization flow fetches this on every attempt and a
connector reconnects often. Cached with a bounded lifetime, because a client
that changes its redirect URIs — or is taken over — must stop being believed
without a restart.
"""
from __future__ import annotations

import ipaddress
import socket
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx

#: Long enough for a healthy CDN, short enough that a hanging endpoint cannot
#: hold a worker while somebody waits at a consent screen.
FETCH_TIMEOUT = httpx.Timeout(5.0, connect=3.0)

#: A client metadata document is a handful of fields. Anything larger is either
#: a mistake or an attempt to make this process read something it should not.
MAX_DOCUMENT_BYTES = 64 * 1024

#: How long a fetched document is believed. A client whose redirect URIs change
#: — or whose document is taken over — stops being believed within this, with
#: nothing to restart.
CACHE_TTL_SECONDS = 300


class ClientMetadataError(Exception):
    """The document is absent, unreachable, or not what it claims to be."""


class UnsafeMetadataUrl(ClientMetadataError):
    """The client id points somewhere this server must not fetch from."""


@dataclass(frozen=True, slots=True)
class ClientMetadata:
    """What a client says about itself, after it has been checked."""

    client_id: str
    redirect_uris: tuple[str, ...]
    client_name: str

    def allows(self, redirect_uri: str) -> bool:
        """Whether this exact callback is one the document declares.

        Exact string comparison, not a host or prefix match. The document is
        specific by construction — the client wrote it — so there is no reason
        to be lenient about it, and prefix matching on redirect URIs is a
        well-worn way to lose an authorization code to an open redirect.
        """

        return redirect_uri in self.redirect_uris


@dataclass
class _CacheEntry:
    metadata: ClientMetadata
    fetched_at: float = field(default_factory=time.monotonic)


_CACHE: dict[str, _CacheEntry] = {}


def looks_like_a_metadata_url(client_id: str) -> bool:
    """Whether this client id is a URL rather than a pre-registered name.

    The distinction the profile draws, and the one that decides which path an
    authorization takes. Deliberately narrow: the HTTPS URL shape required by
    the profile, including a path and excluding userinfo, fragments and dot
    segments. Anything else is treated as a plain identifier and matched
    against the configured client, so a malformed URL cannot fall through into
    the fetching path by accident.
    """

    if not client_id or len(client_id) > 255:
        return False
    try:
        parts = urlsplit(client_id)
        hostname = parts.hostname
        username = parts.username
        password = parts.password
    except ValueError:
        return False
    path_segments = (unquote(segment) for segment in parts.path.split("/"))
    return bool(
        parts.scheme == "https"
        and parts.netloc
        and hostname
        and username is None
        and password is None
        and parts.path
        and not parts.fragment
        and all(segment not in {".", ".."} for segment in path_segments)
    )


def _require_public_destination(host: str) -> None:
    """Refuse anything that resolves to an address inside this network.

    Resolved rather than parsed. A hostname says nothing about where it points,
    and a name whose A record is ``169.254.169.254`` or ``10.0.0.5`` is exactly
    the request an attacker wants this server to make on their behalf — cloud
    metadata endpoints and internal services live at those addresses.

    Every address the name resolves to is checked, not the first: a name with
    one public and one private record would otherwise pass here and connect to
    whichever the socket layer chose.
    """

    try:
        resolved = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UnsafeMetadataUrl(f"{host} does not resolve") from exc

    if not resolved:
        raise UnsafeMetadataUrl(f"{host} does not resolve")

    for entry in resolved:
        address = ipaddress.ip_address(entry[4][0])
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
        ):
            raise UnsafeMetadataUrl(
                f"{host} resolves to {address}, which is not a public address"
            )


def _validated(document: Any, client_id: str) -> ClientMetadata:
    """Turn a decoded document into metadata, or refuse it."""

    if not isinstance(document, dict):
        raise ClientMetadataError("the client metadata document is not an object")

    declared = document.get("client_id")
    if declared != client_id:
        # Without this a client could present somebody else's document as its
        # own identity and inherit their redirect URIs.
        raise ClientMetadataError(
            "the document's client_id does not match the URL it was fetched from"
        )

    raw_uris = document.get("redirect_uris")
    if not isinstance(raw_uris, list) or not raw_uris:
        raise ClientMetadataError("the document declares no redirect_uris")

    uris: list[str] = []
    for uri in raw_uris:
        if not isinstance(uri, str):
            raise ClientMetadataError("a redirect_uri is not a string")
        parts = urlsplit(uri)
        if parts.scheme != "https" or not parts.netloc:
            # ``http`` and custom schemes are where an authorization code goes
            # to be intercepted. A public client that needs a loopback callback
            # is a different profile from this one.
            raise ClientMetadataError(f"redirect_uri {uri!r} is not https")
        if parts.fragment:
            raise ClientMetadataError(f"redirect_uri {uri!r} carries a fragment")
        uris.append(uri)

    name = document.get("client_name")
    if not isinstance(name, str) or not name.strip() or len(name) > 255:
        raise ClientMetadataError("the document declares no usable client_name")

    # Vitals implements the MCP public-client profile, not private_key_jwt or
    # another asymmetric client authentication mechanism.  Accepting a missing
    # or different value and then treating it as public at the token endpoint
    # would silently weaken what the document asked for.
    if document.get("token_endpoint_auth_method") != "none":
        raise ClientMetadataError(
            "the document does not declare public token authentication"
        )
    if "client_secret" in document or "client_secret_expires_at" in document:
        raise ClientMetadataError("a metadata document must not carry a secret")

    return ClientMetadata(
        client_id=client_id,
        redirect_uris=tuple(uris),
        client_name=name,
    )


async def fetch(client_id: str, *, now: float | None = None) -> ClientMetadata:
    """Resolve a client id URL to the metadata it declares.

    Raises :class:`ClientMetadataError` for anything that is not a document this
    server is willing to believe — which includes every failure mode, because a
    caller deciding what to do about a timeout differently from a malformed body
    is a caller with two ways to be wrong.
    """

    if not looks_like_a_metadata_url(client_id):
        raise UnsafeMetadataUrl("a client metadata document is an https URL")

    stamp = time.monotonic() if now is None else now
    cached = _CACHE.get(client_id)
    if cached is not None and stamp - cached.fetched_at < CACHE_TTL_SECONDS:
        return cached.metadata

    parts = urlsplit(client_id)
    _require_public_destination(parts.hostname or "")

    try:
        async with httpx.AsyncClient(
            timeout=FETCH_TIMEOUT,
            # Not one redirect. Following one means checking a second
            # destination against everything above, and a redirect from a
            # public host to a private one is the second half of the trick this
            # module exists to refuse.
            follow_redirects=False,
        ) as client:
            response = await client.get(
                client_id, headers={"Accept": "application/json"}
            )
    except httpx.HTTPError as exc:
        raise ClientMetadataError(f"the client metadata document is unreachable: {exc}") from exc

    if response.status_code != 200:
        raise ClientMetadataError(
            f"the client metadata document answered {response.status_code}"
        )
    if len(response.content) > MAX_DOCUMENT_BYTES:
        raise ClientMetadataError("the client metadata document is too large")

    try:
        document = response.json()
    except ValueError as exc:
        raise ClientMetadataError("the client metadata document is not JSON") from exc

    metadata = _validated(document, client_id)
    _CACHE[client_id] = _CacheEntry(metadata=metadata, fetched_at=stamp)
    return metadata


def forget(client_id: str | None = None) -> None:
    """Drop a cached document, or all of them. For tests and for an operator."""

    if client_id is None:
        _CACHE.clear()
    else:
        _CACHE.pop(client_id, None)


__all__ = [
    "CACHE_TTL_SECONDS",
    "ClientMetadata",
    "ClientMetadataError",
    "MAX_DOCUMENT_BYTES",
    "UnsafeMetadataUrl",
    "fetch",
    "forget",
    "looks_like_a_metadata_url",
]
