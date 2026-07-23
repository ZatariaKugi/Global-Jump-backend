"""Stripe payment operations — checkout, webhook, refunds, Connect onboarding (PRD §3.10)."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import stripe
import structlog
from sqlalchemy import Select, String, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.config import Settings
from app.core.exceptions import AppError, NotFoundError
from app.core.file_storage import resolve_media_url
from app.models.advisor_profile import AdvisorProfile
from app.models.booking import Booking, BookingStatus, PaymentStatus
from app.models.transaction import Transaction, TransactionStatus
from app.models.transaction_event import TransactionEvent, TransactionEventType
from app.models.user import User, UserRole
from app.schemas.payment import (
    AdvisorConnectStatus,
    CheckoutResponse,
    InvoiceLineItem,
    InvoicePerspective,
    InvoiceRead,
    PaymentDisplayStatus,
    PaymentSummaryRead,
    SeekerPaymentRead,
    SeekerPaymentSummaryRead,
    TransactionAdvisorRead,
    TransactionFinanceRead,
)
from app.services import email_service

log = structlog.get_logger()

_INVOICE_TERMS = (
    "Payment is due upon receipt. This invoice reflects charges for consultation "
    "services arranged through the platform. Refunds are subject to the platform "
    "cancellation policy."
)


def display_status(txn: Transaction) -> PaymentDisplayStatus:
    if txn.status == TransactionStatus.succeeded:
        return "paid"
    if txn.status == TransactionStatus.pending:
        return "pending"
    if txn.status in (TransactionStatus.refunded, TransactionStatus.partially_refunded):
        return "refunded"
    return "failed"


def format_invoice_id(invoice_number: int | None) -> str | None:
    if invoice_number is None:
        return None
    return f"INV-{invoice_number:08d}"


def format_appointment_id(appointment_number: int) -> str:
    return f"#{appointment_number:07d}"


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
    # Pay-first: allow checkout while the request is still pending, or after
    # the advisor has confirmed. Reject terminal / non-payable states.
    if booking.status not in (BookingStatus.pending, BookingStatus.confirmed):
        raise AppError("Booking is not in a payable state", code="invalid_booking_state")
    if booking.payment_status != PaymentStatus.unpaid:
        raise AppError("Booking is already paid or refunded", code="already_paid")

    # Resume an existing open Checkout Session instead of hard-failing.
    existing = (
        await session.execute(select(Transaction).where(Transaction.booking_id == booking_id))
    ).scalar_one_or_none()
    if existing is not None:
        if existing.status != TransactionStatus.pending:
            raise AppError(
                "A checkout session already exists for this booking", code="duplicate_checkout"
            )
        try:
            checkout_session = await stripe.checkout.Session.retrieve_async(
                existing.stripe_checkout_session_id
            )
        except stripe.StripeError as exc:
            raise AppError(
                "Could not resume the existing checkout session", code="checkout_resume_failed"
            ) from exc
        url = getattr(checkout_session, "url", None)
        status = getattr(checkout_session, "status", None)
        if status == "open" and url:
            log.info(
                "checkout_session_resumed",
                booking_id=str(booking_id),
                session_id=existing.stripe_checkout_session_id,
            )
            return CheckoutResponse(checkout_url=url, session_id=checkout_session.id)
        # Session expired / completed — drop the pending txn and create a fresh one.
        await session.delete(existing)
        await session.flush()

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
    result = await session.execute(list_for_advisor_stmt(advisor_user_id))
    txns = list(result.scalars().all())

    total_earned = sum(
        (t.advisor_payout_usd for t in txns if t.status == TransactionStatus.succeeded),
        0.0,
    )
    total_commission = sum(
        (t.commission_usd for t in txns if t.status == TransactionStatus.succeeded),
        0.0,
    )

    return {
        "total_earned_usd": round(float(total_earned), 2),
        "total_commission_paid_usd": round(float(total_commission), 2),
        "transactions": txns,
    }


def list_for_advisor_stmt(advisor_id: uuid.UUID) -> Select[tuple[Transaction]]:
    """Non-archived transactions for an advisor's bookings (earnings / payments lists)."""
    return (
        select(Transaction)
        .join(Booking, Booking.id == Transaction.booking_id)
        .where(Booking.advisor_id == advisor_id)
        .where(Transaction.is_archived.is_(False))
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


async def build_invoice(
    session: AsyncSession,
    txn: Transaction,
    settings: Settings,
    *,
    perspective: InvoicePerspective = "seeker",
) -> InvoiceRead:
    if txn.status not in _INVOICE_ELIGIBLE_STATUSES:
        raise AppError("Invoice is only available for a paid transaction", code="not_paid")
    if txn.invoice_number is None:
        raise AppError("Invoice number not yet assigned", code="not_paid")

    booking = await session.get(Booking, txn.booking_id)
    if booking is None:
        raise NotFoundError("Booking not found")
    seeker = await session.get(User, booking.seeker_id)
    advisor = await session.get(User, booking.advisor_id)

    from app.models.seeker_profile import SeekerProfile

    seeker_profile = (
        await session.execute(
            select(SeekerProfile).where(SeekerProfile.user_id == booking.seeker_id)
        )
    ).scalar_one_or_none()
    to_address = None
    if seeker_profile and seeker_profile.country_of_residence:
        from app.core.countries import country_name

        code = seeker_profile.country_of_residence
        to_address = country_name(code) or code

    advisor_profile = (
        await session.execute(
            select(AdvisorProfile).where(AdvisorProfile.user_id == booking.advisor_id)
        )
    ).scalar_one_or_none()

    invoice_id = format_invoice_id(txn.invoice_number) or f"{txn.invoice_number:08d}"
    issued = txn.created_at

    from_phone = getattr(settings, "INVOICE_FROM_PHONE", None)
    to_phone = None  # no phone column on users/profiles yet

    if perspective == "advisor":
        from_name = (advisor.full_name if advisor else None) or "Advisor"
        from_address = None
        if advisor_profile and advisor_profile.country_of_residence:
            from app.core.countries import country_name

            code = advisor_profile.country_of_residence
            from_address = country_name(code) or code
        line_items = [
            InvoiceLineItem(
                description=booking.service_type,
                quantity=1,
                unit_price_usd=txn.amount_usd,
                total_usd=txn.amount_usd,
            )
        ]
        subtotal = txn.amount_usd
        tax = 0.0
        total = txn.amount_usd
    else:
        # seeker / admin — platform invoice split
        from_name = settings.EMAILS_FROM_NAME
        from_address = getattr(settings, "INVOICE_FROM_ADDRESS", None)
        line_items = [
            InvoiceLineItem(
                description="Platform Charges",
                quantity=1,
                unit_price_usd=txn.commission_usd,
                total_usd=txn.commission_usd,
            ),
            InvoiceLineItem(
                description="Consultant Fee",
                quantity=1,
                unit_price_usd=txn.advisor_payout_usd,
                total_usd=txn.advisor_payout_usd,
            ),
        ]
        if txn.tax_usd and txn.tax_usd > 0:
            line_items.append(
                InvoiceLineItem(
                    description="Tax",
                    quantity=1,
                    unit_price_usd=txn.tax_usd,
                    total_usd=txn.tax_usd,
                )
            )
        subtotal = round(txn.commission_usd + txn.advisor_payout_usd, 2)
        tax = txn.tax_usd
        total = txn.amount_usd

    return InvoiceRead(
        invoice_number=f"{txn.invoice_number:08d}",
        invoice_id=invoice_id,
        issued_date=issued,
        due_date=issued,
        transaction_id=txn.id,
        booking_id=booking.id,
        from_name=from_name,
        from_address=from_address,
        from_phone=from_phone,
        to_name=seeker.full_name if seeker else None,
        to_email=seeker.email if seeker else "",
        to_address=to_address,
        to_phone=to_phone,
        line_items=line_items,
        subtotal_usd=subtotal,
        tax_usd=tax,
        total_usd=total,
        status=txn.status,
        display_status=display_status(txn),
        refunded_amount_usd=txn.refunded_amount_usd,
        refunded_at=txn.refunded_at,
        refund_reason=txn.refund_reason,
        terms=_INVOICE_TERMS,
    )


def list_for_seeker_stmt(
    seeker_id: uuid.UUID,
    *,
    q: str | None = None,
    service_types: list[str] | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    sort: str = "-created_at",
) -> Select[tuple[Transaction]]:
    """Visa-seeker payment history with optional search / type / date filters."""
    stmt = (
        select(Transaction)
        .join(Booking, Booking.id == Transaction.booking_id)
        .outerjoin(User, User.id == Booking.advisor_id)
        .where(Booking.seeker_id == seeker_id)
        .where(Transaction.is_archived.is_(False))
    )
    if q:
        pattern = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                User.full_name.ilike(pattern),
                User.email.ilike(pattern),
                Booking.service_type.ilike(pattern),
                cast(Transaction.invoice_number, String).ilike(pattern),
            )
        )
    if service_types:
        stmt = stmt.where(Booking.service_type.in_(service_types))
    if date_from is not None:
        start = datetime(date_from.year, date_from.month, date_from.day, tzinfo=UTC)
        stmt = stmt.where(Transaction.created_at >= start)
    if date_to is not None:
        end = datetime(date_to.year, date_to.month, date_to.day, tzinfo=UTC) + timedelta(days=1)
        stmt = stmt.where(Transaction.created_at < end)

    if sort in ("created_at",):
        stmt = stmt.order_by(Transaction.created_at.asc())
    elif sort in ("amount_usd", "total_amount"):
        stmt = stmt.order_by(Transaction.amount_usd.asc())
    elif sort in ("-amount_usd", "-total_amount"):
        stmt = stmt.order_by(Transaction.amount_usd.desc())
    else:
        stmt = stmt.order_by(Transaction.created_at.desc())
    return stmt


async def seeker_payment_read(
    session: AsyncSession, txn: Transaction, settings: Settings
) -> SeekerPaymentRead:
    booking = await session.get(Booking, txn.booking_id)
    if booking is None:
        raise NotFoundError("Booking not found")
    advisor = await session.get(User, booking.advisor_id)
    advisor_profile = (
        await session.execute(
            select(AdvisorProfile).where(AdvisorProfile.user_id == booking.advisor_id)
        )
    ).scalar_one_or_none()
    return SeekerPaymentRead(
        id=txn.id,
        booking_id=txn.booking_id,
        invoice_id=format_invoice_id(txn.invoice_number),
        advisor_id=booking.advisor_id,
        advisor_name=advisor.full_name if advisor else None,
        advisor_email=advisor.email if advisor else None,
        advisor_photo_url=resolve_media_url(
            advisor_profile.profile_photo_url if advisor_profile else None, settings
        ),
        service_type=booking.service_type,
        created_at=txn.created_at,
        platform_fee_usd=txn.commission_usd,
        consultant_fee_usd=txn.advisor_payout_usd,
        amount_usd=txn.amount_usd,
        total_amount=txn.amount_usd,
        status=txn.status,
        display_status=display_status(txn),
        payment_method=txn.payment_method,
        stripe_payment_intent_id=txn.stripe_payment_intent_id,
        refunded_amount_usd=txn.refunded_amount_usd,
        refunded_at=txn.refunded_at,
        refund_reason=txn.refund_reason,
    )


async def seeker_payment_summary(
    session: AsyncSession, seeker_id: uuid.UUID
) -> SeekerPaymentSummaryRead:
    rows = (
        (
            await session.execute(
                select(Transaction)
                .join(Booking, Booking.id == Transaction.booking_id)
                .where(Booking.seeker_id == seeker_id)
                .where(Transaction.is_archived.is_(False))
                .order_by(Transaction.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    total_paid = 0.0
    pending_amount = 0.0
    refund_amount = 0.0
    for t in rows:
        if t.status in (TransactionStatus.succeeded, TransactionStatus.partially_refunded):
            total_paid += t.amount_usd
        if t.status == TransactionStatus.pending:
            pending_amount += t.amount_usd
        if t.status in (TransactionStatus.refunded, TransactionStatus.partially_refunded):
            if t.refunded_amount_usd is not None:
                refund_amount += t.refunded_amount_usd
            elif t.status == TransactionStatus.refunded:
                refund_amount += t.amount_usd
    last = rows[0] if rows else None
    return SeekerPaymentSummaryRead(
        total_paid_usd=round(total_paid, 2),
        pending_amount_usd=round(pending_amount, 2),
        refund_amount_usd=round(refund_amount, 2),
        last_transaction_usd=round(last.amount_usd, 2) if last else None,
    )


async def platform_payment_summary(session: AsyncSession) -> PaymentSummaryRead:
    paid = (
        await session.execute(
            select(func.coalesce(func.sum(Transaction.amount_usd), 0.0)).where(
                Transaction.is_archived.is_(False),
                Transaction.status.in_(
                    (TransactionStatus.succeeded, TransactionStatus.partially_refunded)
                ),
            )
        )
    ).scalar_one()
    refunded = (
        await session.execute(
            select(func.coalesce(func.sum(Transaction.refunded_amount_usd), 0.0)).where(
                Transaction.is_archived.is_(False),
                Transaction.refunded_amount_usd.is_not(None),
            )
        )
    ).scalar_one()
    commission = (
        await session.execute(
            select(func.coalesce(func.sum(Transaction.commission_usd), 0.0)).where(
                Transaction.is_archived.is_(False),
                Transaction.status.in_(
                    (TransactionStatus.succeeded, TransactionStatus.partially_refunded)
                ),
            )
        )
    ).scalar_one()
    tax = (
        await session.execute(
            select(func.coalesce(func.sum(Transaction.tax_usd), 0.0)).where(
                Transaction.is_archived.is_(False),
                Transaction.status.in_(
                    (TransactionStatus.succeeded, TransactionStatus.partially_refunded)
                ),
            )
        )
    ).scalar_one()
    return PaymentSummaryRead(
        total_paid_usd=round(float(paid or 0), 2),
        total_refunded_usd=round(float(refunded or 0), 2),
        total_commission_usd=round(float(commission or 0), 2),
        total_tax_usd=round(float(tax or 0), 2),
    )


def list_all_stmt(
    status: TransactionStatus | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    search: str | None = None,
) -> Select[tuple[Transaction]]:
    """Full platform transaction list for admin Finance Management, with optional filters."""
    stmt = (
        select(Transaction)
        .join(Booking, Booking.id == Transaction.booking_id)
        .where(Transaction.is_archived.is_(False))
    )

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
    from app.models.seeker_profile import SeekerProfile

    seeker_profile = (
        await session.execute(
            select(SeekerProfile).where(SeekerProfile.user_id == booking.seeker_id)
        )
    ).scalar_one_or_none()
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
        seeker_email=seeker.email if seeker else None,
        advisor_id=booking.advisor_id,
        advisor_name=advisor.full_name if advisor else None,
        advisor_email=advisor.email if advisor else None,
        service_type=booking.service_type,
        scheduled_start=booking.scheduled_start,
        invoice_id=format_invoice_id(txn.invoice_number),
        display_status=display_status(txn),
        seeker_country=seeker_profile.country_of_residence if seeker_profile else None,
    )


async def advisor_earnings_payment_read(
    session: AsyncSession, txn: Transaction, booking: Booking, seeker: User | None
) -> TransactionAdvisorRead:
    from app.models.seeker_profile import SeekerProfile

    seeker_profile = None
    if seeker is not None:
        seeker_profile = (
            await session.execute(
                select(SeekerProfile).where(SeekerProfile.user_id == seeker.id)
            )
        ).scalar_one_or_none()
    return TransactionAdvisorRead(
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
        seeker_id=booking.seeker_id,
        seeker_name=seeker.full_name if seeker else None,
        service_type=booking.service_type,
        scheduled_start=booking.scheduled_start,
        appointment_id=format_appointment_id(booking.appointment_number),
        invoice_id=format_invoice_id(txn.invoice_number),
        display_status=display_status(txn),
        seeker_photo_url=seeker_profile.profile_photo_url if seeker_profile else None,
        platform_fee_usd=txn.commission_usd,
        consultant_fee_usd=txn.advisor_payout_usd,
        net_amount_usd=txn.advisor_payout_usd,
    )


async def resend_receipt(
    session: AsyncSession, txn: Transaction, settings: Settings
) -> None:
    if txn.status not in _INVOICE_ELIGIBLE_STATUSES or txn.invoice_number is None:
        raise AppError("Receipt is only available for a paid transaction", code="not_paid")
    booking = await session.get(Booking, txn.booking_id)
    if booking is None:
        raise NotFoundError("Booking not found")
    seeker = await session.get(User, booking.seeker_id)
    advisor = await session.get(User, booking.advisor_id)
    if seeker is None:
        raise NotFoundError("Seeker not found")
    await email_service.send_payment_receipt_email(
        seeker.email,
        seeker.full_name or "",
        (advisor.full_name if advisor and advisor.full_name else "Advisor"),
        service_type=booking.service_type,
        amount_usd=txn.amount_usd,
        invoice_number=format_invoice_id(txn.invoice_number) or f"{txn.invoice_number:08d}",
        settings=settings,
    )


async def list_events(session: AsyncSession, transaction_id: uuid.UUID) -> list[TransactionEvent]:
    result = await session.execute(
        select(TransactionEvent)
        .where(TransactionEvent.transaction_id == transaction_id)
        .order_by(TransactionEvent.occurred_at)
    )
    return list(result.scalars().all())
