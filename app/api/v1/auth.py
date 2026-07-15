"""Authentication endpoints.

Covers seeker and advisor registration, login with refresh-token issuance,
token rotation, logout, email verification, and password reset.

The ``POST /login`` endpoint returns ``TokenPair`` (access + refresh tokens).
Swagger Authorize still works because ``TokenPair`` includes ``access_token``
and ``token_type``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status
from fastapi.security import OAuth2PasswordRequestForm

from app.api.deps import CurrentUser, RequestIdDep, SettingsDep
from app.core.rate_limit import enforce_cooldown
from app.db.session import SessionDep
from app.schemas.advisor import AdvisorCreate, AdvisorRead
from app.schemas.response import Meta, ResponseEnvelope
from app.schemas.token import (
    EmailVerifyRequest,
    ForgotPasswordRequest,
    RefreshRequest,
    ResendVerificationRequest,
    ResetPasswordRequest,
    TokenPair,
)
from app.schemas.user import UserCreate, UserRead
from app.services import activity_log_service, auth_service, user_service
from app.services.email_service import send_password_reset_email, send_verification_email

router = APIRouter(prefix="/auth", tags=["auth"])


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


@router.post(
    "/register",
    response_model=ResponseEnvelope[UserRead],
    status_code=status.HTTP_201_CREATED,
)
async def register(
    data: UserCreate,
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[UserRead]:
    """Seeker self-registration. Role is always ``seeker``."""
    user = await user_service.create_user(session, data)
    raw_token = await auth_service.create_email_verification_token(session, user, settings)
    await send_verification_email(user.email, user.full_name or "", raw_token, settings)
    return ResponseEnvelope[UserRead](
        data=UserRead.model_validate(user), meta=Meta(request_id=request_id)
    )


@router.post(
    "/register/advisor",
    response_model=ResponseEnvelope[AdvisorRead],
    status_code=status.HTTP_201_CREATED,
)
async def register_advisor(
    data: AdvisorCreate,
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[AdvisorRead]:
    """Advisor self-registration. Account is inactive until admin approves."""
    user = await user_service.create_advisor(session, data)
    raw_token = await auth_service.create_email_verification_token(session, user, settings)
    await send_verification_email(user.email, user.full_name or "", raw_token, settings)
    return ResponseEnvelope[AdvisorRead](
        data=AdvisorRead.model_validate(user), meta=Meta(request_id=request_id)
    )


# ---------------------------------------------------------------------------
# Login / token management
# ---------------------------------------------------------------------------


@router.post("/login", response_model=TokenPair)
async def login(
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
    session: SessionDep,
    settings: SettingsDep,
) -> TokenPair:
    """OAuth2 password flow. Returns access + refresh token pair."""
    user = await user_service.authenticate(session, form_data.username, form_data.password)
    await activity_log_service.record_login(session, user.id)
    access_token, raw_refresh = await auth_service.create_token_pair(session, user, settings)
    return TokenPair(
        access_token=access_token,
        refresh_token=raw_refresh,
        role=user.role,
        verification_status=user.verification_status,
    )


@router.post("/refresh", response_model=TokenPair)
async def refresh(
    body: RefreshRequest,
    session: SessionDep,
    settings: SettingsDep,
) -> TokenPair:
    """Rotate a refresh token: revoke the old one and issue a new pair."""
    access_token, raw_refresh, user = await auth_service.rotate_refresh_token(
        session, body.refresh_token, settings
    )
    return TokenPair(
        access_token=access_token,
        refresh_token=raw_refresh,
        role=user.role,
        verification_status=user.verification_status,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    body: RefreshRequest,
    session: SessionDep,
    _current_user: CurrentUser,
) -> None:
    """Revoke the provided refresh token (requires a valid access token)."""
    await auth_service.revoke_refresh_token(session, body.refresh_token)


# ---------------------------------------------------------------------------
# Email verification
# ---------------------------------------------------------------------------


@router.post("/verify-email", response_model=ResponseEnvelope[UserRead])
async def verify_email(
    body: EmailVerifyRequest,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[UserRead]:
    """Verify a user's email address using the one-time token from the verification email."""
    user = await auth_service.verify_email(session, body.token)
    return ResponseEnvelope[UserRead](
        data=UserRead.model_validate(user), meta=Meta(request_id=request_id)
    )


@router.post("/resend-verification", status_code=status.HTTP_204_NO_CONTENT)
async def resend_verification(
    body: ResendVerificationRequest,
    session: SessionDep,
    settings: SettingsDep,
) -> None:
    """Resend the email verification token. Always 204 to prevent email enumeration."""
    enforce_cooldown(f"resend-verification:{body.email}", cooldown_seconds=60)
    user = await user_service.get_by_email(session, body.email)
    if user is None or user.is_email_verified:
        return
    raw_token = await auth_service.create_email_verification_token(session, user, settings)
    await send_verification_email(user.email, user.full_name or "", raw_token, settings)


# ---------------------------------------------------------------------------
# Password reset
# ---------------------------------------------------------------------------


@router.post("/forgot-password", status_code=status.HTTP_204_NO_CONTENT)
async def forgot_password(
    body: ForgotPasswordRequest,
    session: SessionDep,
    settings: SettingsDep,
) -> None:
    """Request a password reset email. Always 204 to prevent email enumeration."""
    enforce_cooldown(f"forgot-password:{body.email}", cooldown_seconds=60)
    raw_token = await auth_service.create_password_reset_token(session, body.email, settings)
    if raw_token is not None:
        user = await user_service.get_by_email(session, body.email)
        await send_password_reset_email(
            body.email, user.full_name or "" if user else "", raw_token, settings
        )


@router.post("/reset-password", response_model=ResponseEnvelope[UserRead])
async def reset_password(
    body: ResetPasswordRequest,
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[UserRead]:
    """Reset password using the one-time token from the reset email."""
    user = await auth_service.reset_password(session, body.token, body.new_password, settings)
    return ResponseEnvelope[UserRead](
        data=UserRead.model_validate(user), meta=Meta(request_id=request_id)
    )
