"""Password hashing and JWT handling.

Follows the official FastAPI security guidance: ``pwdlib`` (Argon2) for password
hashing and ``PyJWT`` for tokens. Supports two token issuers:

* **local** — tokens this service issues (HS256, ``iss = settings.JWT_ISSUER``).
* **external** — tokens issued by the identity-service, verified with either a shared
  HS256 secret or a JWKS endpoint (RS256), depending on configuration.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any

import jwt
from jwt import PyJWKClient
from pwdlib import PasswordHash

from app.core.config import Settings
from app.core.exceptions import AuthenticationError

_password_hash = PasswordHash.recommended()


# --- One-time / refresh tokens --------------------------------------------
def generate_token() -> str:
    """Generate a 32-byte URL-safe random token string."""
    return secrets.token_urlsafe(32)


def hash_token(raw: str) -> str:
    """SHA-256 hex digest of a token — the value stored in the DB."""
    return hashlib.sha256(raw.encode()).hexdigest()


# --- Passwords -------------------------------------------------------------
def hash_password(password: str) -> str:
    return _password_hash.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return _password_hash.verify(plain_password, hashed_password)


# --- Local token issuance --------------------------------------------------
def create_access_token(
    subject: str | uuid.UUID,
    settings: Settings,
    *,
    expires_delta: timedelta | None = None,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    now = datetime.now(UTC)
    expire = now + (expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES))
    payload: dict[str, Any] = {
        "sub": str(subject),
        "iss": settings.JWT_ISSUER,
        "iat": now,
        "exp": expire,
    }
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


# --- Verification ----------------------------------------------------------
@lru_cache
def _jwks_client(url: str) -> PyJWKClient:
    return PyJWKClient(url, cache_keys=True)


def _decode_local(token: str, settings: Settings) -> dict[str, Any]:
    return jwt.decode(
        token,
        settings.JWT_SECRET,
        algorithms=[settings.JWT_ALGORITHM],
        issuer=settings.JWT_ISSUER,
        options={"require": ["exp", "sub", "iss"]},
    )


def _decode_external(token: str, settings: Settings) -> dict[str, Any]:
    if settings.IDENTITY_JWKS_URL is not None:
        signing_key = _jwks_client(str(settings.IDENTITY_JWKS_URL)).get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256", "ES256"],
            issuer=settings.IDENTITY_ISSUER,
            audience=settings.IDENTITY_AUDIENCE,
            options={"require": ["exp", "sub", "iss"]},
        )
    # Shared-secret (HS256) path.
    return jwt.decode(
        token,
        settings.IDENTITY_JWT_SECRET or "",
        algorithms=["HS256"],
        issuer=settings.IDENTITY_ISSUER,
        audience=settings.IDENTITY_AUDIENCE,
        options={"require": ["exp", "sub", "iss"]},
    )


def decode_token(token: str, settings: Settings) -> dict[str, Any]:
    """Decode a bearer token.

    Tries the local issuer first; if the token's ``iss`` is the external
    identity-service (and external trust is configured), verifies it accordingly.
    Raises :class:`AuthenticationError` on any failure.
    """
    try:
        unverified = jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError as exc:
        raise AuthenticationError("Malformed token") from exc

    issuer = unverified.get("iss")
    try:
        if issuer == settings.JWT_ISSUER:
            return _decode_local(token, settings)
        if settings.external_auth_enabled and issuer == settings.IDENTITY_ISSUER:
            return _decode_external(token, settings)
        raise AuthenticationError("Unrecognized token issuer")
    except jwt.PyJWTError as exc:
        raise AuthenticationError("Could not validate credentials") from exc
