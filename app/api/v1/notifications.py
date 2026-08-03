"""In-app notification feed — the read side of the push outbox."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import CurrentUser, RequestIdDep
from app.api.pagination import PaginationDep, page_meta, paginate
from app.db.session import SessionDep
from app.schemas.notification import NotificationRead, ReadAllResult, UnreadCountRead
from app.schemas.response import Meta, ResponseEnvelope
from app.services import notification_service

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("", response_model=ResponseEnvelope[list[NotificationRead]])
async def list_notifications(
    params: PaginationDep,
    current_user: CurrentUser,
    session: SessionDep,
    request_id: RequestIdDep,
    unread_only: Annotated[bool, Query(description="Only unread notifications")] = False,
) -> ResponseEnvelope[list[NotificationRead]]:
    """The current user's notification feed, newest first."""
    stmt = notification_service.list_for_user_stmt(current_user.id, unread_only=unread_only)
    notifications, total = await paginate(session, stmt, params)
    return ResponseEnvelope[list[NotificationRead]](
        data=[NotificationRead.model_validate(n) for n in notifications],
        meta=page_meta(params, total, request_id),
    )


@router.get("/unread-count", response_model=ResponseEnvelope[UnreadCountRead])
async def get_unread_count(
    current_user: CurrentUser,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[UnreadCountRead]:
    """Badge count for the notification bell."""
    count = await notification_service.unread_count(session, current_user.id)
    return ResponseEnvelope[UnreadCountRead](
        data=UnreadCountRead(unread=count), meta=Meta(request_id=request_id)
    )


@router.post("/read-all", response_model=ResponseEnvelope[ReadAllResult])
async def mark_all_read(
    current_user: CurrentUser,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[ReadAllResult]:
    """Mark every unread notification as read."""
    updated = await notification_service.mark_all_read(session, current_user.id)
    return ResponseEnvelope[ReadAllResult](
        data=ReadAllResult(updated=updated), meta=Meta(request_id=request_id)
    )


@router.post("/{notification_id}/read", response_model=ResponseEnvelope[NotificationRead])
async def mark_read(
    notification_id: uuid.UUID,
    current_user: CurrentUser,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[NotificationRead]:
    """Mark one notification as read; a not-yet-sent push for it is suppressed."""
    notification = await notification_service.mark_read(session, current_user.id, notification_id)
    return ResponseEnvelope[NotificationRead](
        data=NotificationRead.model_validate(notification), meta=Meta(request_id=request_id)
    )
