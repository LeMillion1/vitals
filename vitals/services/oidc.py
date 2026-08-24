"""The OpenID Connect boundary: everything Vitals must verify about a login.

Vitals stops authenticating anybody. A provider does that, and hands back an ID
token. This module's entire job is to decide whether that token is one Vitals
asked for, from the issuer it asked, about the person it asked about — and to
refuse otherwise. Nothing downstream re-checks any of it, so every check that
belongs here has to be here.

The checks, and what each one stops:

``issuer``      Compared verbatim against the configured issuer, in the
                discovery document, in the token, and in the authorization
                response. An attacker who controls one provider in a
                multi-provider deployment otherwise redeems a code at another —
                the OAuth mix-up attack, which is why RFC 9207 added the
                response ``iss`` in the first place.
``audience``    The token must be addressed to this client. A token issued for
                a different client of the same provider is a valid token and
                not a valid login here.
``nonce``       Binds the token to the authorization request this browser
                started, so a token replayed from elsewhere does not match.
``state``       Binds the callback to that same request, which is what stops
                login CSRF: an attacker cannot make somebody's browser complete
                *their* login and silently land in the attacker's account.
``PKCE``        Binds the code to the client that requested it. Without it, a
                code intercepted in the redirect is redeemable by whoever has
                it.
``times``       ``exp``/``iat``/``nbf`` with a small skew allowance, and
                ``auth_time`` when freshness was demanded.
``signature``   Verified against the provider's JWKS, refetched when a key id
                is unknown so a routine rotation is not an outage.

What is deliberately *not* an identity key: email, and display name. Both arrive
in the token and both are things a provider may let a person change. Matching on
either hands one account to whoever claims the address next, which is the classic
federated-login takeover rather than a hypothetical.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

import httpx
import jwt

#: Providers legitimately disagree with us about the clock by a second or two.
#: Wider than this stops being tolerance and starts being a replay window.
CLOCK_SKEW_SECONDS = 60

#: A login is a person waiting at a redirect, not a background job.
_HTTP_TIMEOUT = httpx.Timeout(10.0, connect=5.0)

#: Discovery is stable; refetching it per login is a request nobody needs.
_DISCOVERY_TTL_SECONDS = 3600


class OidcError(RuntimeError):
    """A login could not be completed. The message is for the log, not the user."""


class OidcConfigurationError(OidcError):
    """The deployment's OIDC settings are missing or inconsistent."""


class OidcDiscoveryError(OidcError):
    """The provider's metadata could not be fetched or does not describe it."""


class OidcProtocolError(OidcError):
    """The provider's response is not one this request can accept."""


class OidcTokenError(OidcError):
    """The ID token failed a check that a genuine token would pass."""


@dataclass(frozen=True, slots=True)
class OidcSettings:
    """What this deployment was told about its provider."""

    issuer: str
    client_id: str
    client_secret: str
    redirect_url: str
    #: Scopes beyond ``openid``. ``email`` is requested for display only.
    scopes: tuple[str, ...] = ("openid", "profile", "email")

    def __post_init__(self) -> None:
        for field_name in ("issuer", "client_id", "redirect_url"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise OidcConfigurationError(f"{field_name} must be a non-empty string")
        if not self.issuer.startswith("https://") and not self.issuer.startswith(
            "http://localhost"
        ):
            # http is allowed only where it cannot be intercepted, which in
            # practice means a developer's own machine.
            raise OidcConfigurationError(
                "issuer must be https, or http on localhost for development"
            )
        if "openid" not in self.scopes:
            raise OidcConfigurationError("the openid scope is not optional")


@dataclass(frozen=True, slots=True)
class LoginRequest:
    """The half of a login the browser carries away and must bring back."""

    authorization_url: str
    state: str
    nonce: str
    code_verifier: str


@dataclass(frozen=True, slots=True)
class FederatedIdentity:
    """A validated login. Everything here survived the checks above."""

    issuer: str
    subject: str
    #: When the provider says it actually authenticated the person, which is
    #: what a step-up check measures freshness against — not when it issued the
    #: token, which a refresh can make arbitrarily recent.
    authenticated_at: datetime | None
    #: Authentication context and methods, for deciding whether a sensitive
    #: operation needs sending the person back for a stronger factor.
    acr: str | None
    amr: tuple[str, ...]
    #: Display only. Never a lookup key.
    email: str | None
    email_verified: bool
    #: What the provider suggests calling this person. Display only, like the
    #: email beside it: used to name a newly provisioned account and never to
    #: find an existing one, because a name a provider lets somebody choose is
    #: not a claim about who they are.
    preferred_username: str | None = None


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def new_pkce_pair() -> tuple[str, str]:
    """A fresh verifier and its S256 challenge.

    ``plain`` is not offered. A provider that only supports it is one this
    boundary should refuse rather than accommodate.
    """

    verifier = _b64url(secrets.token_bytes(64))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


class OidcProvider:
    """One configured provider, with its metadata and signing keys cached."""

    def __init__(self, settings: OidcSettings) -> None:
        self._settings = settings
        self._metadata: dict[str, Any] | None = None
        self._metadata_fetched_at = 0.0
        self._key_set: dict[str, Any] | None = None

    @property
    def settings(self) -> OidcSettings:
        return self._settings

    async def metadata(self) -> Mapping[str, Any]:
        """The provider's discovery document, cached for an hour.

        The document's own ``issuer`` is compared with the configured one. A
        provider that describes itself as somebody else is either misconfigured
        or not the provider we think we are talking to; both are refusals.
        """

        now = time.monotonic()
        if self._metadata is not None and now - self._metadata_fetched_at < _DISCOVERY_TTL_SECONDS:
            return self._metadata

        url = self._settings.issuer.rstrip("/") + "/.well-known/openid-configuration"
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                response = await client.get(url)
                response.raise_for_status()
                document = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OidcDiscoveryError(f"could not read provider metadata: {exc}") from exc

        if document.get("issuer") != self._settings.issuer:
            raise OidcDiscoveryError(
                "provider metadata describes a different issuer than the one "
                "this deployment is configured for"
            )
        for required in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            if not document.get(required):
                raise OidcDiscoveryError(f"provider metadata has no {required}")

        methods = document.get("code_challenge_methods_supported")
        if methods is not None and "S256" not in methods:
            raise OidcDiscoveryError(
                "provider does not support S256 PKCE, which this boundary requires"
            )

        self._metadata = document
        self._metadata_fetched_at = now
        self._key_set = None
        return document

    async def begin_login(self, *, prompt: str | None = None) -> LoginRequest:
        """Build the authorization request, and the secrets that bind it."""

        document = await self.metadata()
        verifier, challenge = new_pkce_pair()
        state = _b64url(secrets.token_bytes(32))
        nonce = _b64url(secrets.token_bytes(32))

        from urllib.parse import urlencode

        parameters = {
            "response_type": "code",
            "client_id": self._settings.client_id,
            "redirect_uri": self._settings.redirect_url,
            "scope": " ".join(self._settings.scopes),
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        if prompt:
            parameters["prompt"] = prompt

        separator = "&" if "?" in document["authorization_endpoint"] else "?"
        return LoginRequest(
            authorization_url=(
                f"{document['authorization_endpoint']}{separator}{urlencode(parameters)}"
            ),
            state=state,
            nonce=nonce,
            code_verifier=verifier,
        )

    def check_response_issuer(self, response_issuer: str | None) -> None:
        """Refuse a callback that names a different issuer than we asked.

        RFC 9207. A provider that sends ``iss`` back and gets it wrong is the
        mix-up attack; one that omits it entirely predates the RFC, which is
        common enough that its absence is not itself a refusal — the ID token's
        own ``iss`` still has to match.
        """

        if response_issuer is not None and response_issuer != self._settings.issuer:
            raise OidcProtocolError(
                "authorization response names a different issuer than requested"
            )

    async def _fetch_key_set(self) -> dict[str, Any]:
        document = await self.metadata()
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                response = await client.get(document["jwks_uri"])
                response.raise_for_status()
                key_set = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OidcTokenError(f"could not read the provider's keys: {exc}") from exc
        if not isinstance(key_set.get("keys"), list) or not key_set["keys"]:
            raise OidcTokenError("the provider published no signing keys")
        self._key_set = key_set
        return key_set

    async def _signing_key(self, token: str):
        """The key this token names, refetching once if it is one we have not seen.

        Fetched with the same HTTP client as everything else here, rather than
        through ``PyJWKClient``, which uses urllib: no timeout, no shared
        configuration, and a provider that hangs would hang a login rather than
        fail it.

        A key id we do not hold is the ordinary shape of a rotation, so it costs
        one refetch. It is also the shape of a token signed by a key that does
        not exist, which is why the refetch happens once and not per attempt.
        """

        try:
            header = jwt.get_unverified_header(token)
        except jwt.InvalidTokenError as exc:
            raise OidcTokenError(f"id_token has no readable header: {exc}") from exc
        key_id = header.get("kid")

        key_set = self._key_set or await self._fetch_key_set()
        key = _find_key(key_set, key_id)
        if key is None:
            key_set = await self._fetch_key_set()
            key = _find_key(key_set, key_id)
        if key is None:
            raise OidcTokenError(
                "the provider published no key matching this token's key id"
            )
        try:
            return jwt.PyJWK(key).key
        except Exception as exc:  # PyJWK raises several unrelated types
            raise OidcTokenError(f"the provider's key is unusable: {exc}") from exc

    async def complete_login(
        self,
        *,
        code: str,
        code_verifier: str,
        expected_nonce: str,
        max_age_seconds: int | None = None,
    ) -> FederatedIdentity:
        """Redeem the code and validate everything the token claims."""

        document = await self.metadata()
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                response = await client.post(
                    document["token_endpoint"],
                    data={
                        "grant_type": "authorization_code",
                        "code": code,
                        "redirect_uri": self._settings.redirect_url,
                        "client_id": self._settings.client_id,
                        "client_secret": self._settings.client_secret,
                        "code_verifier": code_verifier,
                    },
                    headers={"Accept": "application/json"},
                )
        except httpx.HTTPError as exc:
            raise OidcProtocolError(f"token endpoint unreachable: {exc}") from exc

        if response.status_code != 200:
            # The body carries the provider's error code, which belongs in the
            # log; it must not reach the browser, where it describes somebody
            # else's account state as readily as this one's.
            raise OidcProtocolError(
                f"token endpoint refused the code: {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise OidcProtocolError("token endpoint returned no JSON") from exc

        id_token = payload.get("id_token")
        if not id_token:
            raise OidcProtocolError("token response carries no id_token")

        key = await self._signing_key(id_token)
        try:
            claims = jwt.decode(
                id_token,
                key=key,
                algorithms=_permitted_algorithms(document),
                audience=self._settings.client_id,
                issuer=self._settings.issuer,
                leeway=CLOCK_SKEW_SECONDS,
                options={
                    "require": ["iss", "sub", "aud", "exp", "iat"],
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_iat": True,
                    "verify_nbf": True,
                    "verify_aud": True,
                    "verify_iss": True,
                },
            )
        except jwt.InvalidTokenError as exc:
            raise OidcTokenError(f"id_token failed validation: {exc}") from exc

        return _identity_from_claims(
            claims,
            issuer=self._settings.issuer,
            client_id=self._settings.client_id,
            expected_nonce=expected_nonce,
            max_age_seconds=max_age_seconds,
        )


def _find_key(key_set: Mapping[str, Any], key_id: str | None) -> dict[str, Any] | None:
    """The key a token names, or the only one when it names none.

    A key set with several keys and a token with no ``kid`` is ambiguous, and
    picking one would mean accepting a signature from whichever key happened to
    be first.
    """

    keys = [k for k in key_set.get("keys", []) if isinstance(k, dict)]
    signing = [k for k in keys if k.get("use") in (None, "sig")]
    if key_id is None:
        return signing[0] if len(signing) == 1 else None
    for candidate in signing:
        if candidate.get("kid") == key_id:
            return candidate
    return None


def _permitted_algorithms(document: Mapping[str, Any]) -> list[str]:
    """Asymmetric signatures only, intersected with what the provider offers.

    ``none`` is not a signature. The HMAC family would make the client secret a
    verification key, so anyone holding it could mint tokens — which is a
    different threat model than the one this boundary is built for.
    """

    permitted = {"RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "PS256", "PS384", "PS512"}
    advertised = document.get("id_token_signing_alg_values_supported")
    if advertised:
        permitted &= set(advertised)
    if not permitted:
        raise OidcDiscoveryError(
            "provider offers no asymmetric id_token signing algorithm"
        )
    return sorted(permitted)


def _identity_from_claims(
    claims: Mapping[str, Any],
    *,
    issuer: str,
    client_id: str,
    expected_nonce: str,
    max_age_seconds: int | None,
) -> FederatedIdentity:
    if claims.get("nonce") != expected_nonce:
        raise OidcTokenError("id_token nonce does not match this login request")

    # ``azp`` names the party the token was issued for when the audience holds
    # more than one. Where present it must be us, or the token is somebody
    # else's even though our client id appears in its audience.
    authorized_party = claims.get("azp")
    if authorized_party is not None and authorized_party != client_id:
        raise OidcTokenError("id_token is authorized for a different party")

    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject.strip():
        raise OidcTokenError("id_token carries no usable subject")

    auth_time_claim = claims.get("auth_time")
    authenticated_at: datetime | None = None
    if auth_time_claim is not None:
        try:
            authenticated_at = datetime.fromtimestamp(
                int(auth_time_claim), tz=timezone.utc
            )
        except (TypeError, ValueError, OSError) as exc:
            raise OidcTokenError("id_token auth_time is not a timestamp") from exc

    if max_age_seconds is not None:
        if authenticated_at is None:
            raise OidcTokenError(
                "a maximum authentication age was requested and the provider "
                "returned no auth_time to check it against"
            )
        age = datetime.now(timezone.utc) - authenticated_at
        if age.total_seconds() > max_age_seconds + CLOCK_SKEW_SECONDS:
            raise OidcTokenError(
                "the provider authenticated this person longer ago than this "
                "operation allows"
            )

    methods = claims.get("amr")
    if isinstance(methods, str):
        methods = [methods]
    amr = tuple(str(item) for item in methods) if methods else ()

    email = claims.get("email")
    preferred_username = claims.get("preferred_username")
    return FederatedIdentity(
        issuer=issuer,
        subject=subject,
        authenticated_at=authenticated_at,
        acr=claims.get("acr"),
        amr=amr,
        email=email if isinstance(email, str) and email.strip() else None,
        email_verified=bool(claims.get("email_verified")),
        preferred_username=(
            preferred_username
            if isinstance(preferred_username, str) and preferred_username.strip()
            else None
        ),
    )


__all__ = [
    "CLOCK_SKEW_SECONDS",
    "FederatedIdentity",
    "LoginRequest",
    "OidcConfigurationError",
    "OidcDiscoveryError",
    "OidcError",
    "OidcProtocolError",
    "OidcProvider",
    "OidcSettings",
    "OidcTokenError",
    "new_pkce_pair",
]
