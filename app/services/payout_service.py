"""Advisor payout requests — available-balance ledger, request creation, admin review."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import AppError, NotFoundError
from app.models.booking import Booking
from app.models.payout_request import PayoutRequest, PayoutStatus
from app.models.transaction import Transaction, TransactionStatus
from app.models.user import User
from app.schemas.payout import PayoutPreviewRead, PayoutRequestCreate


async def get_available_balance(session: AsyncSession, advisor_id: uuid.UUID) -> float:
    """Succeeded transaction payouts minus every non-rejected payout request.

    A pending request reserves its amount just like a completed one — only a
    rejected request releases the balance back for re-requesting.
    """
    earned_result = await session.execute(
        select(func.sum(Transaction.advisor_payout_usd))
        .join(Booking, Booking.id == Transaction.booking_id)
        .where(Booking.advisor_id == advisor_id)
        .where(Transaction.status == TransactionStatus.succeeded)
        .where(Transaction.is_archived.is_(False))
    )
    total_earned = earned_result.scalar_one_or_none() or 0.0

    reserved_result = await session.execute(
        select(func.sum(PayoutRequest.amount_usd))
        .where(PayoutRequest.advisor_id == advisor_id)
        .where(PayoutRequest.status != PayoutStatus.rejected)
    )
    total_reserved = reserved_result.scalar_one_or_none() or 0.0

    return round(total_earned - total_reserved, 2)


def preview_payout(
    available_balance_usd: float, amount_usd: float, settings: Settings
) -> PayoutPreviewRead:
    if amount_usd <= 0:
        raise AppError("Amount must be greater than zero", code="invalid_amount")
    if amount_usd > available_balance_usd:
        raise AppError("Requested amount exceeds available balance", code="insufficient_balance")
    processing_fee_usd = round(amount_usd * settings.PAYOUT_PROCESSING_FEE_RATE, 2)
    net_amount_usd = round(amount_usd - processing_fee_usd, 2)
    return PayoutPreviewRead(
        available_balance_usd=available_balance_usd,
        amount_usd=amount_usd,
        processing_fee_usd=processing_fee_usd,
        processing_fee_rate=settings.PAYOUT_PROCESSING_FEE_RATE,
        net_amount_usd=net_amount_usd,
    )


async def create_request(
    session: AsyncSession, advisor: User, data: PayoutRequestCreate, settings: Settings
) -> PayoutRequest:
    available = await get_available_balance(session, advisor.id)
    if data.amount_usd > available:
        raise AppError("Requested amount exceeds available balance", code="insufficient_balance")

    processing_fee_usd = round(data.amount_usd * settings.PAYOUT_PROCESSING_FEE_RATE, 2)
    net_amount_usd = round(data.amount_usd - processing_fee_usd, 2)

    payout = PayoutRequest(
        advisor_id=advisor.id,
        amount_usd=data.amount_usd,
        method=data.method,
        note=data.note,
        account_holder_name=data.account_holder_name,
        account_number=data.account_number,
        bank_name=data.bank_name,
        swift_code=data.swift_code,
        processing_fee_usd=processing_fee_usd,
        net_amount_usd=net_amount_usd,
        status=PayoutStatus.pending,
        created_by=advisor.id,
    )
    session.add(payout)
    await session.flush()
    await session.refresh(payout)
    return payout


def list_for_advisor_stmt(
    advisor_id: uuid.UUID, status: PayoutStatus | None = None
) -> Select[tuple[PayoutRequest]]:
    stmt = (
        select(PayoutRequest)
        .where(PayoutRequest.advisor_id == advisor_id)
        .order_by(PayoutRequest.created_at.desc())
    )
    if status is not None:
        stmt = stmt.where(PayoutRequest.status == status)
    return stmt


def list_all_stmt(status: PayoutStatus | None = None) -> Select[tuple[PayoutRequest]]:
    stmt = select(PayoutRequest).order_by(PayoutRequest.created_at.desc())
    if status is not None:
        stmt = stmt.where(PayoutRequest.status == status)
    return stmt


async def get_by_id(session: AsyncSession, payout_id: uuid.UUID) -> PayoutRequest:
    payout = await session.get(PayoutRequest, payout_id)
    if payout is None:
        raise NotFoundError("Payout request not found")
    return payout


async def get_for_advisor(
    session: AsyncSession, payout_id: uuid.UUID, advisor_id: uuid.UUID
) -> PayoutRequest:
    payout = await session.get(PayoutRequest, payout_id)
    if payout is None or payout.advisor_id != advisor_id:
        raise NotFoundError("Payout request not found")
    return payout


def _assert_pending(payout: PayoutRequest) -> None:
    if payout.status != PayoutStatus.pending:
        raise AppError("Payout request already processed", code="invalid_state")


async def complete(
    session: AsyncSession, payout: PayoutRequest, admin_id: uuid.UUID
) -> PayoutRequest:
    _assert_pending(payout)
    payout.status = PayoutStatus.completed
    payout.processed_at = datetime.now(UTC)
    payout.processed_by = admin_id
    payout.updated_by = admin_id
    session.add(payout)
    await session.flush()
    await session.refresh(payout)
    return payout


async def reject(
    session: AsyncSession, payout: PayoutRequest, admin_id: uuid.UUID, reason: str | None
) -> PayoutRequest:
    _assert_pending(payout)
    payout.status = PayoutStatus.rejected
    payout.rejection_reason = reason
    payout.processed_at = datetime.now(UTC)
    payout.processed_by = admin_id
    payout.updated_by = admin_id
    session.add(payout)
    await session.flush()
    await session.refresh(payout)
    return payout
