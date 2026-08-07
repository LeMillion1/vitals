"""``/r/<token>`` — the only anonymous route in Vitals.

Everything else in this app sits behind ``require_auth``. This one is reached by
a person who has no account and never will: a doctor holding a link and a
password. That asymmetry is the whole reason this router is a file of its own
rather than three endpoints bolted onto ``share.py`` — the rules here are
different and have to be readable in one place.

  * The link is **not** single-use. Messenger link previews fetch a URL before a
    human ever taps it, so a burn-on-read link arrives already burnt.
  * A missing, revoked, expired and purged token all produce one identical page.
    "This report was revoked" is itself information about the owner.
  * The password step is throttled with **one global counter**, not per IP:
    rotating the source address must not buy a fresh allowance. Same reasoning as
    the 2FA code step.
  * The document's own CSP is far tighter than the app's. There is no Alpine, no
    HTMX and no script of any kind on these pages, so ``'unsafe-eval'`` and
    ``'unsafe-inline'`` scripts — which the main CSP needs — are refused here.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Form, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.i18n import current_lang
from vitals.services import share_service
from vitals.services.share_chart import weight_svg
from web.config import get_web_config
from web.deps import get_session
from web.ratelimit import login_rate_limit
from web.security import verify_password, verify_password_dummy
from web.templating import templates

router = APIRouter(prefix="/r", tags=["public-report"])

# One cookie, holding the id of the report whose password was entered. Signed
# with a salt of its own so it is worthless anywhere a session is expected —
# the same construction as the MCP and pending-2FA tokens.
REPORT_COOKIE = "vitals_report"
REPORT_TTL = 6 * 3600  # long enough for an appointment, short enough to expire

# CSP for a page with no scripts on it at all. Deliberately not derived from the
# app's: this one is allowed to be strict precisely because the document is
# static markup, and it must not inherit 'unsafe-eval' the day Alpine needs it.
_DOC_CSP = (
    "default-src 'none'; "
    "style-src 'unsafe-inline'; "
    "img-src data:; "
    "form-action 'self'; "
    "base-uri 'none'; "
    "frame-ancestors 'none'"
)


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_web_config().session_secret, salt="vitals-report")


def has_access(request: Request, report_id: int) -> bool:
    """Whether this browser already unlocked **this** report.

    The id is inside the signature, so a cookie earned on report A does not open
    report B — which is the difference between one shared secret and one per
    document.
    """
    token = request.cookies.get(REPORT_COOKIE)
    if not token:
        return False
    try:
        payload = _serializer().loads(token, max_age=REPORT_TTL)
    except (SignatureExpired, BadSignature):
        return False
    return payload == report_id


def grant_access(response: Response, report_id: int) -> None:
    cfg = get_web_config()
    response.set_cookie(
        key=REPORT_COOKIE,
        value=_serializer().dumps(report_id),
        max_age=REPORT_TTL,
        path="/r",
        secure=cfg.cookie_secure,
        httponly=True,
        samesite=cfg.cookie_samesite,
    )


def harden(response: Response) -> Response:
    """Headers every public response carries, document or not."""
    response.headers["Content-Security-Policy"] = _DOC_CSP
    response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    return response


def render_document(request: Request, row, *, download: bool = False) -> str:
    """The document as a standalone HTML string.

    Shared with the owner's download route so the file on disk and the page at
    the link are produced by one template and cannot drift apart.
    """
    snapshot = row.snapshot or {}
    # The language was decided when the snapshot was frozen; the reader's browser
    # and the owner's current UI setting have no say over a document that was
    # already handed to somebody.
    current_lang.set(snapshot.get("lang") or current_lang.get())
    return templates.get_template("share/public_report.html").render(
        request=request,
        report=row,
        snapshot=snapshot,
        blocks=snapshot.get("blocks") or {},
        download=download,
        weight_svg=weight_svg,
    )


def _gone(request: Request) -> Response:
    """The one page an unknown, revoked, expired or purged token gets."""
    response = templates.TemplateResponse(
        request, "share/gone.html", {}, status_code=status.HTTP_404_NOT_FOUND
    )
    return harden(response)


def _password_form(request: Request, error: Optional[str] = None) -> Response:
    """Deliberately carries nothing about the report — not its title, not its
    period, not a single number. Everything on this page is visible to whoever
    has the URL, which at minimum includes every link-preview bot it passed."""
    response = templates.TemplateResponse(
        request,
        "share/password.html",
        {"error": error},
        status_code=status.HTTP_401_UNAUTHORIZED if error else status.HTTP_200_OK,
    )
    return harden(response)


@router.get("/{token}", response_class=HTMLResponse)
async def open_report(
    request: Request,
    token: str,
    db: AsyncSession = Depends(get_session),
):
    row = await share_service.resolve_public(db, token)
    if row is None:
        return _gone(request)
    if not has_access(request, row.id):
        return _password_form(request)
    return harden(HTMLResponse(render_document(request, row)))


@router.post("/{token}", response_class=HTMLResponse)
async def unlock_report(
    request: Request,
    token: str,
    password: str = Form(...),
    db: AsyncSession = Depends(get_session),
    # Global, not per-IP: a shared password is guessable in bulk and a rotated
    # address must not hand out a fresh budget. Vitals has one owner, so a global
    # ceiling costs legitimate traffic nothing.
    _rl: None = Depends(
        login_rate_limit(limit=20, window=300, bucket="report_pw", per_ip=False)
    ),
):
    row = await share_service.resolve_public(db, token)
    if row is None:
        # Still run one hash so a dead token and a live one with a wrong password
        # take the same wall-clock time.
        verify_password_dummy(password)
        return _gone(request)
    if not verify_password(password, row.password_hash):
        return _password_form(request, error=True)

    await share_service.register_open(db, row)
    await db.commit()
    response = RedirectResponse(
        url=f"/r/{token}", status_code=status.HTTP_303_SEE_OTHER
    )
    grant_access(response, row.id)
    return harden(response)
