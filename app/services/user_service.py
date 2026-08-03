"""User business logic / data-access layer."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import AuthenticationError, ConflictError
from app.core.file_storage import resolve_media_url
from app.core.security import hash_password, verify_password
from app.models.advisor_profile import AdvisorProfile
from app.models.notification import NotificationEntityType, NotificationType
from app.models.seeker_profile import SeekerProfile
from app.models.user import (
    AuthProvider,
    SignupSource,
    User,
    UserRole,
    VerificationStatus,
)
from app.schemas.advisor import AdvisorCreate
from app.schemas.user import UserCreate, UserRead, UserUpdate
from app.services import notification_service

# Pre-computed Argon2id hash used as a timing sentinel when a login email is
# not found.  Always verifying against a real hash prevents response-time
# differences from leaking whether an email is registered.
_TIMING_SENTINEL_HASH: str = hash_password("__timing_sentinel__")


async def get_by_id(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    return await session.get(User, user_id)


async def get_by_email(session: AsyncSession, email: str) -> User | None:
    result = await session.execute(select(User).where(User.email == email))
    return result.scalar_one_or_none()


async def profile_photo_key(session: AsyncSession, user: User) -> str | None:
    """Raw stored photo key for seeker/advisor profiles; admins have none."""
    if user.role == UserRole.seeker:
        seeker_profile = (
            await session.execute(select(SeekerProfile).where(SeekerProfile.user_id == user.id))
        ).scalar_one_or_none()
        return seeker_profile.profile_photo_url if seeker_profile else None
    if user.role == UserRole.advisor:
        advisor_profile = (
            await session.execute(select(AdvisorProfile).where(AdvisorProfile.user_id == user.id))
        ).scalar_one_or_none()
        return advisor_profile.profile_photo_url if advisor_profile else None
    return None


async def build_user_read(session: AsyncSession, user: User, settings: Settings) -> UserRead:
    """``UserRead`` with resolved ``profile_photo_url`` for session/sidebar UI."""
    base = UserRead.model_validate(user)
    return base.model_copy(
        update={
            "profile_photo_url": resolve_media_url(await profile_photo_key(session, user), settings)
        }
    )


async def _notify_admins_user_registered(session: AsyncSession, user: User) -> None:
    await notification_service.notify_admins(
        session,
        type=NotificationType.user_registered,
        title="New user registered",
        body=f"{user.full_name or user.email} signed up as {user.role.value}",
        entity_type=NotificationEntityType.user,
        entity_id=user.id,
        actor_id=user.id,
    )


async def create_user(session: AsyncSession, data: UserCreate) -> User:
    if await get_by_email(session, data.email):
        raise ConflictError("A user with this email already exists")
    user = User(
        email=data.email,
        full_name=data.full_name,
        hashed_password=hash_password(data.password),
        role=UserRole.seeker,  # role is always seeker for self-registration
        signup_source=data.signup_source or SignupSource.organic,
    )
    session.add(user)
    await session.flush()
    await session.refresh(user)
    await _notify_admins_user_registered(session, user)
    return user


async def create_advisor(session: AsyncSession, data: AdvisorCreate) -> User:
    """Register an advisor account.  Inactive until admin approves."""
    if await get_by_email(session, data.email):
        raise ConflictError("A user with this email already exists")
    user = User(
        email=data.email,
        full_name=data.full_name,
        hashed_password=hash_password(data.password),
        role=UserRole.advisor,
        is_active=False,  # cannot login until admin approves
        verification_status=VerificationStatus.pending,
        signup_source=data.signup_source or SignupSource.organic,
    )
    session.add(user)
    await session.flush()
    await session.refresh(user)
    await _notify_admins_user_registered(session, user)
    return user


def _guard_google_login(user: User, requested_role: UserRole | None) -> None:
    """Enforce role match and advisor-rejection for a Google sign-in.

    One account per email: a seeker cannot sign in through the advisor button and
    vice versa, and admin accounts never sign in via Google. A ``None``
    ``requested_role`` (role-less start) skips the match — the account signs in
    as whatever role it already has — but stays limited to seeker/advisor.
    """
    if user.role not in (UserRole.seeker, UserRole.advisor):
        raise AuthenticationError("This account cannot sign in with Google")
    if requested_role is not None and user.role != requested_role:
        raise ConflictError(
            f"This email is already registered as a {user.role.value}. "
            f"Please sign in as a {user.role.value}."
        )
    if user.role == UserRole.advisor and user.verification_status == VerificationStatus.rejected:
        raise AuthenticationError(
            "Your account was rejected by an admin. Please contact support."
        )


async def get_or_create_google_user(
    session: AsyncSession,
    email: str,
    full_name: str | None,
    role: UserRole | None,
) -> User:
    """Resolve a Google identity to a local User (auto-link or create).

    ``role`` is the role requested on the frontend (signed OAuth state or the
    ``/auth/google/complete`` body). ``None`` means "sign in as whatever role the
    account already has" — valid only for existing accounts; creating a new one
    requires a concrete seeker/advisor role.
    """
    if role is not None and role not in (UserRole.seeker, UserRole.advisor):
        raise AuthenticationError("Unsupported role for Google sign-in")

    now = datetime.now(UTC)

    existing = await get_by_email(session, email)
    if existing is not None:
        _guard_google_login(existing, role)
        # Auto-link: Google verifies the email, so stamp it if not already set.
        if existing.email_verified_at is None:
            existing.email_verified_at = now
            session.add(existing)
            await session.flush()
        return existing


    if role is None:
        raise AuthenticationError("A role is required to create a Google account")


    user = User(
        email=email,
        full_name=full_name,
        hashed_password=None,
        role=role,
        auth_provider=AuthProvider.google,
        email_verified_at=now,
        signup_source=SignupSource.organic,
    )
    if role == UserRole.advisor:
        # Mirror create_advisor: inactive until an admin approves.
        user.is_active = False
        user.verification_status = VerificationStatus.pending

    try:
        async with session.begin_nested():
            session.add(user)
            await session.flush()
    except IntegrityError:
        # Lost the race — another request created this email first. Re-read and
        # treat it as an auto-link.
        winner = await get_by_email(session, email)
        if winner is None:  # pragma: no cover - only if the conflict was unrelated
            raise
        _guard_google_login(winner, role)
        if winner.email_verified_at is None:
            winner.email_verified_at = now
            session.add(winner)
            await session.flush()
        return winner

    await session.refresh(user)
    # Create branch only — an existing-account Google sign-in must not notify.
    await _notify_admins_user_registered(session, user)
    return user


async def update_user(session: AsyncSession, user: User, data: UserUpdate) -> User:
    if data.full_name is not None:
        user.full_name = data.full_name
    if data.password is not None:
        user.hashed_password = hash_password(data.password)
    user.updated_by = user.id
    session.add(user)
    await session.flush()
    await session.refresh(user)
    return user


async def authenticate(session: AsyncSession, email: str, password: str) -> User:
    """Authenticate a user.

    Always runs the password hash comparison even when the user is not found to
    prevent timing-based email enumeration.
    Raises :class:`AuthenticationError` on any failure.
    """
    user = await get_by_email(session, email)
    # Always run the Argon2 check — use the real hash when the user exists and
    # has one, a pre-computed sentinel otherwise — so response time is constant.
    # A Google-only account has ``hashed_password is None``; falling back to the
    # sentinel keeps ``verify_password`` from crashing on ``None`` and makes such
    # an account indistinguishable from a wrong password (clean 401, no leak).
    candidate_hash = (
        user.hashed_password
        if user is not None and user.hashed_password is not None
        else _TIMING_SENTINEL_HASH
    )
    password_ok = verify_password(password, candidate_hash)
    if user is None or not password_ok:
        raise AuthenticationError("Incorrect email or password")
    if getattr(user, "is_suspended", False) or (
        user.verification_status == VerificationStatus.suspended
    ):
        raise AuthenticationError("Account is suspended")
    if user.role == UserRole.advisor and user.verification_status == VerificationStatus.rejected:
        raise AuthenticationError("Your account was rejected by an admin. Please contact support.")
    if not user.is_active:
        if user.role == UserRole.advisor and user.verification_status in (
            VerificationStatus.pending,
            VerificationStatus.under_review,
        ):
            # Pending / under-review advisors can log in to complete onboarding
            # or view Approval Pending. Marketplace actions stay gated by
            # require_verified_advisor.
            return user
        if user.role == UserRole.advisor:
            raise AuthenticationError("Advisor account pending verification")
        raise AuthenticationError("Account is inactive")
    return user
