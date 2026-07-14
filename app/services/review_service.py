"""Ratings & reviews — submission, advisor response, moderation, aggregates."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError, NotFoundError, PermissionDeniedError
from app.models.booking import Booking, BookingStatus, PaymentStatus
from app.models.review import ModerationStatus, Review
from app.models.user import User
from app.schemas.review import ReviewCreate, ReviewRead

PUBLIC_STATUSES = (ModerationStatus.visible, ModerationStatus.flagged)


async def create(
    session: AsyncSession, seeker: User, booking: Booking, data: ReviewCreate
) -> Review:
    if booking.seeker_id != seeker.id:
        raise PermissionDeniedError("Only the booking's seeker can review it")
    if booking.status != BookingStatus.completed:
        raise AppError("Only completed sessions can be reviewed", code="not_completed")

    existing = await session.execute(select(Review.id).where(Review.booking_id == booking.id))
    if existing.scalar_one_or_none() is not None:
        raise AppError("This booking has already been reviewed", code="already_reviewed")

    overall = round(
        (
            data.rating_expertise
            + data.rating_communication
            + data.rating_professionalism
            + data.rating_value
        )
        / 4,
        2,
    )
    review = Review(
        booking_id=booking.id,
        seeker_id=seeker.id,
        advisor_id=booking.advisor_id,
        rating_expertise=data.rating_expertise,
        rating_communication=data.rating_communication,
        rating_professionalism=data.rating_professionalism,
        rating_value=data.rating_value,
        rating_overall=overall,
        text=data.text,
        is_verified=booking.payment_status == PaymentStatus.paid,
        created_by=seeker.id,
    )
    session.add(review)
    await session.flush()
    await session.refresh(review)
    return review


def list_public_stmt(advisor_id: uuid.UUID) -> Select[tuple[Review]]:
    return (
        select(Review)
        .where(Review.advisor_id == advisor_id)
        .where(Review.moderation_status.in_(PUBLIC_STATUSES))
        .order_by(Review.created_at.desc())
    )


def list_flagged_stmt() -> Select[tuple[Review]]:
    return (
        select(Review)
        .where(Review.moderation_status == ModerationStatus.flagged)
        .order_by(Review.updated_at.desc())
    )


async def get_by_id(session: AsyncSession, review_id: uuid.UUID) -> Review:
    review = await session.get(Review, review_id)
    if review is None:
        raise NotFoundError("Review not found")
    return review


async def respond(session: AsyncSession, review: Review, advisor: User, response: str) -> Review:
    if review.advisor_id != advisor.id:
        raise PermissionDeniedError("Only the reviewed advisor can respond")
    if review.advisor_response is not None:
        raise AppError("Review already has a response", code="already_responded")
    review.advisor_response = response
    review.responded_at = datetime.now(UTC)
    review.updated_by = advisor.id
    session.add(review)
    await session.flush()
    await session.refresh(review)
    return review


async def report(
    session: AsyncSession, review: Review, reporter_id: uuid.UUID, reason: str
) -> Review:
    if review.moderation_status == ModerationStatus.removed:
        raise AppError("Review already removed", code="invalid_state")
    review.moderation_status = ModerationStatus.flagged
    review.flag_reason = reason
    review.flagged_by = reporter_id
    review.updated_by = reporter_id
    session.add(review)
    await session.flush()
    await session.refresh(review)
    return review


async def moderate(
    session: AsyncSession, review: Review, action: str, admin_id: uuid.UUID
) -> Review:
    if action == "approve":
        review.moderation_status = ModerationStatus.visible
        review.flag_reason = None
        review.flagged_by = None
    else:  # remove
        review.moderation_status = ModerationStatus.removed
    review.updated_by = admin_id
    session.add(review)
    await session.flush()
    await session.refresh(review)
    return review


async def rating_summary(session: AsyncSession, advisor_id: uuid.UUID) -> tuple[float | None, int]:
    """(average overall rating, public review count) for an advisor."""
    result = await session.execute(
        select(func.avg(Review.rating_overall), func.count(Review.id))
        .where(Review.advisor_id == advisor_id)
        .where(Review.moderation_status.in_(PUBLIC_STATUSES))
    )
    avg, count = result.one()
    return (round(float(avg), 2) if avg is not None else None), int(count)


async def rating_summaries(
    session: AsyncSession, advisor_ids: list[uuid.UUID]
) -> dict[uuid.UUID, tuple[float, int]]:
    """Bulk aggregate for listing cards: advisor_id -> (avg, count)."""
    if not advisor_ids:
        return {}
    result = await session.execute(
        select(Review.advisor_id, func.avg(Review.rating_overall), func.count(Review.id))
        .where(Review.advisor_id.in_(advisor_ids))
        .where(Review.moderation_status.in_(PUBLIC_STATUSES))
        .group_by(Review.advisor_id)
    )
    return {row[0]: (round(float(row[1]), 2), int(row[2])) for row in result.all()}


def build_read(review: Review, seeker: User | None) -> ReviewRead:
    return ReviewRead(
        id=review.id,
        booking_id=review.booking_id,
        advisor_id=review.advisor_id,
        seeker_name=seeker.full_name if seeker else None,
        rating_expertise=review.rating_expertise,
        rating_communication=review.rating_communication,
        rating_professionalism=review.rating_professionalism,
        rating_value=review.rating_value,
        rating_overall=review.rating_overall,
        text=review.text,
        is_verified=review.is_verified,
        advisor_response=review.advisor_response,
        responded_at=review.responded_at,
        created_at=review.created_at,
    )
