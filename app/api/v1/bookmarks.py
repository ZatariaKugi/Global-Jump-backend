"""Seeker advisor bookmarks — Bookmarked list screen CRUD."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import CurrentUser, RequestIdDep
from app.api.pagination import PaginationDep, page_meta, paginate
from app.core.exceptions import PermissionDeniedError
from app.core.visa_types import OptionalVisaType
from app.db.session import SessionDep
from app.models.user import UserRole
from app.schemas.bookmark import BookmarkCreate, BookmarkRead
from app.schemas.response import Meta, ResponseEnvelope
from app.services import bookmark_service
from app.services.advisor_search_service import SortOption

router = APIRouter(prefix="/bookmarks", tags=["bookmarks"])


def _require_seeker(current_user: CurrentUser) -> None:
    if current_user.role != UserRole.seeker:
        raise PermissionDeniedError("Seeker account required")


@router.post("", status_code=201, response_model=ResponseEnvelope[BookmarkRead])
async def create_bookmark(
    data: BookmarkCreate,
    current_user: CurrentUser,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[BookmarkRead]:
    """Bookmark an approved advisor."""
    _require_seeker(current_user)
    bookmark = await bookmark_service.create(session, current_user, data.advisor_id)
    rows = await bookmark_service.build_list_reads(session, current_user.id, [bookmark])
    return ResponseEnvelope[BookmarkRead](data=rows[0], meta=Meta(request_id=request_id))


@router.get("", response_model=ResponseEnvelope[list[BookmarkRead]])
async def list_bookmarks(
    params: PaginationDep,
    current_user: CurrentUser,
    session: SessionDep,
    request_id: RequestIdDep,
    q: Annotated[
        str | None, Query(max_length=100, description="Search name, email, expertise")
    ] = None,
    visa_type: Annotated[
        OptionalVisaType, Query(description="Filter by PRD visa specialization")
    ] = None,
    recommended: Annotated[
        bool,
        Query(description="When true, AI-suggested (featured) advisors sort first"),
    ] = False,
    sort: Annotated[SortOption, Query()] = "newest",
) -> ResponseEnvelope[list[BookmarkRead]]:
    """Bookmarked advisors table — same sort options as ``GET /advisors``."""
    _require_seeker(current_user)
    stmt = bookmark_service.list_for_seeker_stmt(
        current_user.id,
        q=q,
        visa_type=visa_type,
        sort=sort,
        recommended=recommended,
    )
    bookmarks, total = await paginate(session, stmt, params)
    data = await bookmark_service.build_list_reads(session, current_user.id, bookmarks)
    return ResponseEnvelope[list[BookmarkRead]](
        data=data, meta=page_meta(params, total, request_id)
    )


@router.get("/{advisor_id}", response_model=ResponseEnvelope[dict[str, bool]])
async def get_bookmark_status(
    advisor_id: uuid.UUID,
    current_user: CurrentUser,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[dict[str, bool]]:
    """Whether the current seeker has bookmarked this advisor."""
    _require_seeker(current_user)
    bookmarked = await bookmark_service.is_bookmarked(session, current_user.id, advisor_id)
    return ResponseEnvelope[dict[str, bool]](
        data={"is_bookmarked": bookmarked},
        meta=Meta(request_id=request_id),
    )


@router.delete("/{advisor_id}", status_code=204)
async def delete_bookmark(
    advisor_id: uuid.UUID,
    current_user: CurrentUser,
    session: SessionDep,
) -> None:
    """Remove an advisor from the seeker's bookmarks."""
    _require_seeker(current_user)
    await bookmark_service.delete(session, current_user, advisor_id)
