"""Payment endpoints — checkout, webhook, transaction history (PRD §3.10)."""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Query, Request

from app.api.deps import CurrentUser, RequestIdDep, SettingsDep
from app.api.pagination import PaginationDep, page_meta, paginate
from app.db.session import SessionDep
from app.models.transaction import Transaction
from app.models.user import UserRole
from app.schemas.payment import (
    CheckoutCreate,
    CheckoutResponse,
    InvoiceRead,
    PaymentConfigRead,
    SeekerPaymentRead,
    SeekerPaymentSummaryRead,
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


@router.get("/config", response_model=ResponseEnvelope[PaymentConfigRead])
async def get_payment_config(
    settings: SettingsDep,
    request_id: RequestIdDep,
    _current_user: CurrentUser,
) -> ResponseEnvelope[PaymentConfigRead]:
    """Stripe publishable key for frontend Checkout / Elements."""
    return ResponseEnvelope[PaymentConfigRead](
        data=PaymentConfigRead(publishable_key=settings.STRIPE_PUBLISHABLE_KEY),
        meta=Meta(request_id=request_id),
    )


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


@router.get("/summary", response_model=ResponseEnvelope[SeekerPaymentSummaryRead])
async def get_payment_summary(
    current_user: CurrentUser,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[SeekerPaymentSummaryRead]:
    """Seeker Payments summary cards (Total Paid / Pending / Refund / Last)."""
    data = await payment_service.seeker_payment_summary(session, current_user.id)
    return ResponseEnvelope[SeekerPaymentSummaryRead](
        data=data, meta=Meta(request_id=request_id)
    )


@router.get("/history", response_model=ResponseEnvelope[list[SeekerPaymentRead]])
async def get_payment_history(
    params: PaginationDep,
    current_user: CurrentUser,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[list[SeekerPaymentRead]]:
    """Visa-seeker payment history with advisor + fee split columns."""
    stmt = payment_service.list_for_seeker_stmt(current_user.id)
    txns, total = await paginate(session, stmt, params)
    data = [await payment_service.seeker_payment_read(session, t) for t in txns]
    return ResponseEnvelope[list[SeekerPaymentRead]](
        data=data,
        meta=page_meta(params, total, request_id),
    )


@router.get("/{transaction_id}", response_model=ResponseEnvelope[SeekerPaymentRead])
async def get_payment_detail(
    transaction_id: uuid.UUID,
    current_user: CurrentUser,
    session: SessionDep,
    request_id: RequestIdDep,
) -> ResponseEnvelope[SeekerPaymentRead]:
    """Payment Details — seeker/advisor/admin with enriched seeker-oriented fields."""
    txn = await _get_accessible_transaction(session, transaction_id, current_user)
    data = await payment_service.seeker_payment_read(session, txn)
    return ResponseEnvelope[SeekerPaymentRead](
        data=data,
        meta=Meta(request_id=request_id),
    )


@router.get("/{transaction_id}/invoice", response_model=ResponseEnvelope[InvoiceRead])
async def get_payment_invoice(
    transaction_id: uuid.UUID,
    current_user: CurrentUser,
    session: SessionDep,
    settings: SettingsDep,
    request_id: RequestIdDep,
    perspective: Literal["seeker", "advisor", "admin"] | None = Query(default=None),
) -> ResponseEnvelope[InvoiceRead]:
    """Invoice Detail — only available once the transaction has succeeded."""
    txn = await _get_accessible_transaction(session, transaction_id, current_user)
    if perspective is None:
        if current_user.role == UserRole.advisor:
            perspective = "advisor"
        elif current_user.role == UserRole.admin:
            perspective = "admin"
        else:
            perspective = "seeker"
    invoice = await payment_service.build_invoice(
        session, txn, settings, perspective=perspective
    )
    return ResponseEnvelope[InvoiceRead](
        data=invoice,
        meta=Meta(request_id=request_id),
    )
