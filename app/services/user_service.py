"""User business logic / data-access layer."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AuthenticationError, ConflictError
from app.core.security import hash_password, verify_password
from app.models.user import SignupSource, User, UserRole, VerificationStatus
from app.schemas.advisor import AdvisorCreate
from app.schemas.user import UserCreate, UserUpdate

# Pre-computed Argon2id hash used as a timing sentinel when a login email is
# not found.  Always verifying against a real hash prevents response-time
# differences from leaking whether an email is registered.
_TIMING_SENTINEL_HASH: str = hash_password("__timing_sentinel__")


async def get_by_id(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    return await session.get(User, user_id)


async def get_by_email(session: AsyncSession, email: str) -> User | None:
    result = await session.execute(select(User).where(User.email == email))
    return result.scalar_one_or_none()


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
    # Always run the Argon2 check — use the real hash when the user exists,
    # a pre-computed sentinel otherwise — so response time is constant.
    candidate_hash = user.hashed_password if user is not None else _TIMING_SENTINEL_HASH
    password_ok = verify_password(password, candidate_hash)
    if user is None or not password_ok:
        raise AuthenticationError("Incorrect email or password")
    if not user.is_active:
        if user.role == UserRole.advisor and user.verification_status == VerificationStatus.pending:
            # Pending advisors can log in to complete their onboarding profile and
            # upload credentials, but remain gated by require_verified_advisor on
            # externally-facing actions (bookings, availability, etc.).
            return user
        if user.role == UserRole.advisor:
            raise AuthenticationError("Advisor account pending verification")
        raise AuthenticationError("Account is inactive")
    return user
