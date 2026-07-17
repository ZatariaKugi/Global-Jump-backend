"""Stateful auth operations: refresh token rotation, email verification, password reset.

All database writes go through the async session.  Raw tokens are generated here and
returned to the caller (endpoint) — they are NEVER stored; only their SHA-256 hashes
are persisted.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import AuthenticationError
from app.core.security import (
    create_access_token,
    generate_token,
    hash_password,
    hash_token,
)
from app.models.token import RefreshToken, TokenPurpose, UserToken
from app.models.user import User, UserRole, VerificationStatus

# ---------------------------------------------------------------------------
# Refresh-token pair
# ---------------------------------------------------------------------------


async def create_token_pair(
    session: AsyncSession, user: User, settings: Settings
) -> tuple[str, str]:
    """Issue (access_token, raw_refresh_token).  Persists refresh hash to DB."""
    access_token = create_access_token(
        subject=user.id,
        settings=settings,
        extra_claims={"role": user.role.value, "email_verified": user.is_email_verified},
    )
    raw_refresh = generate_token()
    refresh_record = RefreshToken(
        user_id=user.id,
        token_hash=hash_token(raw_refresh),
        expires_at=datetime.now(UTC) + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
    )
    session.add(refresh_record)
    await session.flush()
    return access_token, raw_refresh


async def rotate_refresh_token(
    session: AsyncSession, raw_refresh_token: str, settings: Settings
) -> tuple[str, str, User]:
    """Validate the old refresh token, revoke it, and issue a new pair."""
    token_hash = hash_token(raw_refresh_token)
    result = await session.execute(
        select(RefreshToken).where(RefreshToken.token_hash == token_hash)
    )
    record = result.scalar_one_or_none()

    now = datetime.now(UTC)
    if record is None or record.revoked_at is not None or record.expires_at < now:
        raise AuthenticationError("Invalid or expired refresh token")

    record.revoked_at = now
    session.add(record)

    user = await session.get(User, record.user_id)
    if user is None:
        raise AuthenticationError("User not found or inactive")
    if user.role == UserRole.advisor and user.verification_status == VerificationStatus.rejected:
        raise AuthenticationError(
            "Your account was rejected by an admin. Please contact support."
        )
    if not user.is_active and not (
        user.role == UserRole.advisor
        and user.verification_status
        in (
            VerificationStatus.pending,
            VerificationStatus.under_review,
        )
    ):
        raise AuthenticationError("User not found or inactive")

    access_token, raw_refresh = await create_token_pair(session, user, settings)
    return access_token, raw_refresh, user


async def revoke_refresh_token(session: AsyncSession, raw_refresh_token: str) -> None:
    """Logout: mark the refresh token as revoked."""
    token_hash = hash_token(raw_refresh_token)
    result = await session.execute(
        select(RefreshToken).where(RefreshToken.token_hash == token_hash)
    )
    record = result.scalar_one_or_none()
    if record is not None and record.revoked_at is None:
        record.revoked_at = datetime.now(UTC)
        session.add(record)
        await session.flush()


async def _revoke_all_refresh_tokens(session: AsyncSession, user_id: uuid.UUID) -> None:
    """Invalidate every active refresh token for a user (e.g. after password reset)."""
    result = await session.execute(
        select(RefreshToken).where(
            RefreshToken.user_id == user_id,
            RefreshToken.revoked_at.is_(None),
        )
    )
    now = datetime.now(UTC)
    for record in result.scalars():
        record.revoked_at = now
        session.add(record)
    await session.flush()


# ---------------------------------------------------------------------------
# Email verification
# ---------------------------------------------------------------------------


async def create_email_verification_token(
    session: AsyncSession, user: User, settings: Settings
) -> str:
    """Generate + store a UserToken for email verification.  Returns the raw token."""
    raw = generate_token()
    record = UserToken(
        user_id=user.id,
        token_hash=hash_token(raw),
        purpose=TokenPurpose.email_verification,
        expires_at=datetime.now(UTC) + timedelta(hours=settings.EMAIL_VERIFY_TOKEN_EXPIRE_HOURS),
    )
    session.add(record)
    await session.flush()
    return raw


async def verify_email(session: AsyncSession, raw_token: str) -> User:
    """Validate the one-time token and mark the user's email as verified."""
    token_hash = hash_token(raw_token)
    result = await session.execute(
        select(UserToken).where(
            UserToken.token_hash == token_hash,
            UserToken.purpose == TokenPurpose.email_verification,
        )
    )
    record = result.scalar_one_or_none()
    now = datetime.now(UTC)
    if record is None or record.used_at is not None or record.expires_at < now:
        raise AuthenticationError("Invalid or expired verification token")

    user = await session.get(User, record.user_id)
    if user is None:
        raise AuthenticationError("User not found")

    user.email_verified_at = now
    record.used_at = now
    session.add(user)
    session.add(record)
    await session.flush()
    await session.refresh(user)
    return user


# ---------------------------------------------------------------------------
# Password reset
# ---------------------------------------------------------------------------


async def _issue_password_reset_token(session: AsyncSession, user: User, settings: Settings) -> str:
    raw = generate_token()
    record = UserToken(
        user_id=user.id,
        token_hash=hash_token(raw),
        purpose=TokenPurpose.password_reset,
        expires_at=datetime.now(UTC)
        + timedelta(minutes=settings.PASSWORD_RESET_TOKEN_EXPIRE_MINUTES),
    )
    session.add(record)
    await session.flush()
    return raw


async def create_password_reset_token(
    session: AsyncSession, email: str, settings: Settings
) -> str | None:
    """Generate + store a password reset token.  Returns raw token, or None if email unknown.

    Callers should always return HTTP 200 regardless of the return value to prevent
    email enumeration.
    """
    from app.services.user_service import get_by_email

    user = await get_by_email(session, email)
    if user is None:
        return None
    return await _issue_password_reset_token(session, user, settings)


async def create_password_reset_token_for_user(
    session: AsyncSession, user: User, settings: Settings
) -> str:
    """Admin-triggered variant — the caller already has the User loaded (e.g. by
    id), so there's no email lookup or enumeration concern to guard against."""
    return await _issue_password_reset_token(session, user, settings)


async def reset_password(
    session: AsyncSession, raw_token: str, new_password: str, settings: Settings
) -> User:
    """Validate token, hash the new password, and revoke all existing refresh tokens."""
    token_hash = hash_token(raw_token)
    result = await session.execute(
        select(UserToken).where(
            UserToken.token_hash == token_hash,
            UserToken.purpose == TokenPurpose.password_reset,
        )
    )
    record = result.scalar_one_or_none()
    now = datetime.now(UTC)
    if record is None or record.used_at is not None or record.expires_at < now:
        raise AuthenticationError("Invalid or expired reset token")

    user = await session.get(User, record.user_id)
    if user is None:
        raise AuthenticationError("User not found")

    user.hashed_password = hash_password(new_password)
    record.used_at = now
    session.add(user)
    session.add(record)
    await _revoke_all_refresh_tokens(session, user.id)
    await session.flush()
    await session.refresh(user)
    return user
