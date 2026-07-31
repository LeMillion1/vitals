"""CSRF origin check + security headers (ported from Boxly's ``web/csrf.py``).

Session cookies are ``SameSite=lax`` (primary CSRF defence). This adds a second,
independent barrier: unsafe-method requests carrying a cross-origin ``Origin``
header are rejected. The CSP keeps ``'unsafe-eval'`` because Alpine compiles every
``x-*`` expression with ``Function()`` — without it the UI silently breaks.
"""
from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


async def _origin_check(request: Request, call_next):
    path = request.url.path
    # Server-to-server callers that authenticate with their own secret, not a
    # session cookie: MCP, the OAuth token exchange, and the Telegram webhook
    # (which a forged Origin header would otherwise 403 instead of ignore).
    if path.startswith("/mcp") or path.startswith("/tg/") or path == "/oauth/token":
        return await call_next(request)

    if request.method not in _SAFE_METHODS:
        origin = request.headers.get("origin")
        if origin:
            host = request.headers.get("host", "")
            if urlsplit(origin).netloc != host:
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
# (Inter / Outfit / Bricolage Grotesque — no monospace, per the design system)
# are self-hosted woff2 under web/static/fonts/, so font-src/style-src stay 'self'.
#
# form-action has to name every OAuth callback host, not merely 'self': the consent
# form posts to /oauth/authorize/approve and that response 302s onward to the
# client's callback, and Chrome applies form-action across the whole redirect
# chain. A host missing here swallows the "Approve" click with a console message
# naming the *form action* rather than the redirect target — which reads like a
# same-origin violation and sends you looking in the wrong place entirely. Built
# from the same allowlist /oauth/authorize checks so the two cannot drift apart.
_CSP_TEMPLATE = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://static.cloudflareinsights.com; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob: https:; "
    "font-src 'self'; "
    "connect-src 'self' https://cloudflareinsights.com; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self' {callbacks}"
)
_headers_cache: dict[str, str] | None = None


def security_headers() -> dict[str, str]:
    """The static security headers, assembled on first use rather than at import —
    ``get_web_config()`` raises without a session secret, and importing this module
    must not require a live environment.

    ponytail: cached for the process lifetime, so a changed callback allowlist needs
    a restart. Same as every other env-driven knob here; drop the cache if the hosts
    ever become editable at runtime.
    """
    global _headers_cache
    if _headers_cache is None:
        from web.config import get_web_config

        callbacks = " ".join(f"https://{h}" for h in get_web_config().mcp_redirect_hosts)
        _headers_cache = {
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": _CSP_TEMPLATE.format(callbacks=callbacks),
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "strict-origin-when-cross-origin",
        }
    return _headers_cache


async def _security_headers(request: Request, call_next):
    response = await call_next(request)
    for name, value in security_headers().items():
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
