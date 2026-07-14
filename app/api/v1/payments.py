"""Payment endpoints — checkout, webhook, transaction history (PRD §3.10)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Request

from app.api.deps import CurrentUser, RequestIdDep, SettingsDep
from app.api.pagination import PaginationDep, page_meta, paginate
from app.db.session import SessionDep
from app.models.transaction import Transaction
from app.models.user import UserRole
from app.schemas.payment import (
    CheckoutCreate,
    CheckoutResponse,
    InvoiceRead,
    TransactionRead,
)
from app.schemas.response import Meta, ResponseEnvelope
from app.services import payment_service

router = APIRouter(prefix="/payments", tags=["payments"])


async def _get_accessible_transaction(
    session: SessionDep, transaction_id: uuid.UUID, current_user: CurrentUser
) -> Transaction:
    """A transaction, accessible to its booking's seeker/advisor, or any admin."""
    if current_user.role == UserRole.admin:
        return await payment_service.get_by_id(session, transaction_id)
    return await payment_service.get_for_party(session, transaction_id, current_user.id)


@router.post("/checkout", response_model=ResponseEnvelope[CheckoutResponse], status_code=201)
async def create_checkout(
    data: CheckoutCreate,
    current_user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[CheckoutResponse]:
    """Initiate a Stripe Checkout Session for a confirmed booking."""
    result = await payment_service.create_checkout_session(
        session, data.booking_id, current_user.id, settings
    )
    return ResponseEnvelope[CheckoutResponse](
        data=result,
        meta=Meta(request_id=request_id),
    )


@router.post("/webhook", include_in_schema=False)
async def stripe_webhook(
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
) -> dict[str, bool]:
    """Stripe webhook handler — validates signature before processing."""
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    await payment_service.handle_webhook(payload, sig_header, settings, session)
    return {"received": True}


@router.get("/history", response_model=ResponseEnvelope[list[TransactionRead]])
async def get_payment_history(
    params: PaginationDep,
    current_user: CurrentUser,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[list[TransactionRead]]:
    """Transaction history for the current user (seeker sees their payments)."""
    from sqlalchemy import select

    from app.models.booking import Booking

    stmt = (
        select(Transaction)
        .join(Booking, Booking.id == Transaction.booking_id)
        .where(Booking.seeker_id == current_user.id)
        .order_by(Transaction.created_at.desc())
    )
    txns, total = await paginate(session, stmt, params)
    return ResponseEnvelope[list[TransactionRead]](
        data=[TransactionRead.model_validate(t) for t in txns],
        meta=page_meta(params, total, request_id),
    )


@router.get("/{transaction_id}", response_model=ResponseEnvelope[TransactionRead])
async def get_payment_detail(
    transaction_id: uuid.UUID,
    current_user: CurrentUser,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[TransactionRead]:
    """Payment Details modal — the booking's seeker, its advisor, or admin only."""
    txn = await _get_accessible_transaction(session, transaction_id, current_user)
    return ResponseEnvelope[TransactionRead](
        data=TransactionRead.model_validate(txn),
        meta=Meta(request_id=request_id),
    )


@router.get("/{transaction_id}/invoice", response_model=ResponseEnvelope[InvoiceRead])
async def get_payment_invoice(
    transaction_id: uuid.UUID,
    current_user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[InvoiceRead]:
    """Invoice Detail — only available once the transaction has succeeded."""
    txn = await _get_accessible_transaction(session, transaction_id, current_user)
    invoice = await payment_service.build_invoice(session, txn, settings)
    return ResponseEnvelope[InvoiceRead](
        data=invoice,
        meta=Meta(request_id=request_id),
    )
