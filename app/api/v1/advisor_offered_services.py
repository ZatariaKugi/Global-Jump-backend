"""Advisor offered services — ``advisor_offered_services`` with optional price/duration.

Permissions:
  - Seeker / public: list services (``id`` + ``service_type`` only — no price/duration)
  - Advisor: list + replace/update own offered services (includes price/duration)
  - Admin: full CRUD on any advisor's offered services

Distinct from priced bookable ``advisor_services`` (see ``advisor_services.py``).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Query

from app.api.deps import CurrentUser, RequestIdDep, require_role
from app.db.session import SessionDep
from app.models.user import UserRole
from app.schemas.advisor_profile import (
    AdminOfferedServiceCreateRequest,
    AdminOfferedServiceRead,
    OfferedServiceCreateRequest,
    OfferedServicePublicRead,
    OfferedServiceRead,
    OfferedServicesReplaceRequest,
    OfferedServiceUpdateRequest,
)
from app.schemas.response import Meta, ResponseEnvelope
from app.services import advisor_offered_service_service

router = APIRouter(tags=["advisor_offered_services"])


# ── Seeker / public: catalog (no price / duration) ──────────────────────


@router.get(
    "/offered_services",
    response_model=ResponseEnvelope[list[OfferedServicePublicRead]],
)
async def list_all_offered_services(
    session: SessionDep,
    request_id: RequestIdDep,
    service_type: Annotated[str | None, Query()] = None,
) -> ResponseEnvelope[list[OfferedServicePublicRead]]:
    """Seeker/public catalog — same global rows admin manages (id + service_type)."""
    items = await advisor_offered_service_service.list_all_public(
        session, service_type=service_type
    )
    return ResponseEnvelope[list[OfferedServicePublicRead]](
        data=items,
        meta=Meta(request_id=request_id),
    )


# ── Advisor: own offered services (/me MUST come before /{advisor_id}) ──


@router.get(
    "/advisors/me/offered_services",
    response_model=ResponseEnvelope[list[OfferedServiceRead]],
    dependencies=[Depends(require_role(UserRole.advisor))],
)
async def get_my_offered_services(
    current_user: CurrentUser,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[list[OfferedServiceRead]]:
    """Advisor's own offered services (includes price/duration from PUT/PATCH)."""
    items = await advisor_offered_service_service.list_for_advisor(
        session, current_user.id
    )
    return ResponseEnvelope[list[OfferedServiceRead]](
        data=items,
        meta=Meta(request_id=request_id),
    )


@router.put(
    "/advisors/me/offered_services",
    response_model=ResponseEnvelope[list[OfferedServiceRead]],
    dependencies=[Depends(require_role(UserRole.advisor))],
)
async def replace_my_offered_services(
    data: OfferedServicesReplaceRequest,
    current_user: CurrentUser,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[list[OfferedServiceRead]]:
    """Replace the advisor's full offered_services set (onboarding Continue)."""
    items = await advisor_offered_service_service.replace_for_advisor(
        session, current_user.id, data, actor_id=current_user.id
    )
    return ResponseEnvelope[list[OfferedServiceRead]](
        data=items,
        meta=Meta(request_id=request_id),
    )


@router.patch(
    "/advisors/me/offered_services/{offered_service_id}",
    response_model=ResponseEnvelope[OfferedServiceRead],
    dependencies=[Depends(require_role(UserRole.advisor))],
)
async def update_my_offered_service(
    offered_service_id: uuid.UUID,
    data: OfferedServiceUpdateRequest,
    current_user: CurrentUser,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[OfferedServiceRead]:
    result = await advisor_offered_service_service.update(
        session,
        current_user.id,
        offered_service_id,
        data,
        actor_id=current_user.id,
    )
    return ResponseEnvelope[OfferedServiceRead](
        data=result,
        meta=Meta(request_id=request_id),
    )


# ── Public: one advisor's services (seeker — no price / duration) ───────


@router.get(
    "/advisors/{advisor_id}/offered_services",
    response_model=ResponseEnvelope[list[OfferedServicePublicRead]],
)
async def get_advisor_offered_services(
    advisor_id: uuid.UUID,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[list[OfferedServicePublicRead]]:
    """Public/seeker view of one advisor's services — no price or duration."""
    items = await advisor_offered_service_service.list_for_advisor_public(
        session, advisor_id
    )
    return ResponseEnvelope[list[OfferedServicePublicRead]](
        data=items,
        meta=Meta(request_id=request_id),
    )


# ── Admin: full CRUD ────────────────────────────────────────────────────
# GET /admin/offered_services — all rows (with price + advisor_id)
# GET /admin/advisors/{advisor_id}/offered_services — one advisor
# POST/PATCH/DELETE /admin/offered_services[/{id}] — mutate by row id


@router.get(
    "/admin/offered_services",
    response_model=ResponseEnvelope[list[AdminOfferedServiceRead]],
    dependencies=[Depends(require_role(UserRole.admin))],
)
async def admin_list_all_offered_services(
    session: SessionDep,
    request_id: RequestIdDep,
    service_type: Annotated[str | None, Query()] = None,
) -> ResponseEnvelope[list[AdminOfferedServiceRead]]:
    """List all offered services (admin — id + service_type only)."""
    items = await advisor_offered_service_service.list_all_admin(
        session, service_type=service_type
    )
    return ResponseEnvelope[list[AdminOfferedServiceRead]](
        data=items,
        meta=Meta(request_id=request_id),
    )


@router.get(
    "/admin/advisors/{advisor_id}/offered_services",
    response_model=ResponseEnvelope[list[OfferedServiceRead]],
    dependencies=[Depends(require_role(UserRole.admin))],
)
async def admin_list_offered_services(
    advisor_id: uuid.UUID,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[list[OfferedServiceRead]]:
    """List offered services for one advisor (admin — includes price/duration)."""
    items = await advisor_offered_service_service.list_for_advisor(session, advisor_id)
    return ResponseEnvelope[list[OfferedServiceRead]](
        data=items,
        meta=Meta(request_id=request_id),
    )


@router.post(
    "/admin/offered_services",
    response_model=ResponseEnvelope[OfferedServiceRead],
    status_code=201,
    dependencies=[Depends(require_role(UserRole.admin))],
)
async def admin_create_offered_service(
    data: AdminOfferedServiceCreateRequest,
    current_user: CurrentUser,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[OfferedServiceRead]:
    """Create one offered service.

    Omit ``advisor_id`` to create a global catalog row; pass it to attach to an advisor.
    """
    payload = OfferedServiceCreateRequest(
        service_type=data.service_type,
        price_usd=data.price_usd,
        duration_minutes=data.duration_minutes,
    )
    if data.advisor_id is None:
        result = await advisor_offered_service_service.create_global(session, payload)
    else:
        result = await advisor_offered_service_service.create(
            session,
            data.advisor_id,
            payload,
            actor_id=current_user.id,
        )
    return ResponseEnvelope[OfferedServiceRead](
        data=result,
        meta=Meta(request_id=request_id),
    )


@router.patch(
    "/admin/offered_services/{offered_service_id}",
    response_model=ResponseEnvelope[OfferedServiceRead],
    dependencies=[Depends(require_role(UserRole.admin))],
)
async def admin_update_offered_service(
    offered_service_id: uuid.UUID,
    current_user: CurrentUser,
    session: SessionDep,
    request_id: RequestIdDep,
    data: OfferedServiceUpdateRequest = Body(default_factory=OfferedServiceUpdateRequest),
) -> ResponseEnvelope[OfferedServiceRead]:
    """Update any offered-service row by its id. Send only fields to change."""
    result = await advisor_offered_service_service.update_by_id(
        session,
        offered_service_id,
        data,
        actor_id=current_user.id,
    )
    return ResponseEnvelope[OfferedServiceRead](
        data=result,
        meta=Meta(request_id=request_id),
    )


@router.delete(
    "/admin/offered_services/{offered_service_id}",
    status_code=204,
    dependencies=[Depends(require_role(UserRole.admin))],
)
async def admin_delete_offered_service(
    offered_service_id: uuid.UUID,
    current_user: CurrentUser,
    session: SessionDep,
) -> None:
    """Delete any offered-service row by its id (no advisor_id needed)."""
    await advisor_offered_service_service.delete_by_id(
        session, offered_service_id, actor_id=current_user.id
    )

@router.put(
    "/admin/advisors/{advisor_id}/offered_services",
    response_model=ResponseEnvelope[list[OfferedServiceRead]],
    dependencies=[Depends(require_role(UserRole.admin))],
)
async def admin_replace_offered_services(
    advisor_id: uuid.UUID,
    data: OfferedServicesReplaceRequest,
    current_user: CurrentUser,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[list[OfferedServiceRead]]:
    """Replace an advisor's full offered-services set."""
    items = await advisor_offered_service_service.replace_for_advisor(
        session, advisor_id, data, actor_id=current_user.id
    )
    return ResponseEnvelope[list[OfferedServiceRead]](
        data=items,
        meta=Meta(request_id=request_id),
    )

