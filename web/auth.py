"""Compatibility facade for browser authentication.

New code should import credential primitives from
``web.authentication.tokens`` and route-specific behavior from the matching
delivery module. The facade keeps the established imports stable for callers.
"""

from web.authentication.federated import (
    federated_login_callback,
    federated_login_start,
    logout,
    registration_request_status,
)
from web.authentication.routes import router
from web.authentication.legacy import (
    authenticate,
    login,
    login_2fa,
    login_2fa_page,
    login_page,
)
from web.authentication.tokens import (
    SessionClaims,
    _get_mcp_serializer,
    _get_oidc_handoff_serializer,
    _get_pending_2fa_serializer,
    _get_serializer,
    clear_oidc_handoff_cookie,
    clear_pending_2fa_cookie,
    clear_session_cookie,
    create_federated_session,
    create_oidc_handoff,
    create_pending_2fa,
    create_session,
    decode_session,
    read_oidc_handoff,
    read_pending_2fa,
    read_session,
    safe_next,
    session_allowed_in_current_auth_mode,
    session_issued_at,
    set_oidc_handoff_cookie,
    set_pending_2fa_cookie,
    set_session_cookie,
)

__all__ = [
    "SessionClaims",
    "_get_mcp_serializer",
    "_get_oidc_handoff_serializer",
    "_get_pending_2fa_serializer",
    "_get_serializer",
    "authenticate",
    "clear_oidc_handoff_cookie",
    "clear_pending_2fa_cookie",
    "clear_session_cookie",
    "create_federated_session",
    "create_oidc_handoff",
    "create_pending_2fa",
    "create_session",
    "decode_session",
    "federated_login_callback",
    "federated_login_start",
    "login",
    "login_2fa",
    "login_2fa_page",
    "login_page",
    "logout",
    "read_oidc_handoff",
    "read_pending_2fa",
    "read_session",
    "registration_request_status",
    "router",
    "safe_next",
    "session_allowed_in_current_auth_mode",
    "session_issued_at",
    "set_oidc_handoff_cookie",
    "set_pending_2fa_cookie",
    "set_session_cookie",
]
