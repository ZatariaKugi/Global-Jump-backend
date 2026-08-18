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
from app.core.exceptions import AppError, AuthenticationError, ConflictError
from app.core.security import (
    create_access_token,
    generate_token,
    hash_password,
    hash_token,
    verify_password,
)
from app.models.token import RefreshToken, TokenPurpose, UserToken
from app.models.user import AuthProvider, User, UserRole, VerificationStatus

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
        extra_claims={
            "role": user.role.value,
            "email_verified": user.is_email_verified,
            "token_version": user.token_version,
        },
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
        raise AuthenticationError("Your account was rejected by an admin. Please contact support.")
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


async def invalidate_sessions(session: AsyncSession, user: User) -> None:
    """Kill every live session: bump ``token_version`` and revoke refresh tokens.

    Outstanding email-verify / password-reset links snapshot ``token_version`` at
    issue time and will fail on consume after this bump.
    """
    user.token_version = user.token_version + 1
    session.add(user)
    await _revoke_all_refresh_tokens(session, user.id)


def _assert_bound_token_version(record: UserToken, user: User, *, message: str, code: str) -> None:
    if record.token_version != user.token_version:
        raise AppError(message, code=code)


# ---------------------------------------------------------------------------
# Email verification
# ---------------------------------------------------------------------------


async def _revoke_unused_email_verification_tokens(
    session: AsyncSession, user_id: uuid.UUID
) -> None:
    """Invalidate outstanding email-verification links before issuing a new one."""
    now = datetime.now(UTC)
    result = await session.execute(
        select(UserToken).where(
            UserToken.user_id == user_id,
            UserToken.purpose == TokenPurpose.email_verification,
            UserToken.used_at.is_(None),
        )
    )
    for old_record in result.scalars().all():
        old_record.used_at = now
        old_record.expires_at = now
        session.add(old_record)
    await session.flush()


async def create_email_verification_token(
    session: AsyncSession, user: User, settings: Settings
) -> str:
    """Generate + store a UserToken for email verification.  Returns the raw token.

    Any previous unused verification links for this user are revoked so only the
    latest email works (same pattern as password reset).
    """
    await _revoke_unused_email_verification_tokens(session, user.id)
    raw = generate_token()
    record = UserToken(
        user_id=user.id,
        token_hash=hash_token(raw),
        purpose=TokenPurpose.email_verification,
        expires_at=datetime.now(UTC) + timedelta(hours=settings.EMAIL_VERIFY_TOKEN_EXPIRE_HOURS),
        token_version=user.token_version,
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
    if record is None:
        raise AuthenticationError("Invalid or expired verification token")
    # Expiry first so links superseded by a newer resend (force-expired) and
    # naturally timed-out links share a clear "expired" message (GJ-EV-061).
    if _as_utc(record.expires_at) <= now:
        raise AppError(
            "Verification link has expired",
            code="verify_token_expired",
        )
    if record.used_at is not None:
        raise AppError(
            "This verification link has already been used",
            code="verify_token_used",
        )

    user = await session.get(User, record.user_id)
    if user is None:
        raise AuthenticationError("User not found")
    _assert_bound_token_version(
        record,
        user,
        message="Verification link has expired",
        code="verify_token_expired",
    )

    if user.is_email_verified:
        # Burn the token so a stale link cannot be replayed after verification.
        record.used_at = now
        session.add(record)
        await session.flush()
        raise ConflictError("Email is already verified", code="already_verified")

    user.email_verified_at = now
    record.used_at = now
    record.expires_at = now
    session.add(user)
    session.add(record)
    await session.flush()
    await session.refresh(user)
    return user


# ---------------------------------------------------------------------------
# Password reset
# ---------------------------------------------------------------------------


def assert_password_reset_allowed(user: User) -> None:
    """Google sign-in accounts cannot use the password-reset flow."""
    if user.auth_provider == AuthProvider.google or user.hashed_password is None:
        raise AppError(
            "This account uses Google sign-in. Password reset is not available — "
            "please continue with Google.",
            code="google_account_no_password_reset",
        )


async def _issue_password_reset_token(session: AsyncSession, user: User, settings: Settings) -> str:
    # Invalidate all previous unused password_reset tokens for this user so
    # only the latest link works (GJ-FP-020).
    assert_password_reset_allowed(user)
    now = datetime.now(UTC)
    result = await session.execute(
        select(UserToken).where(
            UserToken.user_id == user.id,
            UserToken.purpose == TokenPurpose.password_reset,
            UserToken.used_at.is_(None),
        )
    )
    for old_record in result.scalars().all():
        old_record.used_at = now
        old_record.expires_at = now
        session.add(old_record)

    raw = generate_token()
    record = UserToken(
        user_id=user.id,
        token_hash=hash_token(raw),
        purpose=TokenPurpose.password_reset,
        expires_at=now + timedelta(minutes=settings.PASSWORD_RESET_TOKEN_EXPIRE_MINUTES),
        token_version=user.token_version,
    )
    session.add(record)
    await session.flush()
    return raw


async def create_password_reset_token(
    session: AsyncSession, email: str, settings: Settings
) -> tuple[User, str] | None:
    """Issue a reset token for a local-password account, or ``None``.

    Returns ``None`` for unknown emails *and* Google-only accounts so public
    callers can always respond 204 without leaking whether the address is
    registered or how the account signs in.
    """
    from app.services.user_service import get_by_email

    user = await get_by_email(session, email)
    if user is None:
        return None
    if user.auth_provider == AuthProvider.google or user.hashed_password is None:
        return None
    raw = await _issue_password_reset_token(session, user, settings)
    return user, raw


async def create_password_reset_token_for_user(
    session: AsyncSession, user: User, settings: Settings
) -> str:
    """Admin-triggered variant — the caller already has the User loaded (e.g. by
    id), so there's no email lookup or enumeration concern to guard against."""
    return await _issue_password_reset_token(session, user, settings)


def _as_utc(value: datetime) -> datetime:
    """Normalise DB datetimes so expiry checks are timezone-safe."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


async def _get_valid_password_reset_token(session: AsyncSession, raw_token: str) -> UserToken:
    """Return an unused, unexpired password-reset token or raise AppError."""
    token_hash = hash_token(raw_token)
    result = await session.execute(
        select(UserToken).where(
            UserToken.token_hash == token_hash,
            UserToken.purpose == TokenPurpose.password_reset,
        )
    )
    record = result.scalar_one_or_none()
    now = datetime.now(UTC)
    if record is None:
        raise AppError(
            "Link has expired or is no longer valid",
            code="reset_token_invalid",
        )
    # Expiry first so superseded (force-expired) and timed-out links share a
    # clear "expired" message (GJ-FP-020 / GJ-FP-046).
    if _as_utc(record.expires_at) <= now:
        raise AppError(
            "Reset link has expired",
            code="reset_token_expired",
        )
    if record.used_at is not None:
        raise AppError(
            "This reset link has already been used",
            code="reset_token_used",
        )
    return record


async def validate_password_reset_token(session: AsyncSession, raw_token: str) -> None:
    """Confirm a reset link is still usable (for FE page-load checks). Does not consume it."""
    record = await _get_valid_password_reset_token(session, raw_token)
    user = await session.get(User, record.user_id)
    if user is None:
        raise AppError(
            "Link has expired or is no longer valid",
            code="reset_token_invalid",
        )
    _assert_bound_token_version(
        record,
        user,
        message="Reset link has expired",
        code="reset_token_expired",
    )
    assert_password_reset_allowed(user)


async def reset_password(
    session: AsyncSession, raw_token: str, new_password: str, settings: Settings
) -> User:
    """Validate token, hash the new password, and invalidate every active session.

    Completing a reset proves email ownership, so ``email_verified_at`` is set when
    still unset — the user can sign in immediately without a separate verify step.
    Refresh tokens are revoked and ``token_version`` is bumped so existing access
    JWTs fail auth on the next request (immediate logout everywhere). The reset
    token is marked used + expired so the same link cannot be reused.
    """
    record = await _get_valid_password_reset_token(session, raw_token)
    now = datetime.now(UTC)

    user = await session.get(User, record.user_id)
    if user is None:
        raise AuthenticationError("User not found")
    _assert_bound_token_version(
        record,
        user,
        message="Reset link has expired",
        code="reset_token_expired",
    )
    assert_password_reset_allowed(user)

    if user.hashed_password and verify_password(new_password, user.hashed_password):
        raise ConflictError("New password must be different from your current password")

    user.hashed_password = hash_password(new_password)
    if user.email_verified_at is None:
        user.email_verified_at = now
        # Drop any outstanding verify links — account is verified via reset.
        await _revoke_unused_email_verification_tokens(session, user.id)
    # Single-use: mark consumed and force-expire so a second submit fails validation.
    record.used_at = now
    record.expires_at = now
    session.add(user)
    session.add(record)
    await invalidate_sessions(session, user)
    await session.flush()
    await session.refresh(user)
    return user
