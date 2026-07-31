"""Google OAuth (authorization-code flow).

The backend owns the redirect: it builds the Google authorize URL, receives the
``code`` on the callback, exchanges it for tokens, and verifies the returned
``id_token`` against Google's JWKS. No Google access token is stored — we only
read the verified identity claims and then issue our own local token pair.

The OAuth ``state`` carries a signed CSRF nonce **and** the requested role
(``seeker``/``advisor``, or ``None`` when the frontend didn't ask for one) so the
role selected on the frontend survives the round trip through Google. It is a
short-lived JWT signed with ``JWT_SECRET`` — a forged or tampered ``state`` fails
signature verification.

When a *new* user arrives with no role in the state, the callback defers account
creation: it hands the frontend a signed **pending-signup token** carrying the
verified Google identity, the frontend shows a "continue as seeker/advisor"
chooser, and ``POST /auth/google/complete`` turns token + chosen role into the
actual account.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt
from jwt import PyJWKClient

from app.core.config import Settings
from app.core.exceptions import AuthenticationError
from app.models.user import UserRole

GOOGLE_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"  # noqa: S105 - public URL
GOOGLE_JWKS_URI = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS = ("accounts.google.com", "https://accounts.google.com")

# The signed OAuth state is short-lived — it only needs to survive the user's
# Google consent screen.
_STATE_EXPIRE_MINUTES = 10
_STATE_ISSUER = "globlejump-oauth-state"

# The pending-signup token only needs to survive the frontend's role-chooser
# screen. Replay is harmless: completing twice hits the existing-email
# auto-link path in ``get_or_create_google_user``.
_SIGNUP_EXPIRE_MINUTES = 15
_SIGNUP_ISSUER = "globlejump-oauth-signup"


@dataclass(slots=True)
class GoogleUserInfo:
    """Verified identity claims from a Google ``id_token``."""

    email: str
    email_verified: bool
    full_name: str | None
    sub: str


# --- OAuth state (signed CSRF nonce + requested role) ----------------------
def sign_state(role: UserRole | None, settings: Settings) -> str:
    """Sign a short-lived state token carrying a CSRF nonce and the role.

    ``role`` is ``None`` when the frontend started the flow without picking one —
    the callback then defers the choice to the post-Google role chooser.
    """
    now = datetime.now(UTC)
    payload = {
        "iss": _STATE_ISSUER,
        "iat": now,
        "exp": now + timedelta(minutes=_STATE_EXPIRE_MINUTES),
        "nonce": secrets.token_urlsafe(16),
        "role": role.value if role is not None else None,
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm="HS256")


def verify_state(state: str, settings: Settings) -> UserRole | None:
    """Validate the state signature/expiry and return the requested role.

    Returns ``None`` when the flow was started without a role. Raises
    :class:`AuthenticationError` on any tampering, expiry, or an unexpected
    role value.
    """
    try:
        payload = jwt.decode(
            state,
            settings.JWT_SECRET,
            algorithms=["HS256"],
            issuer=_STATE_ISSUER,
            options={"require": ["exp", "iss", "nonce"]},
        )
    except jwt.PyJWTError as exc:
        raise AuthenticationError("Invalid or expired OAuth state") from exc

    role_value = payload.get("role")
    if role_value is None:
        return None
    if not isinstance(role_value, str):
        raise AuthenticationError("Invalid OAuth state role")
    try:
        role = UserRole(role_value)
    except ValueError as exc:
        raise AuthenticationError("Invalid OAuth state role") from exc
    if role not in (UserRole.seeker, UserRole.advisor):
        raise AuthenticationError("Unsupported role for Google sign-in")
    return role


# --- Pending-signup token (verified identity awaiting a role choice) --------
def sign_signup_token(google_user: GoogleUserInfo, settings: Settings) -> str:
    """Sign a short-lived token carrying a verified-but-roleless Google identity.

    Issued by the callback instead of creating a user when a new email arrives
    with no requested role; redeemed by ``POST /auth/google/complete``.
    """
    now = datetime.now(UTC)
    payload = {
        "iss": _SIGNUP_ISSUER,
        "iat": now,
        "exp": now + timedelta(minutes=_SIGNUP_EXPIRE_MINUTES),
        "nonce": secrets.token_urlsafe(16),
        "email": google_user.email,
        "full_name": google_user.full_name,
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm="HS256")


def verify_signup_token(token: str, settings: Settings) -> tuple[str, str | None]:
    """Validate a pending-signup token and return ``(email, full_name)``.

    Raises :class:`AuthenticationError` on tampering or expiry.
    """
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET,
            algorithms=["HS256"],
            issuer=_SIGNUP_ISSUER,
            options={"require": ["exp", "iss", "nonce"]},
        )
    except jwt.PyJWTError as exc:
        raise AuthenticationError("Invalid or expired signup token") from exc

    email = payload.get("email")
    if not isinstance(email, str) or not email:
        raise AuthenticationError("Invalid signup token")
    full_name = payload.get("full_name")
    return email, full_name if isinstance(full_name, str) else None


# --- Authorize URL ---------------------------------------------------------
def build_authorization_url(settings: Settings, state: str) -> str:
    """Build the Google consent-screen URL to redirect the browser to."""
    params = {
        "client_id": settings.GOOGLE_CLIENT_ID,
        "redirect_uri": settings.GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "online",
        "prompt": "select_account",
    }
    return f"{GOOGLE_AUTH_ENDPOINT}?{urlencode(params)}"


# --- Code exchange + id_token verification ---------------------------------
async def exchange_code(code: str, settings: Settings) -> GoogleUserInfo:
    """Exchange the authorization ``code`` for tokens and verify the id_token.

    Raises :class:`AuthenticationError` on any exchange or verification failure.
    """
    data = {
        "code": code,
        "client_id": settings.GOOGLE_CLIENT_ID,
        "client_secret": settings.GOOGLE_CLIENT_SECRET,
        "redirect_uri": settings.GOOGLE_REDIRECT_URI,
        "grant_type": "authorization_code",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(GOOGLE_TOKEN_ENDPOINT, data=data)
    except httpx.HTTPError as exc:
        raise AuthenticationError("Could not reach Google to exchange code") from exc

    if response.status_code != 200:
        raise AuthenticationError("Google rejected the authorization code")

    id_token = response.json().get("id_token")
    if not id_token:
        raise AuthenticationError("Google response did not include an id_token")

    claims = _verify_id_token(id_token, settings)
    return GoogleUserInfo(
        email=claims["email"],
        email_verified=bool(claims.get("email_verified", False)),
        full_name=claims.get("name"),
        sub=claims["sub"],
    )


def _verify_id_token(id_token: str, settings: Settings) -> dict[str, Any]:
    """Verify a Google ``id_token`` against Google's JWKS (RS256)."""
    try:
        signing_key = _google_jwks_client().get_signing_key_from_jwt(id_token)
        claims = jwt.decode(
            id_token,
            signing_key.key,
            algorithms=["RS256"],
            audience=settings.GOOGLE_CLIENT_ID,
            issuer=list(GOOGLE_ISSUERS),
            options={"require": ["exp", "iss", "aud", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise AuthenticationError("Could not verify Google id_token") from exc

    if not claims.get("email"):
        raise AuthenticationError("Google id_token is missing an email claim")
    return claims


# Module-cached JWKS client (mirrors app.core.security._jwks_client). PyJWKClient
# caches signing keys internally and is safe to share across requests.
_GOOGLE_JWKS_CLIENT: PyJWKClient | None = None


def _google_jwks_client() -> PyJWKClient:
    global _GOOGLE_JWKS_CLIENT
    if _GOOGLE_JWKS_CLIENT is None:
        _GOOGLE_JWKS_CLIENT = PyJWKClient(GOOGLE_JWKS_URI, cache_keys=True)
    return _GOOGLE_JWKS_CLIENT
