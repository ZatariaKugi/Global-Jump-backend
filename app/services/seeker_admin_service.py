"""Admin "Visa Seeker Management" — seeker-specific enriched list/detail plus
the admin-invite "Add Visa Seeker" flow.
"""

from __future__ import annotations

import secrets
import uuid

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.countries import country_name
from app.core.exceptions import ConflictError, NotFoundError
from app.core.security import hash_password
from app.core.visa_types import parse_visa_type, visa_type_name
from app.models.assessment import Assessment
from app.models.booking import Booking
from app.models.seeker_profile import SeekerProfile
from app.models.user import User, UserRole
from app.models.visa_type import VisaType
from app.schemas.seeker_admin import SeekerCreate, SeekerDetailRead, SeekerListRead
from app.schemas.user_admin import AccountStatus
from app.services import auth_service, seeker_profile_service, user_admin_service, user_service
from app.services.email_service import send_password_reset_email


def list_seekers_stmt(
    search: str | None,
    status: AccountStatus | None,
    study_visa: str | None,
    visa_type: VisaType | None = None,
) -> Select[tuple[User]]:
    """Same filter shape as user_admin_service.list_users_stmt, scoped to seekers.

    ``visa_type`` is the PRD enum. ``study_visa`` remains a legacy alias that
    resolves via ``parse_visa_type`` (e.g. ``study`` → ``student``).
    """
    stmt = select(User).where(User.role == UserRole.seeker).order_by(User.created_at.desc())
    effective = visa_type or parse_visa_type(study_visa)
    if effective is not None:
        stmt = stmt.join(SeekerProfile, SeekerProfile.user_id == User.id).where(
            func.lower(SeekerProfile.intended_visa_type) == effective.value
        )
    if search:
        pattern = f"%{search.strip()}%"
        stmt = stmt.where(or_(User.full_name.ilike(pattern), User.email.ilike(pattern)))
    if status == AccountStatus.suspended:
        stmt = stmt.where(User.is_suspended.is_(True))
    elif status == AccountStatus.unverified:
        stmt = stmt.where(
            User.is_suspended.is_(False),
            User.is_active.is_(True),
            User.email_verified_at.is_(None),
        )
    elif status == AccountStatus.verified:
        stmt = stmt.where(
            User.is_suspended.is_(False),
            User.is_active.is_(True),
            User.email_verified_at.is_not(None),
        )
    return stmt


async def build_list_read(session: AsyncSession, users: list[User]) -> list[SeekerListRead]:
    """Bulk-enrich one page: profiles + AI Assessment Count + Total Bookings,
    each a single grouped query keyed by the page's user ids — not N+1."""
    ids = [u.id for u in users]
    if not ids:
        return []
    profiles = {
        p.user_id: p
        for p in (
            (await session.execute(select(SeekerProfile).where(SeekerProfile.user_id.in_(ids))))
            .scalars()
            .all()
        )
    }
    ai_count_rows = (
        await session.execute(
            select(Assessment.user_id, func.count())
            .where(Assessment.user_id.in_(ids))
            .group_by(Assessment.user_id)
        )
    ).all()
    ai_counts: dict[uuid.UUID, int] = {}
    for assessment_user_id, count in ai_count_rows:
        ai_counts[assessment_user_id] = count

    booking_count_rows = (
        await session.execute(
            select(Booking.seeker_id, func.count())
            .where(Booking.seeker_id.in_(ids))
            .group_by(Booking.seeker_id)
        )
    ).all()
    booking_counts: dict[uuid.UUID, int] = {}
    for booking_seeker_id, count in booking_count_rows:
        booking_counts[booking_seeker_id] = count
    result = []
    for u in users:
        residence = profiles[u.id].country_of_residence if u.id in profiles else None
        result.append(
            SeekerListRead(
                id=u.id,
                full_name=u.full_name,
                email=u.email,
                country_of_residence=residence,
                country_of_residence_name=country_name(residence),
                intended_visa_type=profiles[u.id].intended_visa_type if u.id in profiles else None,
                intended_visa_type_name=visa_type_name(
                    profiles[u.id].intended_visa_type if u.id in profiles else None
                ),
                status=user_admin_service.compute_status(u),
                ai_assessment_count=ai_counts.get(u.id, 0),
                total_bookings=booking_counts.get(u.id, 0),
                created_at=u.created_at,
            )
        )
    return result


async def get_seeker_detail(session: AsyncSession, user_id: uuid.UUID) -> SeekerDetailRead:
    """Single-record equivalent of build_list_read — reuses
    seeker_profile_service.get_or_create so a seeker with no profile row yet
    still returns a valid (mostly-null) detail response instead of 404ing on
    the profile half."""
    user = await session.get(User, user_id)
    if user is None or user.role != UserRole.seeker:
        raise NotFoundError("Seeker not found")
    profile = await seeker_profile_service.get_or_create(session, user_id)
    ai_count = (
        await session.execute(
            select(func.count()).select_from(Assessment).where(Assessment.user_id == user_id)
        )
    ).scalar_one()
    total_bookings = (
        await session.execute(
            select(func.count()).select_from(Booking).where(Booking.seeker_id == user_id)
        )
    ).scalar_one()
    return SeekerDetailRead(
        id=user.id,
        full_name=user.full_name,
        email=user.email,
        country_of_residence=profile.country_of_residence,
        country_of_residence_name=country_name(profile.country_of_residence),
        intended_visa_type=profile.intended_visa_type,
        intended_visa_type_name=visa_type_name(profile.intended_visa_type),
        status=user_admin_service.compute_status(user),
        ai_assessment_count=ai_count,
        total_bookings=total_bookings,
        created_at=user.created_at,
        nationality=profile.nationality,
        nationality_name=country_name(profile.nationality),
        intended_destination=profile.intended_destination,
        intended_destination_name=country_name(profile.intended_destination),
        education_level=profile.education_level,
        employment_status=profile.employment_status,
    )


async def create_seeker(
    session: AsyncSession, data: SeekerCreate, settings: Settings
) -> SeekerDetailRead:
    """Admin-invite flow: the admin never sets a password. A random,
    never-surfaced password is hashed and stored so the account satisfies
    User.hashed_password's NOT NULL constraint; the seeker sets their real
    password via the same reset-token email used by 'Reset Password'."""
    if await user_service.get_by_email(session, data.email) is not None:
        raise ConflictError("A user with this email already exists")
    user = User(
        email=data.email,
        full_name=data.full_name,
        hashed_password=hash_password(secrets.token_urlsafe(32)),
        role=UserRole.seeker,
    )
    session.add(user)
    await session.flush()
    if data.country_of_residence or data.intended_visa_type:
        session.add(
            SeekerProfile(
                user_id=user.id,
                country_of_residence=data.country_of_residence,
                intended_visa_type=data.intended_visa_type,
            )
        )
        await session.flush()
    await session.refresh(user)
    raw_token = await auth_service.create_password_reset_token_for_user(session, user, settings)
    await send_password_reset_email(user.email, user.full_name or "", raw_token, settings)
    return await get_seeker_detail(session, user.id)
