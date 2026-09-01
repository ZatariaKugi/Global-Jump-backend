"""Advisor service offerings — role-based CRUD endpoints.

Permissions:
  - Admin: full CRUD on any advisor's services
  - Advisor: get own services, update price/duration on own services
  - Seeker/public: get an advisor's services (via public profile)
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends

from app.api.deps import (
    CurrentUser,
    RequestIdDep,
    require_role,
)
from app.core.exceptions import PermissionDeniedError
from app.db.session import SessionDep
from app.models.user import UserRole
from app.schemas.advisor_profile import (
    ServiceCreateRequest,
    ServiceRead,
    ServiceUpdateRequest,
)
from app.schemas.response import Meta, ResponseEnvelope
from app.services import advisor_service_service

router = APIRouter(tags=["advisor-services"])


# ── Advisor: get own services ───────────────────────────────────────────
# NOTE: /me routes MUST come before /{advisor_id} to avoid "me" being
# parsed as a UUID path parameter.


@router.get(
    "/advisors/me/services",
    response_model=ResponseEnvelope[list[ServiceRead]],
    dependencies=[Depends(require_role(UserRole.advisor,UserRole.seeker))],
)
async def get_my_services(
    current_user: CurrentUser,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[list[ServiceRead]]:
    services = await advisor_service_service.list_services(session, current_user.id)
    return ResponseEnvelope[list[ServiceRead]](
        data=services,
        meta=Meta(request_id=request_id),
    )


# ── Advisor: update price/duration on own service ───────────────────────


@router.patch(
    "/advisors/me/services/{service_id}",
    response_model=ResponseEnvelope[ServiceRead],
    dependencies=[Depends(require_role(UserRole.advisor))],
)
async def update_my_service(
    service_id: uuid.UUID,
    data: ServiceUpdateRequest,
    current_user: CurrentUser,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[ServiceRead]:
    if data.service_type is not None:
        raise PermissionDeniedError(
            "Advisors cannot change service type; ask an admin to recreate the service"
        )
    result = await advisor_service_service.update_service(
        session, current_user.id, service_id, data, actor_id=current_user.id
    )
    return ResponseEnvelope[ServiceRead](
        data=result,
        meta=Meta(request_id=request_id),
    )


# ── Public: list services for any advisor (no auth required) ─────────────


@router.get(
    "/advisors/{advisor_id}/services",
    response_model=ResponseEnvelope[list[ServiceRead]],
)
async def get_advisor_services(
    advisor_id: uuid.UUID,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[list[ServiceRead]]:
    services = await advisor_service_service.list_services(session, advisor_id)
    return ResponseEnvelope[list[ServiceRead]](
        data=services,
        meta=Meta(request_id=request_id),
    )


# ── Admin: create service for any advisor ───────────────────────────────


@router.post(
    "/admin/advisors/{advisor_id}/services",
    response_model=ResponseEnvelope[ServiceRead],
    status_code=201,
    dependencies=[Depends(require_role(UserRole.admin))],
)
async def admin_create_service(
    advisor_id: uuid.UUID,
    data: ServiceCreateRequest,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[ServiceRead]:
    result = await advisor_service_service.create_service(
        session, advisor_id, data, actor_id=None
    )
    return ResponseEnvelope[ServiceRead](
        data=result,
        meta=Meta(request_id=request_id),
    )


# ── Admin: update any service ───────────────────────────────────────────


@router.patch(
    "/admin/advisors/{advisor_id}/services/{service_id}",
    response_model=ResponseEnvelope[ServiceRead],
    dependencies=[Depends(require_role(UserRole.admin))],
)
async def admin_update_service(
    advisor_id: uuid.UUID,
    service_id: uuid.UUID,
    data: ServiceUpdateRequest,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[ServiceRead]:
    result = await advisor_service_service.update_service(
        session, advisor_id, service_id, data, actor_id=None
    )
    return ResponseEnvelope[ServiceRead](
        data=result,
        meta=Meta(request_id=request_id),
    )


# ── Admin: delete any service ───────────────────────────────────────────


@router.delete(
    "/admin/advisors/{advisor_id}/services/{service_id}",
    status_code=204,
    dependencies=[Depends(require_role(UserRole.admin))],
)
async def admin_delete_service(
    advisor_id: uuid.UUID,
    service_id: uuid.UUID,
    session: SessionDep,
) -> None:
    await advisor_service_service.delete_service(
        session, advisor_id, service_id, actor_id=None
    )
