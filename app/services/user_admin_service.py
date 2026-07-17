"""Admin "User Management" — broad, all-roles account directory and actions
(verify account, trigger password reset). Suspend/Reactivate are handled by
the existing /admin/users/{id}/suspend|reactivate endpoints, unchanged.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import Select, String, and_, cast, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.core.config import Settings
from app.core.exceptions import NotFoundError
from app.models.seeker_profile import SeekerProfile
from app.models.user import User, UserRole, VerificationStatus
from app.schemas.user_admin import AccountStatus, UserDetailRead, UserListRead
from app.services import auth_service
from app.services.email_service import send_password_reset_email

# Advisors register with is_active=False until admin approves — that is
# onboarding, not a soft-suspend. Mirror the login gate in user_service.
_ADVISOR_ONBOARDING = and_(
    User.role == UserRole.advisor,
    User.verification_status.in_(
        (VerificationStatus.pending, VerificationStatus.under_review)
    ),
)


def _is_advisor_onboarding(user: User) -> bool:
    return user.role == UserRole.advisor and user.verification_status in (
        VerificationStatus.pending,
        VerificationStatus.under_review,
    )


def compute_status(user: User) -> AccountStatus:
    """Map DB flags → admin list status.

    ``is_suspended`` / ``verification_status=suspended`` mark an intentional
    admin soft-suspend and always win. Pending/under-review advisors who are
    merely inactive for onboarding are not treated as suspended.
    """
    onboarding = _is_advisor_onboarding(user)
    suspended = bool(getattr(user, "is_suspended", False)) or (
        user.verification_status == VerificationStatus.suspended
    )
    if suspended or (not user.is_active and not onboarding):
        return AccountStatus.suspended
    if user.email_verified_at is None:
        return AccountStatus.unverified
    return AccountStatus.verified


def user_search_clause(
    search: str | None = None,
    *,
    username: str | None = None,
    user_id: uuid.UUID | None = None,
) -> ColumnElement[bool] | None:
    """Match users by free-text ``search`` (name / email / id), and/or exact filters.

    ``search`` matches ``full_name``, ``email``, and stringified ``id`` (UUID).
    ``username`` is an ilike filter on ``full_name`` only.
    ``user_id`` is an exact UUID match.
    """
    clauses: list[ColumnElement[bool]] = []
    if search and (q := search.strip()):
        pattern = f"%{q}%"
        text_match = or_(
            User.full_name.ilike(pattern),
            User.email.ilike(pattern),
            cast(User.id, String).ilike(pattern),
        )
        try:
            uid = uuid.UUID(q)
            clauses.append(or_(text_match, User.id == uid))
        except ValueError:
            clauses.append(text_match)
    if username and (name := username.strip()):
        clauses.append(User.full_name.ilike(f"%{name}%"))
    if user_id is not None:
        clauses.append(User.id == user_id)
    if not clauses:
        return None
    return and_(*clauses) if len(clauses) > 1 else clauses[0]


def list_users_stmt(
    search: str | None,
    status: AccountStatus | None,
    role: UserRole | None,
    *,
    username: str | None = None,
    user_id: uuid.UUID | None = None,
) -> Select[tuple[User]]:
    stmt = select(User).where(User.role != UserRole.admin).order_by(User.created_at.desc())
    if role is not None:
        stmt = stmt.where(User.role == role)
    clause = user_search_clause(search, username=username, user_id=user_id)
    if clause is not None:
        stmt = stmt.where(clause)
    if status == AccountStatus.suspended:
        stmt = stmt.where(
            or_(
                User.is_suspended.is_(True),
                User.verification_status == VerificationStatus.suspended,
                and_(User.is_active.is_(False), not_(_ADVISOR_ONBOARDING)),
            )
        )
    elif status == AccountStatus.unverified:
        stmt = stmt.where(
            User.is_suspended.is_(False),
            User.verification_status.is_distinct_from(VerificationStatus.suspended),
            User.email_verified_at.is_(None),
            or_(User.is_active.is_(True), _ADVISOR_ONBOARDING),
        )
    elif status == AccountStatus.verified:
        stmt = stmt.where(
            User.is_suspended.is_(False),
            User.verification_status.is_distinct_from(VerificationStatus.suspended),
            User.email_verified_at.is_not(None),
            or_(User.is_active.is_(True), _ADVISOR_ONBOARDING),
        )
    return stmt


async def build_list_read(session: AsyncSession, users: list[User]) -> list[UserListRead]:
    """Bulk-enrich a page of Users with country (seekers only) — a single
    id-scoped query for the page, not one query per row."""
    seeker_ids = [u.id for u in users if u.role == UserRole.seeker]
    profiles: dict[uuid.UUID, SeekerProfile] = {}
    if seeker_ids:
        rows = (
            (
                await session.execute(
                    select(SeekerProfile).where(SeekerProfile.user_id.in_(seeker_ids))
                )
            )
            .scalars()
            .all()
        )
        profiles = {p.user_id: p for p in rows}
    return [
        UserListRead(
            id=u.id,
            full_name=u.full_name,
            email=u.email,
            role=u.role,
            user_type=u.role,
            country_of_residence=profiles[u.id].country_of_residence if u.id in profiles else None,
            status=compute_status(u),
            created_at=u.created_at,
        )
        for u in users
    ]


async def get_user_detail(session: AsyncSession, user_id: uuid.UUID) -> UserDetailRead:
    user = await session.get(User, user_id)
    if user is None or user.role == UserRole.admin:
        raise NotFoundError("User not found")
    profile = None
    if user.role == UserRole.seeker:
        profile = (
            await session.execute(select(SeekerProfile).where(SeekerProfile.user_id == user_id))
        ).scalar_one_or_none()
    return UserDetailRead(
        id=user.id,
        full_name=user.full_name,
        email=user.email,
        role=user.role,
        user_type=user.role,
        country_of_residence=profile.country_of_residence if profile else None,
        status=compute_status(user),
        created_at=user.created_at,
        verification_status=user.verification_status if user.role == UserRole.advisor else None,
    )


async def verify_account(session: AsyncSession, user_id: uuid.UUID, admin_id: uuid.UUID) -> User:
    """Mark email verified. For advisors, also set ``verification_status=approved``
    (frontend badge) and activate the account."""
    user = await session.get(User, user_id)
    if user is None:
        raise NotFoundError("User not found")
    user.email_verified_at = datetime.now(UTC)
    user.updated_by = admin_id
    if user.role == UserRole.advisor:
        user.verification_status = VerificationStatus.approved
        user.pre_suspend_verification_status = None
        user.is_suspended = False
        user.is_active = True
    session.add(user)
    await session.flush()
    await session.refresh(user)
    return user


async def suspend_account(session: AsyncSession, user_id: uuid.UUID) -> User:
    """Soft-suspend. For advisors, sets ``verification_status=suspended`` so the
    frontend badge updates, preserving the prior status for reactivate."""
    user = await session.get(User, user_id)
    if user is None:
        raise NotFoundError("User not found")
    if user.role == UserRole.advisor and user.verification_status != VerificationStatus.suspended:
        user.pre_suspend_verification_status = user.verification_status
        user.verification_status = VerificationStatus.suspended
    user.is_active = False
    user.is_suspended = True
    session.add(user)
    await session.flush()
    await session.refresh(user)
    return user


async def reactivate_account(session: AsyncSession, user_id: uuid.UUID) -> User:
    """Clear soft-suspend and restore the pre-suspend ``verification_status``."""
    user = await session.get(User, user_id)
    if user is None:
        raise NotFoundError("User not found")
    user.is_suspended = False
    if user.role == UserRole.advisor:
        restored = user.pre_suspend_verification_status
        if restored is None or restored == VerificationStatus.suspended:
            restored = VerificationStatus.pending
        user.verification_status = restored
        user.pre_suspend_verification_status = None
        user.is_active = restored == VerificationStatus.approved
    else:
        user.is_active = True
    session.add(user)
    await session.flush()
    await session.refresh(user)
    return user


async def trigger_password_reset(
    session: AsyncSession, user_id: uuid.UUID, settings: Settings
) -> None:
    user = await session.get(User, user_id)
    if user is None:
        raise NotFoundError("User not found")
    raw_token = await auth_service.create_password_reset_token_for_user(session, user, settings)
    await send_password_reset_email(user.email, user.full_name or "", raw_token, settings)
