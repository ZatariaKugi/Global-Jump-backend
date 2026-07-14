"""Admin "User Management" — broad, all-roles account directory and actions
(verify account, trigger password reset). Suspend/Reactivate are handled by
the existing /admin/users/{id}/suspend|activate endpoints, unchanged.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import Select, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import NotFoundError
from app.models.seeker_profile import SeekerProfile
from app.models.user import User, UserRole
from app.schemas.user_admin import AccountStatus, UserDetailRead, UserListRead
from app.services import auth_service
from app.services.email_service import send_password_reset_email


def compute_status(user: User) -> AccountStatus:
    if not user.is_active:
        return AccountStatus.suspended
    if user.email_verified_at is None:
        return AccountStatus.unverified
    return AccountStatus.verified


def list_users_stmt(
    search: str | None, status: AccountStatus | None, role: UserRole | None
) -> Select[tuple[User]]:
    stmt = select(User).where(User.role != UserRole.admin).order_by(User.created_at.desc())
    if role is not None:
        stmt = stmt.where(User.role == role)
    if search:
        pattern = f"%{search.strip()}%"
        stmt = stmt.where(or_(User.full_name.ilike(pattern), User.email.ilike(pattern)))
    if status == AccountStatus.suspended:
        stmt = stmt.where(User.is_active.is_(False))
    elif status == AccountStatus.unverified:
        stmt = stmt.where(User.is_active.is_(True), User.email_verified_at.is_(None))
    elif status == AccountStatus.verified:
        stmt = stmt.where(User.is_active.is_(True), User.email_verified_at.is_not(None))
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
        country_of_residence=profile.country_of_residence if profile else None,
        status=compute_status(user),
        created_at=user.created_at,
        verification_status=user.verification_status if user.role == UserRole.advisor else None,
    )


async def verify_account(session: AsyncSession, user_id: uuid.UUID, admin_id: uuid.UUID) -> User:
    user = await session.get(User, user_id)
    if user is None:
        raise NotFoundError("User not found")
    user.email_verified_at = datetime.now(UTC)
    user.updated_by = admin_id
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
