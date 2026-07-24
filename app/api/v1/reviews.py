"""Ratings & reviews endpoints (PRD §3.9)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import CurrentUser, RequestIdDep
from app.api.pagination import PaginationDep, page_meta, paginate
from app.core.visa_types import OptionalVisaType
from app.db.session import SessionDep
from app.models.review import Review
from app.models.user import User
from app.schemas.response import Meta, ResponseEnvelope
from app.schemas.review import (
    AdvisorRatingSummary,
    ReviewCreate,
    ReviewRead,
    ReviewReport,
    ReviewResponseCreate,
)
from app.services import booking_service, review_service

router = APIRouter(tags=["reviews"])


async def _read_with_seeker(session: SessionDep, review: Review) -> ReviewRead:
    seeker = await session.get(User, review.seeker_id)
    return review_service.build_read(review, seeker)


@router.post(
    "/bookings/{booking_id}/review",
    status_code=201,
    response_model=ResponseEnvelope[ReviewRead],
)
async def submit_review(
    booking_id: uuid.UUID,
    data: ReviewCreate,
    current_user: CurrentUser,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[ReviewRead]:
    booking = await booking_service.get_for_party(session, booking_id, current_user.id)
    review = await review_service.create(session, current_user, booking, data)
    return ResponseEnvelope[ReviewRead](
        data=await _read_with_seeker(session, review),
        meta=Meta(request_id=request_id),
    )


@router.get(
    "/advisors/{advisor_id}/reviews",
    response_model=ResponseEnvelope[list[ReviewRead]],
)
async def list_advisor_reviews(
    advisor_id: uuid.UUID,
    params: PaginationDep,
    _current_user: CurrentUser,
    session: SessionDep,
    request_id: RequestIdDep,
    visa_type: OptionalVisaType = None,
    q: Annotated[
        str | None,
        Query(max_length=100, description="Search seeker name or review text"),
    ] = None,
) -> ResponseEnvelope[list[ReviewRead]]:
    stmt = review_service.list_public_stmt(advisor_id, visa_type=visa_type, q=q)
    reviews, total = await paginate(session, stmt, params)
    data = await review_service.build_enriched_reads(session, reviews)
    return ResponseEnvelope[list[ReviewRead]](
        data=data,
        meta=page_meta(params, total, request_id),
    )


@router.get(
    "/advisors/{advisor_id}/rating",
    response_model=ResponseEnvelope[AdvisorRatingSummary],
)
async def get_advisor_rating(
    advisor_id: uuid.UUID,
    _current_user: CurrentUser,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[AdvisorRatingSummary]:
    average, count = await review_service.rating_summary(session, advisor_id)
    return ResponseEnvelope[AdvisorRatingSummary](
        data=AdvisorRatingSummary(average_rating=average, review_count=count),
        meta=Meta(request_id=request_id),
    )


@router.post("/reviews/{review_id}/response", response_model=ResponseEnvelope[ReviewRead])
async def respond_to_review(
    review_id: uuid.UUID,
    data: ReviewResponseCreate,
    current_user: CurrentUser,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[ReviewRead]:
    review = await review_service.get_by_id(session, review_id)
    review = await review_service.respond(session, review, current_user, data.response)
    return ResponseEnvelope[ReviewRead](
        data=await _read_with_seeker(session, review),
        meta=Meta(request_id=request_id),
    )


@router.patch("/reviews/{review_id}/response", response_model=ResponseEnvelope[ReviewRead])
async def update_review_response(
    review_id: uuid.UUID,
    data: ReviewResponseCreate,
    current_user: CurrentUser,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[ReviewRead]:
    """Edit an existing advisor reply (fails with ``no_response`` if none yet)."""
    review = await review_service.get_by_id(session, review_id)
    review = await review_service.update_response(session, review, current_user, data.response)
    return ResponseEnvelope[ReviewRead](
        data=await _read_with_seeker(session, review),
        meta=Meta(request_id=request_id),
    )


@router.delete("/reviews/{review_id}/response", response_model=ResponseEnvelope[ReviewRead])
async def delete_review_response(
    review_id: uuid.UUID,
    current_user: CurrentUser,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[ReviewRead]:
    """Remove the advisor reply (``advisor_response`` / ``responded_at`` cleared)."""
    review = await review_service.get_by_id(session, review_id)
    review = await review_service.delete_response(session, review, current_user)
    return ResponseEnvelope[ReviewRead](
        data=await _read_with_seeker(session, review),
        meta=Meta(request_id=request_id),
    )


@router.post("/reviews/{review_id}/report", response_model=ResponseEnvelope[ReviewRead])
async def report_review(
    review_id: uuid.UUID,
    data: ReviewReport,
    current_user: CurrentUser,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[ReviewRead]:
    review = await review_service.get_by_id(session, review_id)
    review = await review_service.report(session, review, current_user.id, data.reason)
    return ResponseEnvelope[ReviewRead](
        data=await _read_with_seeker(session, review),
        meta=Meta(request_id=request_id),
    )
