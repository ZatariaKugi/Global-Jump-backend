"""Shared API dependencies: settings, DB session, and hybrid authentication.

``get_current_principal`` accepts a bearer token issued by either this service or the
external identity-service. Either way it resolves to a :class:`Principal` carrying the
bare user UUID — exactly what ``BaseModel.created_by`` / ``updated_by`` expect.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import OAuth2PasswordBearer

from app.core.config import Settings, get_settings
from app.core.exceptions import AuthenticationError, PermissionDeniedError
from app.db.session import SessionDep
from app.models.user import User, UserRole, VerificationStatus
from app.schemas.token import TokenPayload
from app.services import user_service

SettingsDep = Annotated[Settings, Depends(get_settings)]


def get_request_id(request: Request) -> str | None:
    """The correlation ID assigned by ``RequestContextMiddleware``."""
    return getattr(request.state, "request_id", None)


RequestIdDep = Annotated[str | None, Depends(get_request_id)]

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login", auto_error=False)
TokenDep = Annotated[str | None, Depends(oauth2_scheme)]


@dataclass(slots=True)
class Principal:
    """Authenticated caller. ``user`` is populated only for local accounts."""

    id: uuid.UUID
    role: str | None
    is_external: bool
    user: User | None = None
    impersonated_by: uuid.UUID | None = None


async def get_current_principal(
    token: TokenDep,
    settings: SettingsDep,
    session: SessionDep,
) -> Principal:
    from app.core.security import decode_token  # local import avoids cycle at import time

    if not token:
        raise AuthenticationError("Not authenticated")

    claims = decode_token(token, settings)
    try:
        payload = TokenPayload.model_validate(claims)
    except ValueError as exc:
        raise AuthenticationError("Invalid token payload") from exc

    is_external = payload.iss != settings.JWT_ISSUER
    if is_external:
        return Principal(id=payload.sub, role=payload.role, is_external=True)

    user = await user_service.get_by_id(session, payload.sub)
    if user is None:
        raise AuthenticationError("User not found or inactive")
    if user.role == UserRole.advisor and user.verification_status == VerificationStatus.rejected:
        raise AuthenticationError(
            "Your account was rejected by an admin. Please contact support."
        )
    # Pending / under-review advisors are allowed through so they can complete
    # onboarding or view Approval Pending. Externally-facing advisor actions
    # are still gated by require_verified_advisor.
    if not user.is_active and not (
        user.role.value == "advisor"
        and user.verification_status
        in (
            VerificationStatus.pending,
            VerificationStatus.under_review,
        )
    ):
        raise AuthenticationError("User not found or inactive")
    return Principal(
        id=user.id,
        role=user.role.value,
        is_external=False,
        user=user,
        impersonated_by=payload.impersonated_by,
    )


CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]


async def get_current_user(principal: CurrentPrincipal) -> User:
    """Require a *local* user account (rejects external-only principals)."""
    if principal.user is None:
        raise AuthenticationError("A local user account is required for this endpoint")
    return principal.user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_role(*roles: UserRole) -> Callable[[Principal], Awaitable[Principal]]:
    """Dependency factory enforcing that the caller has one of ``roles``."""
    allowed = {r.value for r in roles}

    async def _guard(principal: CurrentPrincipal) -> Principal:
        if principal.role not in allowed:
            raise PermissionDeniedError("Not enough permissions")
        return principal

    return _guard


async def require_verified_advisor(principal: CurrentPrincipal) -> Principal:
    """Require an advisor whose account has been approved by an admin."""
    if principal.role != UserRole.advisor.value:
        raise PermissionDeniedError("Advisor account required")
    if principal.user and principal.user.verification_status != VerificationStatus.approved:
        raise PermissionDeniedError("Advisor account not yet verified")
    return principal
