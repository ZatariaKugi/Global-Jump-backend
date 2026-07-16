"""Admin user impersonation — issue a short-lived token as another user."""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import NotFoundError, PermissionDeniedError
from app.core.logging import get_logger
from app.core.security import create_access_token
from app.models.user import User, UserRole, VerificationStatus
from app.schemas.impersonation import ImpersonationRead
from app.schemas.user import UserRead

logger = get_logger(__name__)


async def impersonate(
    session: AsyncSession,
    *,
    target_user_id: uuid.UUID,
    admin: User,
    settings: Settings,
) -> ImpersonationRead:
    """Issue an access token for ``target_user_id`` on behalf of ``admin``.

    Guards:
    - target must exist
    - cannot impersonate yourself
    - cannot impersonate another admin
    - target must be active (or an onboarding advisor still pending/under review)
    """
    target = await session.get(User, target_user_id)
    if target is None:
        raise NotFoundError("User not found")

    if target.id == admin.id:
        raise PermissionDeniedError("Cannot impersonate yourself")

    if target.role == UserRole.admin:
        raise PermissionDeniedError("Cannot impersonate an admin account")

    advisor_onboarding = target.role == UserRole.advisor and target.verification_status in (
        VerificationStatus.pending,
        VerificationStatus.under_review,
    )
    if not target.is_active and not advisor_onboarding:
        raise PermissionDeniedError("Cannot impersonate an inactive account")

    expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        subject=target.id,
        settings=settings,
        expires_delta=expires,
        extra_claims={
            "role": target.role.value,
            "email_verified": target.is_email_verified,
            "impersonated_by": str(admin.id),
            "imp": True,
        },
    )

    logger.info(
        "user_impersonated",
        admin_id=str(admin.id),
        target_user_id=str(target.id),
        target_role=target.role.value,
    )

    return ImpersonationRead(
        access_token=access_token,
        expires_in=int(expires.total_seconds()),
        role=target.role,
        verification_status=target.verification_status,
        user=UserRead.model_validate(target),
        impersonated_by=admin.id,
    )
