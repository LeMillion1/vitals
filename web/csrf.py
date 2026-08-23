"""Cross-site request forgery defences, and the security headers.

Three barriers, deliberately independent, because each has a gap the next one
covers.

``SameSite=lax`` on the session cookie is the first: a cross-site POST simply
arrives without credentials. It is also the one a misconfigured reverse proxy or
an old browser can quietly undo.

The ``Origin`` check is the second, and its gap is the reason for the third: a
request that carries *no* ``Origin`` header passes it. Some clients omit the
header, and "absent" cannot be distinguished from "same-origin" without more
information.

Fetch Metadata is that information. Every current browser sends
``Sec-Fetch-Site`` on every request, and it says directly where the request came
from rather than leaving it to be inferred. A cross-site request that mutates is
refused whatever its ``Origin`` says, and a request that carries neither header
is a non-browser client, which the exemptions below already account for.

The CSP keeps ``'unsafe-eval'`` because Alpine compiles every ``x-*`` expression
with ``Function()`` — without it the UI silently breaks.
"""
from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

#: Where a request may come from and still be allowed to change something.
#: ``same-origin`` is this page talking to itself. ``same-site`` covers a
#: subdomain, which a self-hosted deployment may legitimately use.
_ALLOWED_FETCH_SITES = frozenset({"same-origin", "same-site"})

#: Paths whose callers authenticate with their own secret rather than a session
#: cookie, so a forged cross-site request carries nothing worth forging: MCP and
#: the OAuth token exchange, which a forged ``Origin`` header would otherwise 403
#: instead of ignore.
#:
#: ``/tg/`` — the Telegram webhook — used to be here. It is gone, and so is the
#: exemption: a prefix that exempts a route nobody has mounted is an open door
#: waiting for the next thing to be mounted behind it.
def _is_exempt(path: str) -> bool:
    return path.startswith("/mcp") or path == "/oauth/token"


async def _origin_check(request: Request, call_next):
    if _is_exempt(request.url.path) or request.method in _SAFE_METHODS:
        return await call_next(request)

    # Fetch Metadata first: it is present on every request from a current
    # browser and says where the request came from rather than leaving it to be
    # inferred from a header that may be absent.
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site is not None and fetch_site not in _ALLOWED_FETCH_SITES:
        # ``none`` means the user typed the address or opened a bookmark, which
        # no browser does for a mutating method — so it is as unexpected here as
        # ``cross-site``.
        return PlainTextResponse("Cross-site request refused.", status_code=403)

    origin = request.headers.get("origin")
    if origin and urlsplit(origin).netloc != request.headers.get("host", ""):
        return PlainTextResponse("Origin not allowed.", status_code=403)

    return await call_next(request)



def add_csrf_origin_check(app: FastAPI) -> None:
    app.middleware("http")(_origin_check)


# 'unsafe-eval' is REQUIRED by Alpine.js (Function() compilation of x-* directives).
# 'unsafe-inline' covers inline <script> + Alpine/HTMX inline attributes. img-src
# data:/blob: cover Chart.js canvases and inline SVG icons.
# Scripts and fonts are vendored under /static (no CDN) — the one exception is
# Cloudflare's Web Analytics beacon, which Cloudflare injects at the edge (into
# the proxied HTML response) rather than anything our own templates load, so
# there's no template reference to point at; it needs its own
# script-src/connect-src entries or the browser blocks it outright. Fonts
# (Inter / Bricolage Grotesque — no monospace, per the design system)
# are self-hosted woff2 under web/static/fonts/, so font-src/style-src stay 'self'.
#
# form-action allows any https target, not just 'self': the consent form posts to
# /oauth/authorize/approve, that response 302s to the connector's callback, and the
# connector bounces on through hosts of its own — Chrome enforces form-action across
# the entire redirect chain, and a chain inside somebody else's product can't be
# enumerated here. Failure mode when it is too narrow: the "Approve" click does
# nothing, and the console names the *form action* instead of the blocked hop, which
# reads like a same-origin violation and sends you looking in the wrong place. The
# real gate on where an approval may land is redirect_allowed() in the OAuth router;
# with 'unsafe-inline' scripts permitted above, a stricter form-action buys nothing.
_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://static.cloudflareinsights.com; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob: https:; "
    "font-src 'self'; "
    "connect-src 'self' https://cloudflareinsights.com; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self' https:"
)
_SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": _CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
}


async def _security_headers(request: Request, call_next):
    response = await call_next(request)
    for name, value in _SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)

    content_type = response.headers.get("content-type", "").lower()

    # Disable browser caching for HTML documents
    if "text/html" in content_type:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"

    # For JS/CSS: no-cache forces the browser to revalidate via ETag/Last-Modified
    # on every load, so updated files are never served stale from browser cache.
    elif "javascript" in content_type or "text/css" in content_type:
        response.headers.setdefault("Cache-Control", "no-cache")

    return response


def add_security_headers(app: FastAPI) -> None:
    app.middleware("http")(_security_headers)
