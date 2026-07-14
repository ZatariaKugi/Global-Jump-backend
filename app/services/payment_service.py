"""Stripe payment operations — checkout, webhook, refunds, Connect onboarding (PRD §3.10)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import stripe
import structlog
from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.config import Settings
from app.core.exceptions import AppError, NotFoundError
from app.models.advisor_profile import AdvisorProfile
from app.models.booking import Booking, BookingStatus, PaymentStatus
from app.models.transaction import Transaction, TransactionStatus
from app.models.transaction_event import TransactionEvent, TransactionEventType
from app.models.user import User, UserRole
from app.schemas.payment import (
    AdvisorConnectStatus,
    CheckoutResponse,
    InvoiceLineItem,
    InvoiceRead,
    TransactionFinanceRead,
)
from app.services import email_service

log = structlog.get_logger()


def _init_stripe(settings: Settings) -> None:
    if not settings.STRIPE_SECRET_KEY:
        raise AppError("Payment processing is not configured", code="stripe_not_configured")
    stripe.api_key = settings.STRIPE_SECRET_KEY


async def _log_event(
    session: AsyncSession, transaction_id: uuid.UUID, event_type: TransactionEventType
) -> None:
    # occurred_at is set explicitly (not left to the DB server_default) so that
    # several events logged within the same request stay in insertion order even
    # at SQLite's one-second timestamp resolution (same rationale as
    # Message.created_at — see conversation_service.send_message).
    session.add(
        TransactionEvent(
            transaction_id=transaction_id,
            event_type=event_type,
            occurred_at=datetime.now(UTC),
        )
    )
    await session.flush()


async def _get_advisor_profile(session: AsyncSession, advisor_id: uuid.UUID) -> AdvisorProfile:
    result = await session.execute(
        select(AdvisorProfile).where(AdvisorProfile.user_id == advisor_id)
    )
    profile = result.scalar_one_or_none()
    if profile is None:
        raise NotFoundError("Advisor profile not found")
    return profile


async def create_checkout_session(
    session: AsyncSession,
    booking_id: uuid.UUID,
    seeker_id: uuid.UUID,
    settings: Settings,
) -> CheckoutResponse:
    _init_stripe(settings)

    booking = await session.get(Booking, booking_id)
    if booking is None or booking.seeker_id != seeker_id:
        raise NotFoundError("Booking not found")
    if booking.status != BookingStatus.confirmed:
        raise AppError("Booking is not in a payable state", code="invalid_booking_state")
    if booking.payment_status != PaymentStatus.unpaid:
        raise AppError("Booking is already paid or refunded", code="already_paid")

    # Guard against duplicate checkout sessions
    existing = await session.execute(
        select(Transaction).where(Transaction.booking_id == booking_id)
    )
    if existing.scalar_one_or_none() is not None:
        raise AppError(
            "A checkout session already exists for this booking", code="duplicate_checkout"
        )

    advisor_profile = await _get_advisor_profile(session, booking.advisor_id)

    commission_rate = settings.PLATFORM_COMMISSION_RATE
    commission_usd = round(booking.price_usd * commission_rate, 2)
    tax_rate = settings.TAX_WITHHOLDING_RATE
    tax_usd = round(booking.price_usd * tax_rate, 2)
    advisor_payout_usd = round(booking.price_usd - commission_usd - tax_usd, 2)

    advisor = await session.get(User, booking.advisor_id)
    advisor_name = advisor.full_name if advisor else "Advisor"

    # Use Stripe Connect when the advisor has a connected account; otherwise
    # collect to the platform account and track the expected advisor payout.
    # The platform retains commission + withheld tax; the connected account
    # only receives advisor_payout_usd.
    payment_intent_data: dict[str, object] = {}
    if advisor_profile.stripe_account_id:
        payment_intent_data = {
            "application_fee_amount": int(round((commission_usd + tax_usd) * 100)),
            "transfer_data": {"destination": advisor_profile.stripe_account_id},
        }

    checkout_session = await stripe.checkout.Session.create_async(
        payment_method_types=["card"],
        line_items=[
            {
                "price_data": {
                    "currency": "usd",
                    "product_data": {
                        "name": f"{booking.service_type} with {advisor_name}",
                        "description": f"{booking.duration_minutes}-minute session",
                    },
                    "unit_amount": int(booking.price_usd * 100),
                },
                "quantity": 1,
            }
        ],
        mode="payment",
        # Stripe's stubs expose ~40 individually-typed overloaded kwargs for this
        # call; a dict splat can't type-check against that regardless of shape.
        **({"payment_intent_data": payment_intent_data} if payment_intent_data else {}),  # type: ignore[arg-type]
        success_url=f"{settings.FRONTEND_URL}/bookings/{booking_id}?payment=success",
        cancel_url=f"{settings.FRONTEND_URL}/bookings/{booking_id}?payment=cancelled",
        metadata={"booking_id": str(booking_id)},
    )

    txn = Transaction(
        booking_id=booking_id,
        stripe_checkout_session_id=checkout_session.id,
        amount_usd=booking.price_usd,
        commission_rate=commission_rate,
        commission_usd=commission_usd,
        tax_rate=tax_rate,
        tax_usd=tax_usd,
        advisor_payout_usd=advisor_payout_usd,
        status=TransactionStatus.pending,
        created_by=seeker_id,
    )
    session.add(txn)
    await session.flush()
    await _log_event(session, txn.id, TransactionEventType.initiated)

    log.info(
        "checkout_session_created",
        booking_id=str(booking_id),
        session_id=checkout_session.id,
        amount_usd=booking.price_usd,
    )
    return CheckoutResponse(checkout_url=checkout_session.url, session_id=checkout_session.id)


async def handle_webhook(
    payload: bytes,
    sig_header: str,
    settings: Settings,
    session: AsyncSession,
) -> None:
    _init_stripe(settings)

    if settings.STRIPE_WEBHOOK_SECRET:
        try:
            event = stripe.Webhook.construct_event(  # type: ignore[no-untyped-call]
                payload, sig_header, settings.STRIPE_WEBHOOK_SECRET
            )
        except (ValueError, stripe.SignatureVerificationError) as exc:
            raise AppError("Invalid webhook signature", code="invalid_signature") from exc
    else:
        # Dev mode: no webhook secret configured, skip signature verification.
        import json as _json

        event = _json.loads(payload)
        log.warning(
            "webhook_signature_verification_skipped", reason="STRIPE_WEBHOOK_SECRET not set"
        )

    event_type: str = event["type"]
    log.info("stripe_webhook", event_type=event_type, event_id=event["id"])

    if event_type == "checkout.session.completed":
        await _handle_checkout_completed(session, event["data"]["object"], settings)
    elif event_type == "checkout.session.expired":
        await _handle_checkout_expired(session, event["data"]["object"])
    elif event_type == "charge.refunded":
        await _handle_charge_refunded(session, event["data"]["object"])


async def _next_invoice_number(session: AsyncSession) -> int:
    """Next sequential invoice number, assigned only once a transaction succeeds.

    A plain max+1 query rather than a DB identity/sequence column — this
    project's models are UUID-keyed throughout and a numeric identity column
    doesn't translate to the SQLite dialect the test suite runs on. Invoice
    numbering is low-volume and cosmetic, so the small race window under
    concurrent webhook delivery is an acceptable tradeoff.
    """
    result = await session.execute(select(func.max(Transaction.invoice_number)))
    current_max = result.scalar_one_or_none() or 0
    return current_max + 1


async def _handle_checkout_completed(session: AsyncSession, cs: object, settings: Settings) -> None:
    session_id: str = cs["id"]  # type: ignore[index]
    txn_result = await session.execute(
        select(Transaction).where(Transaction.stripe_checkout_session_id == session_id)
    )
    txn = txn_result.scalar_one_or_none()
    if txn is None:
        log.warning("webhook_checkout_no_txn", session_id=session_id)
        return

    pi_id: str | None = cs.get("payment_intent")  # type: ignore[attr-defined]
    charge_id: str | None = None
    if pi_id:
        try:
            pi = await stripe.PaymentIntent.retrieve_async(pi_id, expand=["latest_charge"])
            latest_charge = pi.get("latest_charge")  # type: ignore[attr-defined]
            if latest_charge:
                charge_id = (
                    latest_charge["id"] if isinstance(latest_charge, dict) else latest_charge.id
                )
        except stripe.StripeError as exc:
            log.warning("webhook_pi_retrieve_failed", pi_id=pi_id, error=str(exc))

    txn.status = TransactionStatus.succeeded
    txn.stripe_payment_intent_id = pi_id
    txn.stripe_charge_id = charge_id
    txn.invoice_number = await _next_invoice_number(session)
    session.add(txn)
    await _log_event(session, txn.id, TransactionEventType.authorized)
    await _log_event(session, txn.id, TransactionEventType.completed)
    await _log_event(session, txn.id, TransactionEventType.invoice_generated)

    booking = await session.get(Booking, txn.booking_id)
    if booking is not None:
        booking.payment_status = PaymentStatus.paid
        session.add(booking)

    await session.flush()
    log.info("payment_succeeded", booking_id=str(txn.booking_id), amount_usd=txn.amount_usd)

    if booking is not None:
        seeker = await session.get(User, booking.seeker_id)
        advisor = await session.get(User, booking.advisor_id)
        if seeker is not None:
            await email_service.send_payment_receipt_email(
                seeker.email,
                seeker.full_name or seeker.email,
                advisor.full_name if advisor and advisor.full_name else "Advisor",
                service_type=booking.service_type,
                amount_usd=txn.amount_usd,
                invoice_number=f"{txn.invoice_number:08d}",
                settings=settings,
            )
    await _log_event(session, txn.id, TransactionEventType.receipt_sent)
    await _log_event(session, txn.id, TransactionEventType.closed)


async def _handle_checkout_expired(session: AsyncSession, cs: object) -> None:
    session_id: str = cs["id"]  # type: ignore[index]
    txn_result = await session.execute(
        select(Transaction).where(Transaction.stripe_checkout_session_id == session_id)
    )
    txn = txn_result.scalar_one_or_none()
    if txn is None:
        return
    txn.status = TransactionStatus.failed
    session.add(txn)
    await _log_event(session, txn.id, TransactionEventType.failed)
    await _log_event(session, txn.id, TransactionEventType.closed)
    await session.flush()
    log.info("checkout_expired", session_id=session_id)


async def _handle_charge_refunded(session: AsyncSession, charge: object) -> None:
    charge_id: str = charge["id"]  # type: ignore[index]
    txn_result = await session.execute(
        select(Transaction).where(Transaction.stripe_charge_id == charge_id)
    )
    txn = txn_result.scalar_one_or_none()
    if txn is None:
        log.warning("webhook_refund_no_txn", charge_id=charge_id)
        return

    amount_refunded_cents = charge.get("amount_refunded")  # type: ignore[attr-defined]
    refunded_amount_usd = (
        round(amount_refunded_cents / 100, 2)
        if amount_refunded_cents is not None
        else txn.amount_usd
    )
    is_full = refunded_amount_usd >= txn.amount_usd
    txn.status = TransactionStatus.refunded if is_full else TransactionStatus.partially_refunded
    txn.refunded_amount_usd = refunded_amount_usd
    txn.refunded_at = datetime.now(UTC)
    session.add(txn)
    await _log_event(session, txn.id, TransactionEventType.refunded)
    if is_full:
        await _log_event(session, txn.id, TransactionEventType.closed)

    booking = await session.get(Booking, txn.booking_id)
    if booking is not None:
        booking.payment_status = PaymentStatus.refunded
        session.add(booking)

    await session.flush()
    log.info("payment_refunded_via_webhook", booking_id=str(txn.booking_id))


async def refund_transaction(
    session: AsyncSession,
    transaction_id: uuid.UUID,
    admin_id: uuid.UUID,
    reason: str | None,
    settings: Settings,
    amount_usd: float | None = None,
) -> Transaction:
    """Issue a full or partial refund. ``amount_usd=None`` refunds the full amount."""
    _init_stripe(settings)

    txn = await session.get(Transaction, transaction_id)
    if txn is None:
        raise NotFoundError("Transaction not found")
    if txn.status != TransactionStatus.succeeded:
        raise AppError("Only succeeded transactions can be refunded", code="not_refundable")

    refund_amount = amount_usd if amount_usd is not None else txn.amount_usd
    if refund_amount > txn.amount_usd:
        raise AppError(
            "Refund amount cannot exceed the transaction amount", code="refund_amount_too_large"
        )

    refund_params: dict[str, str | int] = {}
    if txn.stripe_payment_intent_id:
        refund_params["payment_intent"] = txn.stripe_payment_intent_id
    elif txn.stripe_charge_id:
        refund_params["charge"] = txn.stripe_charge_id
    else:
        raise AppError("No Stripe charge found for this transaction", code="no_charge")
    if amount_usd is not None:
        refund_params["amount"] = int(round(amount_usd * 100))

    # Same overloaded-kwargs stub mismatch as the checkout call above.
    await stripe.Refund.create_async(**refund_params)  # type: ignore[arg-type]

    is_full = refund_amount >= txn.amount_usd
    txn.status = TransactionStatus.refunded if is_full else TransactionStatus.partially_refunded
    txn.refunded_at = datetime.now(UTC)
    txn.refunded_by = admin_id
    txn.refund_reason = reason
    txn.refunded_amount_usd = refund_amount
    txn.updated_by = admin_id
    session.add(txn)
    await _log_event(session, txn.id, TransactionEventType.refunded)
    if is_full:
        await _log_event(session, txn.id, TransactionEventType.closed)

    booking = await session.get(Booking, txn.booking_id)
    if booking is not None:
        booking.payment_status = PaymentStatus.refunded
        session.add(booking)

    await session.flush()
    await session.refresh(txn)
    log.info(
        "payment_refunded_by_admin", transaction_id=str(transaction_id), admin_id=str(admin_id)
    )
    return txn


async def create_connect_account(
    session: AsyncSession,
    advisor_user: User,
    settings: Settings,
) -> AdvisorConnectStatus:
    _init_stripe(settings)

    if advisor_user.role != UserRole.advisor:
        raise AppError("Only advisors can connect a Stripe account", code="wrong_role")

    profile = await _get_advisor_profile(session, advisor_user.id)

    if not profile.stripe_account_id:
        account = await stripe.Account.create_async(
            type="express",
            email=advisor_user.email,
            capabilities={
                "card_payments": {"requested": True},
                "transfers": {"requested": True},
            },
            metadata={"user_id": str(advisor_user.id)},
        )
        profile.stripe_account_id = account.id
        session.add(profile)
        await session.flush()
        log.info(
            "stripe_connect_account_created", advisor_id=str(advisor_user.id), account_id=account.id
        )

    account_link = await stripe.AccountLink.create_async(
        account=profile.stripe_account_id,
        refresh_url=f"{settings.FRONTEND_URL}/advisor/connect/refresh",
        return_url=f"{settings.FRONTEND_URL}/advisor/connect/return",
        type="account_onboarding",
    )
    return AdvisorConnectStatus(
        stripe_account_id=profile.stripe_account_id,
        charges_enabled=False,
        onboarding_complete=False,
        onboarding_url=account_link.url,
    )


async def get_connect_status(
    session: AsyncSession,
    advisor_user_id: uuid.UUID,
    settings: Settings,
) -> AdvisorConnectStatus:
    _init_stripe(settings)

    profile = await _get_advisor_profile(session, advisor_user_id)

    if not profile.stripe_account_id:
        return AdvisorConnectStatus(
            stripe_account_id=None,
            charges_enabled=False,
            onboarding_complete=False,
        )

    account = await stripe.Account.retrieve_async(profile.stripe_account_id)
    charges_enabled: bool = bool(account.get("charges_enabled", False))  # type: ignore[attr-defined]
    details_submitted: bool = bool(
        account.get("details_submitted", False)  # type: ignore[attr-defined]
    )

    return AdvisorConnectStatus(
        stripe_account_id=profile.stripe_account_id,
        charges_enabled=charges_enabled,
        onboarding_complete=details_submitted and charges_enabled,
    )


async def get_advisor_earnings(
    session: AsyncSession,
    advisor_user_id: uuid.UUID,
) -> dict[str, object]:
    result = await session.execute(
        select(Transaction)
        .join(Booking, Booking.id == Transaction.booking_id)
        .where(Booking.advisor_id == advisor_user_id)
        .order_by(Transaction.created_at.desc())
    )
    txns = list(result.scalars().all())

    total_earned = sum(
        t.advisor_payout_usd for t in txns if t.status == TransactionStatus.succeeded
    )
    total_commission = sum(
        t.commission_usd for t in txns if t.status == TransactionStatus.succeeded
    )

    return {
        "total_earned_usd": round(total_earned, 2),
        "total_commission_paid_usd": round(total_commission, 2),
        "transactions": txns,
    }


def list_for_advisor_stmt(advisor_id: uuid.UUID) -> Select[tuple[Transaction]]:
    """Transactions for the advisor's "Payment of customers" list."""
    return (
        select(Transaction)
        .join(Booking, Booking.id == Transaction.booking_id)
        .where(Booking.advisor_id == advisor_id)
        .order_by(Transaction.created_at.desc())
    )


async def get_for_party(
    session: AsyncSession, transaction_id: uuid.UUID, user_id: uuid.UUID
) -> Transaction:
    """A transaction, gated to its booking's seeker or advisor (admin bypasses
    this check at the endpoint layer instead of calling this function)."""
    txn = await session.get(Transaction, transaction_id)
    if txn is None:
        raise NotFoundError("Transaction not found")
    booking = await session.get(Booking, txn.booking_id)
    if booking is None or user_id not in (booking.seeker_id, booking.advisor_id):
        raise NotFoundError("Transaction not found")
    return txn


async def get_by_id(session: AsyncSession, transaction_id: uuid.UUID) -> Transaction:
    txn = await session.get(Transaction, transaction_id)
    if txn is None:
        raise NotFoundError("Transaction not found")
    return txn


_INVOICE_ELIGIBLE_STATUSES = (
    TransactionStatus.succeeded,
    TransactionStatus.partially_refunded,
    TransactionStatus.refunded,
)


async def build_invoice(session: AsyncSession, txn: Transaction, settings: Settings) -> InvoiceRead:
    if txn.status not in _INVOICE_ELIGIBLE_STATUSES:
        raise AppError("Invoice is only available for a paid transaction", code="not_paid")
    if txn.invoice_number is None:
        raise AppError("Invoice number not yet assigned", code="not_paid")

    booking = await session.get(Booking, txn.booking_id)
    if booking is None:
        raise NotFoundError("Booking not found")
    seeker = await session.get(User, booking.seeker_id)
    advisor = await session.get(User, booking.advisor_id)
    advisor_name = advisor.full_name if advisor else "Advisor"

    line_items = [
        InvoiceLineItem(
            description=f"{booking.service_type} consultation with {advisor_name}",
            quantity=1,
            unit_price_usd=txn.amount_usd,
            total_usd=txn.amount_usd,
        ),
        InvoiceLineItem(
            description="Platform service fee (included in total)",
            quantity=1,
            unit_price_usd=txn.commission_usd,
            total_usd=txn.commission_usd,
        ),
    ]

    return InvoiceRead(
        invoice_number=f"{txn.invoice_number:08d}",
        issued_date=txn.created_at,
        transaction_id=txn.id,
        booking_id=booking.id,
        from_name=settings.EMAILS_FROM_NAME,
        to_name=seeker.full_name if seeker else None,
        to_email=seeker.email if seeker else "",
        line_items=line_items,
        total_usd=txn.amount_usd,
        status=txn.status,
        refunded_amount_usd=txn.refunded_amount_usd,
        refunded_at=txn.refunded_at,
        refund_reason=txn.refund_reason,
    )


def list_all_stmt(
    status: TransactionStatus | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    search: str | None = None,
) -> Select[tuple[Transaction]]:
    """Full platform transaction list for admin Finance Management, with optional filters."""
    stmt = select(Transaction).join(Booking, Booking.id == Transaction.booking_id)

    if status is not None:
        stmt = stmt.where(Transaction.status == status)
    if date_from is not None:
        stmt = stmt.where(Transaction.created_at >= date_from)
    if date_to is not None:
        stmt = stmt.where(Transaction.created_at <= date_to)
    if search:
        seeker = aliased(User)
        advisor = aliased(User)
        stmt = (
            stmt.join(seeker, seeker.id == Booking.seeker_id)
            .join(advisor, advisor.id == Booking.advisor_id)
            .where(
                or_(
                    seeker.full_name.ilike(f"%{search}%"),
                    advisor.full_name.ilike(f"%{search}%"),
                )
            )
        )

    return stmt.order_by(Transaction.created_at.desc())


async def finance_read(session: AsyncSession, txn: Transaction) -> TransactionFinanceRead:
    """Enrich a transaction with its booking's seeker/advisor names for admin views."""
    booking = await session.get(Booking, txn.booking_id)
    if booking is None:
        raise NotFoundError("Booking not found")
    seeker = await session.get(User, booking.seeker_id)
    advisor = await session.get(User, booking.advisor_id)
    return TransactionFinanceRead(
        id=txn.id,
        booking_id=txn.booking_id,
        amount_usd=txn.amount_usd,
        commission_rate=txn.commission_rate,
        commission_usd=txn.commission_usd,
        tax_rate=txn.tax_rate,
        tax_usd=txn.tax_usd,
        advisor_payout_usd=txn.advisor_payout_usd,
        payment_method=txn.payment_method,
        invoice_number=txn.invoice_number,
        status=txn.status,
        stripe_payment_intent_id=txn.stripe_payment_intent_id,
        refunded_at=txn.refunded_at,
        refund_reason=txn.refund_reason,
        created_at=txn.created_at,
        refunded_by=txn.refunded_by,
        refunded_amount_usd=txn.refunded_amount_usd,
        stripe_checkout_session_id=txn.stripe_checkout_session_id,
        stripe_charge_id=txn.stripe_charge_id,
        seeker_id=booking.seeker_id,
        seeker_name=seeker.full_name if seeker else None,
        advisor_id=booking.advisor_id,
        advisor_name=advisor.full_name if advisor else None,
        service_type=booking.service_type,
        scheduled_start=booking.scheduled_start,
    )


async def list_events(session: AsyncSession, transaction_id: uuid.UUID) -> list[TransactionEvent]:
    result = await session.execute(
        select(TransactionEvent)
        .where(TransactionEvent.transaction_id == transaction_id)
        .order_by(TransactionEvent.occurred_at)
    )
    return list(result.scalars().all())
