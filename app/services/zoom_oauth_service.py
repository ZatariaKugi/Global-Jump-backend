"""Zoom OAuth (user-managed authorization-code flow) for Advisor account linking.

Each advisor connects their own Zoom account. Tokens are exchanged and refreshed
server-side; the frontend never sees access or refresh tokens.

OAuth ``state`` is a short-lived JWT (advisor_id + CSRF nonce) signed with
``JWT_SECRET``. An optional HttpOnly cookie may also carry the nonce when the
browser can store it (same-site API). Cross-origin SPAs (e.g. localhost FE →
ngrok API) often cannot set that cookie, so the signed ``state`` alone is
accepted — it is unguessable and expires quickly.
"""

from __future__ import annotations

import base64
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt

from app.core.config import Settings
from app.core.exceptions import AppError, AuthenticationError, NotFoundError
from app.core.logging import get_logger

logger = get_logger(__name__)

ZOOM_AUTH_ENDPOINT = "https://zoom.us/oauth/authorize"
ZOOM_TOKEN_ENDPOINT = "https://zoom.us/oauth/token"  # noqa: S105 - public URL
ZOOM_REVOKE_ENDPOINT = "https://zoom.us/oauth/revoke"
ZOOM_API_BASE = "https://api.zoom.us/v2"

# Granular scopes: profile read + create/update/delete scheduled meetings.
ZOOM_OAUTH_SCOPES = "user:read:user meeting:write:meeting meeting:delete:meeting"

_STATE_EXPIRE_MINUTES = 10
_STATE_ISSUER = "globlejump-zoom-oauth-state"

# Refresh a few minutes early so callers rarely hit an expired token race.
_TOKEN_REFRESH_SKEW = timedelta(minutes=2)


@dataclass(slots=True)
class ZoomTokenSet:
    access_token: str
    refresh_token: str
    expires_in: int
    scope: str | None


@dataclass(slots=True)
class ZoomUserInfo:
    zoom_user_id: str
    zoom_account_id: str | None
    zoom_email: str | None


def require_zoom_configured(settings: Settings) -> None:
    if not settings.zoom_oauth_enabled:
        raise NotFoundError("Zoom integration is not configured")


def sign_state(advisor_id: uuid.UUID, settings: Settings) -> tuple[str, str]:
    """Return ``(state_jwt, nonce)`` bound to ``advisor_id``."""
    now = datetime.now(UTC)
    nonce = secrets.token_urlsafe(16)
    payload = {
        "iss": _STATE_ISSUER,
        "iat": now,
        "exp": now + timedelta(minutes=_STATE_EXPIRE_MINUTES),
        "nonce": nonce,
        "advisor_id": str(advisor_id),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm="HS256"), nonce


def verify_state(state: str, expected_nonce: str | None, settings: Settings) -> uuid.UUID:
    """Validate signed OAuth state; return advisor_id.

    The ``state`` JWT alone is sufficient (signed + short-lived). When the
    optional ``gj_zoom_oauth_state`` cookie is present, its value must match the
    state's nonce (extra same-browser binding). Missing cookie is allowed so
    cross-origin SPAs work without third-party cookies / ``withCredentials``.
    """
    try:
        payload = jwt.decode(
            state,
            settings.JWT_SECRET,
            algorithms=["HS256"],
            issuer=_STATE_ISSUER,
            options={"require": ["exp", "iss", "nonce", "advisor_id"]},
        )
    except jwt.PyJWTError as exc:
        raise AuthenticationError("Invalid or expired OAuth state") from exc

    nonce = payload.get("nonce")
    if not isinstance(nonce, str) or not nonce:
        raise AuthenticationError("Invalid OAuth state")

    # Cookie is optional. If the browser sent one, it must match (CSRF binding).
    if expected_nonce is not None and not secrets.compare_digest(nonce, expected_nonce):
        raise AuthenticationError("OAuth state did not match this browser")

    raw_id = payload.get("advisor_id")
    if not isinstance(raw_id, str):
        raise AuthenticationError("Invalid OAuth state")
    try:
        return uuid.UUID(raw_id)
    except ValueError as exc:
        raise AuthenticationError("Invalid OAuth state") from exc


def build_authorization_url(settings: Settings, state: str) -> str:
    require_zoom_configured(settings)
    params = {
        "response_type": "code",
        "client_id": settings.ZOOM_CLIENT_ID,
        "redirect_uri": settings.ZOOM_REDIRECT_URI,
        "state": state,
        "scope": ZOOM_OAUTH_SCOPES,
    }
    return f"{ZOOM_AUTH_ENDPOINT}?{urlencode(params)}"


def _basic_auth_header(settings: Settings) -> str:
    raw = f"{settings.ZOOM_CLIENT_ID}:{settings.ZOOM_CLIENT_SECRET}".encode()
    return "Basic " + base64.b64encode(raw).decode()


async def exchange_code(code: str, settings: Settings) -> ZoomTokenSet:
    """Exchange an authorization code for Zoom tokens."""
    require_zoom_configured(settings)
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": settings.ZOOM_REDIRECT_URI,
    }
    headers = {
        "Authorization": _basic_auth_header(settings),
        "Content-Type": "application/x-www-form-urlencoded",
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(ZOOM_TOKEN_ENDPOINT, data=data, headers=headers)
    except httpx.HTTPError as exc:
        logger.warning("zoom_token_exchange_http_error", error=str(exc))
        raise AppError(
            "Unable to connect your Zoom account. Please try again.",
            code="zoom_token_exchange_failed",
        ) from exc

    if response.status_code >= 400:
        logger.warning(
            "zoom_token_exchange_rejected",
            status_code=response.status_code,
        )
        raise AppError(
            "Unable to connect your Zoom account. Please try again.",
            code="zoom_token_exchange_failed",
        )

    body: dict[str, Any] = response.json()
    access = body.get("access_token")
    refresh = body.get("refresh_token")
    expires_in = body.get("expires_in")
    if not isinstance(access, str) or not isinstance(refresh, str):
        raise AppError(
            "Unable to connect your Zoom account. Please try again.",
            code="zoom_token_exchange_failed",
        )
    if not isinstance(expires_in, int):
        try:
            expires_in = int(expires_in)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise AppError(
                "Unable to connect your Zoom account. Please try again.",
                code="zoom_token_exchange_failed",
            ) from exc

    scope = body.get("scope")
    return ZoomTokenSet(
        access_token=access,
        refresh_token=refresh,
        expires_in=expires_in,
        scope=scope if isinstance(scope, str) else None,
    )


async def refresh_access_token(refresh_token: str, settings: Settings) -> ZoomTokenSet:
    """Refresh Zoom tokens. Zoom may rotate the refresh token — always persist both."""
    require_zoom_configured(settings)
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    headers = {
        "Authorization": _basic_auth_header(settings),
        "Content-Type": "application/x-www-form-urlencoded",
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(ZOOM_TOKEN_ENDPOINT, data=data, headers=headers)
    except httpx.HTTPError as exc:
        logger.warning("zoom_token_refresh_http_error", error=str(exc))
        raise AppError(
            "Zoom session expired. Please reconnect your Zoom account.",
            code="zoom_token_refresh_failed",
        ) from exc

    if response.status_code >= 400:
        logger.warning("zoom_token_refresh_rejected", status_code=response.status_code)
        raise AppError(
            "Zoom session expired. Please reconnect your Zoom account.",
            code="zoom_token_refresh_failed",
        )

    body: dict[str, Any] = response.json()
    access = body.get("access_token")
    new_refresh = body.get("refresh_token") or refresh_token
    expires_in = body.get("expires_in")
    if not isinstance(access, str) or not isinstance(new_refresh, str):
        raise AppError(
            "Zoom session expired. Please reconnect your Zoom account.",
            code="zoom_token_refresh_failed",
        )
    if not isinstance(expires_in, int):
        try:
            expires_in = int(expires_in)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise AppError(
                "Zoom session expired. Please reconnect your Zoom account.",
                code="zoom_token_refresh_failed",
            ) from exc

    scope = body.get("scope")
    return ZoomTokenSet(
        access_token=access,
        refresh_token=new_refresh,
        expires_in=expires_in,
        scope=scope if isinstance(scope, str) else None,
    )


async def fetch_zoom_user(access_token: str) -> ZoomUserInfo:
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(f"{ZOOM_API_BASE}/users/me", headers=headers)
    except httpx.HTTPError as exc:
        logger.warning("zoom_users_me_http_error", error=str(exc))
        raise AppError(
            "Unable to connect your Zoom account. Please try again.",
            code="zoom_user_fetch_failed",
        ) from exc

    if response.status_code >= 400:
        logger.warning("zoom_users_me_rejected", status_code=response.status_code)
        raise AppError(
            "Unable to connect your Zoom account. Please try again.",
            code="zoom_user_fetch_failed",
        )

    body: dict[str, Any] = response.json()
    zoom_user_id = body.get("id")
    if not isinstance(zoom_user_id, str) or not zoom_user_id:
        raise AppError(
            "Unable to connect your Zoom account. Please try again.",
            code="zoom_user_fetch_failed",
        )
    account_id = body.get("account_id")
    email = body.get("email")
    return ZoomUserInfo(
        zoom_user_id=zoom_user_id,
        zoom_account_id=account_id if isinstance(account_id, str) else None,
        zoom_email=email if isinstance(email, str) else None,
    )


async def revoke_token(token: str, settings: Settings) -> None:
    """Best-effort revoke at Zoom. Failures are logged; local disconnect still proceeds."""
    if not settings.zoom_oauth_enabled:
        return
    data = {"token": token}
    headers = {
        "Authorization": _basic_auth_header(settings),
        "Content-Type": "application/x-www-form-urlencoded",
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(ZOOM_REVOKE_ENDPOINT, data=data, headers=headers)
        if response.status_code >= 400:
            logger.warning("zoom_revoke_rejected", status_code=response.status_code)
    except httpx.HTTPError as exc:
        logger.warning("zoom_revoke_http_error", error=str(exc))


def token_needs_refresh(expires_at: datetime) -> bool:
    now = datetime.now(UTC)
    expires = expires_at if expires_at.tzinfo is not None else expires_at.replace(tzinfo=UTC)
    return expires <= now + _TOKEN_REFRESH_SKEW


def frontend_return_url(settings: Settings, **query: str) -> str:
    path = settings.ZOOM_FRONTEND_RETURN_PATH
    if not path.startswith("/"):
        path = f"/{path}"
    base = f"{settings.FRONTEND_URL.rstrip('/')}{path}"
    if not query:
        return base
    return f"{base}?{urlencode(query)}"
