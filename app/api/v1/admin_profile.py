"""Admin profile endpoints: GET / PATCH /admins/me/profile."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import CurrentUser, RequestIdDep, SettingsDep, require_role
from app.db.session import SessionDep
from app.models.user import UserRole
from app.schemas.admin_profile import AdminProfileRead, AdminProfileUpdate
from app.schemas.response import Meta, ResponseEnvelope
from app.services import admin_profile_service

router = APIRouter(prefix="/admins", tags=["admin-profile"])


@router.get(
    "/me/profile",
    response_model=ResponseEnvelope[AdminProfileRead],
    dependencies=[Depends(require_role(UserRole.admin))],
)
async def get_my_admin_profile(
    current_user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[AdminProfileRead]:
    profile = await admin_profile_service.get_or_create(session, current_user.id)
    data = admin_profile_service.build_read(profile, current_user, settings)
    return ResponseEnvelope[AdminProfileRead](data=data, meta=Meta(request_id=request_id))


@router.patch(
    "/me/profile",
    response_model=ResponseEnvelope[AdminProfileRead],
    dependencies=[Depends(require_role(UserRole.admin))],
)
async def update_my_admin_profile(
    body: AdminProfileUpdate,
    current_user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[AdminProfileRead]:
    profile = await admin_profile_service.get_or_create(session, current_user.id)
    profile = await admin_profile_service.update(session, profile, body)
    data = admin_profile_service.build_read(profile, current_user, settings)
    return ResponseEnvelope[AdminProfileRead](data=data, meta=Meta(request_id=request_id))
