"""User endpoints — current user + an admin-only paginated list."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select

from app.api.deps import CurrentUser, RequestIdDep, require_role
from app.api.pagination import PaginationDep, page_meta, paginate
from app.db.session import SessionDep
from app.models.user import User, UserRole
from app.schemas.response import Meta, ResponseEnvelope
from app.schemas.user import UserRead, UserUpdate
from app.services import user_service

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me", response_model=ResponseEnvelope[UserRead])
async def read_current_user(
    current_user: CurrentUser, request_id: RequestIdDep
) -> ResponseEnvelope[UserRead]:
    return ResponseEnvelope[UserRead](
        data=UserRead.model_validate(current_user), meta=Meta(request_id=request_id)
    )


@router.patch("/me", response_model=ResponseEnvelope[UserRead])
async def update_current_user(
    data: UserUpdate,
    current_user: CurrentUser,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[UserRead]:
    user = await user_service.update_user(session, current_user, data)
    return ResponseEnvelope[UserRead](
        data=UserRead.model_validate(user), meta=Meta(request_id=request_id)
    )


@router.get(
    "",
    response_model=ResponseEnvelope[list[UserRead]],
    dependencies=[Depends(require_role(UserRole.admin))],
)
async def list_users(
    params: PaginationDep, session: SessionDep, request_id: RequestIdDep
) -> ResponseEnvelope[list[UserRead]]:
    stmt = select(User).where(User.is_archived.is_(False)).order_by(User.created_at.desc())
    users, total = await paginate(session, stmt, params)
    return ResponseEnvelope[list[UserRead]](
        data=[UserRead.model_validate(u) for u in users],
        meta=page_meta(params, total, request_id),
    )
