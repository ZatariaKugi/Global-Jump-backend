"""Stripe payment operations — checkout, webhook, refunds, Connect onboarding (PRD §3.10)."""

from __future__ import annotations

import asyncio
import csv
import io
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import stripe
import structlog
from sqlalchemy import Select, String, cast, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.config import Settings, get_settings
from app.core.exceptions import AppError, NotFoundError
from app.core.file_storage import resolve_media_url
from app.core.money import as_float
from app.db.session import async_session_factory
from app.models.advisor_profile import AdvisorProfile
from app.models.booking import Booking, BookingStatus, PaymentStatus
from app.models.notification import NotificationEntityType, NotificationType
from app.models.stripe_webhook_event import StripeWebhookEvent, WebhookEventStatus
from app.models.transaction import (
    ChargeModel,
    Transaction,
    TransactionStatus,
    TransferStatus,
)
from app.models.transaction_event import TransactionEvent, TransactionEventType
from app.models.transaction_refund import RefundStatus, TransactionRefund
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
    TransactionRefundRead,
)
from app.services import (
    advisor_earnings,
    booking_meeting_service,
    email_service,
    notification_service,
    payment_config_service,
    refund_engine,
    stripe_config_service,
    subscription_service,
    tax_service,
    zoom_connection_service,
)
from app.services.payment_config_service import PaymentConfig, compute_platform_fee

log = structlog.get_logger()

STRIPE_STANDARD_DASHBOARD_URL = "https://dashboard.stripe.com"


async def _notify_payment(
    session: AsyncSession,
    txn: Transaction,
    *,
    recipient_id: uuid.UUID,
    type: NotificationType,
    title: str,
    body: str,
    actor_id: uuid.UUID | None = None,
) -> None:
    await notification_service.notify(
        session,
        user_id=recipient_id,
        type=type,
        title=title,
        body=body,
        entity_type=NotificationEntityType.transaction,
        entity_id=txn.id,
        actor_id=actor_id,
    )


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


def _build_display_id(txn: Transaction) -> str:
    """Public display ID: INV-… for paid, TXN-… for pending/failed."""
    if txn.invoice_number is not None:
        return f"INV-{txn.invoice_number:08d}"
    # Use first 8 hex chars of the transaction UUID for a short, stable public ID.
    return f"TXN-{txn.id.hex[:8].upper()}"


def format_appointment_id(appointment_number: int) -> str:
    return f"#{appointment_number:07d}"


async def _init_stripe(session: AsyncSession, settings: Settings) -> None:
    """Point the SDK at the credentials in force: admin-saved, else the environment."""
    keys = await stripe_config_service.effective_keys(session, settings)
    if not keys.secret_key:
        raise AppError("Payment processing is not configured", code="stripe_not_configured")
    stripe.api_key = keys.secret_key


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


def _account_unusable(exc: stripe.StripeError) -> bool:
    """Whether Stripe has *told us* this connected account cannot be used from here.

    ``PermissionError`` is the platform account not being a Connect platform at all;
    ``InvalidRequestError`` is "No such account", which is what a connected account
    created by a different platform looks like after a key change. Both are answers
    about the account. Every other StripeError — auth, network, rate limit — is an
    answer about the connection, and nothing may be concluded from it.
    """
    return isinstance(exc, stripe.PermissionError | stripe.InvalidRequestError)


def _mark_connect_unusable(profile: AdvisorProfile) -> None:
    """Drop the cached readiness so the checkout gate stops trusting it.

    ``advisor_not_payable`` reads these columns, so while they say True a seeker
    reaches the payment step for an advisor this platform cannot pay. The account id
    is deliberately kept: it is evidence, and clearing it would destroy the only
    record of what the advisor previously onboarded.
    """
    profile.stripe_charges_enabled = False
    profile.stripe_payouts_enabled = False
    profile.stripe_details_submitted = False
    zoom_connection_service.sync_stripe_connect_flag(profile)


def _connect_unavailable(exc: Exception, **context: str) -> AppError:
    log.warning("stripe_connect_unavailable", error=str(exc)[:300], **context)
    return AppError(
        "Stripe could not be reached for your payout account. Please try again.",
        code="stripe_connect_unavailable",
    )


def _connect_setup_failed(exc: Exception, **context: str) -> AppError:
    """Onboarding could not even be started.

    The Stripe text names the platform account and its missing capabilities, which
    means nothing to an advisor, so it goes to the log and a code goes to the client.
    """
    log.warning("stripe_connect_setup_failed", error=str(exc)[:300], **context)
    return AppError(
        "Payouts cannot be set up right now. Please contact support.",
        code="stripe_connect_setup_failed",
    )


def _sync_connect_flags(profile: AdvisorProfile, account: object) -> None:
    """Copy the connected account's readiness flags onto the cached profile columns.

    Accepts a Stripe ``Account`` object or a webhook payload dict interchangeably
    (via ``_stripe_get``) so both the live-status path and the account.updated
    webhook can share it.
    """
    profile.stripe_charges_enabled = bool(_stripe_get(account, "charges_enabled", False))
    profile.stripe_payouts_enabled = bool(_stripe_get(account, "payouts_enabled", False))
    profile.stripe_details_submitted = bool(_stripe_get(account, "details_submitted", False))
    zoom_connection_service.sync_stripe_connect_flag(profile)


@dataclass(frozen=True, slots=True)
class ConsultationSplit:
    """How one consultation divides (QA 2026-10-08, section 27):

        seeker pays   = price + tax                 (``total_usd``)
        price         = commission + advisor gross  (``commission_usd`` + ``advisor_payout_usd``)

    Tax is Stripe Tax's job: it is added on top of the price at checkout from the seeker's
    billing address, recorded when the session completes (``tax_usd``), held by the
    platform, and never revenue nor the advisor's. Stripe's processing fee is likewise
    only known once the charge exists (``stripe_fee_usd``). So at creation ``tax_usd`` is
    0 and ``total_usd`` equals the price.
    """

    price_usd: Decimal
    commission_usd: Decimal
    application_fee_usd: Decimal
    advisor_payout_usd: Decimal
    tax_usd: Decimal
    commission_rate: Decimal  # effective fee / price, stored for reporting
    tax_rate: Decimal = Decimal("0.0000")  # fraction, for ``transactions.tax_rate``
    total_usd: Decimal = Decimal("0.00")


def compute_consultation_split(
    price_usd: Decimal | float, config: PaymentConfig
) -> ConsultationSplit:
    price = Decimal(str(price_usd)).quantize(Decimal("0.01"))
    fee = compute_platform_fee(config, price)
    rate = (fee / price).quantize(Decimal("0.0001")) if price > 0 else Decimal("0")
    return ConsultationSplit(
        price_usd=price,
        commission_usd=fee,
        application_fee_usd=fee,
        advisor_payout_usd=(price - fee).quantize(Decimal("0.01")),
        tax_usd=Decimal("0.00"),
        commission_rate=rate,
        total_usd=price,
    )


async def create_checkout_session(
    session: AsyncSession,
    booking_id: uuid.UUID,
    seeker_id: uuid.UUID,
    settings: Settings,
) -> CheckoutResponse:
    await _init_stripe(session, settings)

    # Row lock (PAY-106): a concurrent cancel/reschedule waits for this checkout.
    booking = (
        await session.execute(select(Booking).where(Booking.id == booking_id).with_for_update())
    ).scalar_one_or_none()
    if booking is None or booking.seeker_id != seeker_id:
        raise NotFoundError("Booking not found")
    # Pay-first: allow checkout while the request is still pending, or after
    # the advisor has confirmed. Reject terminal / non-payable states.
    if booking.status not in (BookingStatus.pending, BookingStatus.confirmed):
        raise AppError("Booking is not in a payable state", code="invalid_booking_state")
    if booking.payment_status != PaymentStatus.unpaid:
        raise AppError("Booking is already paid or refunded", code="already_paid")
    # Advisor-created (free) bookings are not payable through checkout.
    if booking.price_usd <= 0:
        raise AppError("Booking has no payable amount", code="not_payable")

    # Payout-readiness gate: the advisor must have a Connect account that can accept
    # transfers before a seeker can pay them — otherwise funds would land on the
    # platform with no destination for the delayed payout.
    advisor_profile = await _get_advisor_profile(session, booking.advisor_id)
    if advisor_profile.needs_stripe_connect:
        raise AppError("Advisor is not set up to receive payments yet", code="advisor_not_payable")
    if settings.zoom_oauth_enabled and advisor_profile.needs_zoom_connect:
        raise AppError(
            "Advisor has not connected Zoom for video consultations yet",
            code="advisor_zoom_not_connected",
        )

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

    # Fee split from the admin payment settings, snapshotted on the transaction so a
    # later settings change never rewrites this payment's numbers.
    config = await payment_config_service.get_config(session)
    live_keys = await stripe_config_service.effective_keys(session, settings)
    stripe_config_service.assert_live_allowed(live_keys, config)
    split = compute_consultation_split(booking.price_usd, config)

    advisor = await session.get(User, booking.advisor_id)
    advisor_name = advisor.full_name if advisor else "Advisor"

    # Destination charge. The seeker is charged the price (plus whatever Stripe Tax adds
    # when the admin switch is on) on the platform; Stripe moves exactly the advisor's
    # gross share to the connected account (``transfer_data.amount``), so commission and
    # tax both stay on the platform without ever appearing as an "application fee" on
    # the advisor's Stripe. Stripe's own processing fee is debited from the platform
    # whatever we send here (its docs are explicit, with or without on_behalf_of); the
    # advisor bears it by a reversal of the real fee once the charge exists -- see
    # ``_recover_stripe_fee``.
    fee_cents = int(round(float(split.application_fee_usd) * 100))
    advisor_cents = int(round(float(split.advisor_payout_usd) * 100))
    txn_id = uuid.uuid4()
    money_meta = {
        "booking_id": str(booking_id),
        "transaction_id": str(txn_id),
        "appointment_id": format_appointment_id(booking.appointment_number),
        "seeker_id": str(seeker_id),
        "advisor_id": str(booking.advisor_id),
        "advisor_stripe_account_id": advisor_profile.stripe_account_id or "",
        "payment_type": "consultation",
        "consultation_amount": f"{split.price_usd:.2f}",
        # Tax and the total are Stripe's to decide; both are stamped on completion.
        "tax_mode": "stripe_tax" if config.automatic_tax_enabled else "none",
        "platform_commission": f"{split.commission_usd:.2f}",
        "advisor_gross": f"{split.advisor_payout_usd:.2f}",
        "currency": "usd",
    }
    payment_intent_data: Any = {"metadata": dict(money_meta)}
    if advisor_cents > 0:
        payment_intent_data["transfer_data"] = {
            "destination": advisor_profile.stripe_account_id,
            "amount": advisor_cents,
        }
        if fee_cents <= 0:
            # Unchanged from before: with no commission the advisor is the settlement
            # merchant (their descriptor on the seeker's statement).
            payment_intent_data["on_behalf_of"] = advisor_profile.stripe_account_id
    # Otherwise the commission swallowed the whole price: nothing to transfer, and
    # Stripe rejects a zero transfer amount.
    price_data: dict[str, Any] = {
        "currency": "usd",
        "product_data": {
            "name": f"{booking.name} with {advisor_name}",
            "description": f"{booking.duration_minutes}-minute session",
        },
        "unit_amount": int(round(float(split.price_usd) * 100)),
    }
    session_params: dict[str, Any] = {}
    if config.automatic_tax_enabled:
        # Stripe Tax: the price is tax-exclusive, Stripe adds the tax line from the
        # seeker's billing address and the platform's registrations (Dashboard → Tax).
        # The product tax code is the Dashboard's preset unless one is set here.
        # Address collection is "auto" (PM, 2026-10-08): Stripe asks for the billing
        # address only when it needs it to place the seeker for tax.
        price_data["tax_behavior"] = "exclusive"
        session_params["automatic_tax"] = {"enabled": True}
        session_params["billing_address_collection"] = "auto"
    line_items: Any = [{"price_data": price_data, "quantity": 1}]
    # Same key within a minute -> Stripe returns the same session on a retry.
    checkout_key = f"checkout_{booking_id}_{datetime.now(UTC):%Y%m%d%H%M}"
    try:
        checkout_session = await stripe.checkout.Session.create_async(
            line_items=line_items,
            mode="payment",
            success_url=f"{settings.FRONTEND_URL}/bookings/{booking_id}?payment=success",
            cancel_url=f"{settings.FRONTEND_URL}/bookings/{booking_id}?payment=cancelled",
            metadata=dict(money_meta),
            payment_intent_data=payment_intent_data,
            managed_payments={"enabled": False},
            idempotency_key=checkout_key,
            **session_params,
        )
    except stripe.StripeError as exc:
        raise stripe_config_service.checkout_failed(exc, booking_id=str(booking_id)) from exc

    txn = Transaction(
        id=txn_id,
        booking_id=booking_id,
        stripe_checkout_session_id=checkout_session.id,
        amount_usd=float(split.total_usd),
        commission_rate=float(split.commission_rate),
        commission_usd=float(split.commission_usd),
        charge_model=ChargeModel.destination,
        application_fee_usd=float(split.application_fee_usd),
        tax_rate=0.0,
        tax_usd=0.0,
        advisor_payout_usd=float(split.advisor_payout_usd),
        status=TransactionStatus.pending,
        created_by=seeker_id,
    )
    session.add(txn)
    try:
        await session.flush()
    except IntegrityError as exc:
        # Concurrent checkout won the unique booking_id insert. Roll back our attempt
        # and return the winner's open session instead of surfacing a 500.
        await session.rollback()
        winner = (
            await session.execute(select(Transaction).where(Transaction.booking_id == booking_id))
        ).scalar_one_or_none()
        if winner is not None and winner.status == TransactionStatus.pending:
            resumed = await stripe.checkout.Session.retrieve_async(
                winner.stripe_checkout_session_id
            )
            resumed_url = getattr(resumed, "url", None)
            if resumed_url:
                return CheckoutResponse(checkout_url=resumed_url, session_id=resumed.id)
        raise AppError(
            "A checkout session already exists for this booking", code="duplicate_checkout"
        ) from exc
    await _log_event(session, txn.id, TransactionEventType.initiated)

    log.info(
        "checkout_session_created",
        booking_id=str(booking_id),
        session_id=checkout_session.id,
        amount_usd=float(split.total_usd),
        automatic_tax=config.automatic_tax_enabled,
    )
    return CheckoutResponse(checkout_url=checkout_session.url, session_id=checkout_session.id)


async def handle_webhook(
    payload: bytes,
    sig_header: str,
    settings: Settings,
    session: AsyncSession,
) -> None:
    await _init_stripe(session, settings)

    keys = await stripe_config_service.effective_keys(session, settings)
    if keys.webhook_secret:
        try:
            event = stripe.Webhook.construct_event(  # type: ignore[no-untyped-call]
                payload, sig_header, keys.webhook_secret
            )
        except (ValueError, stripe.SignatureVerificationError) as exc:
            raise AppError("Invalid webhook signature", code="invalid_signature") from exc
    else:
        # Unsigned JSON is a local-dev convenience only. In production an event we
        # cannot verify must never mark anything paid.
        if settings.is_production:
            raise AppError("Stripe webhook secret is not configured", code="stripe_not_configured")
        event = json.loads(payload)
        log.warning(
            "webhook_signature_verification_skipped",
            reason="no webhook secret configured in the admin panel",
        )

    await process_event(session, event, settings)


async def process_event(session: AsyncSession, event: object, settings: Settings) -> None:
    """Ledger-guarded dispatch of one verified Stripe event."""
    event_id = str(_stripe_get(event, "id") or "")
    event_type = str(_stripe_get(event, "type") or "")
    log.info("stripe_webhook", event_type=event_type, event_id=event_id)

    # Ledger first (PAY-106): a redelivered event that already succeeded or was
    # ignored never reaches a handler again; a failed one is re-run.
    row = await session.get(StripeWebhookEvent, event_id)
    if row is not None and row.status in (
        WebhookEventStatus.processed,
        WebhookEventStatus.ignored,
    ):
        log.info("stripe_webhook_duplicate", event_id=event_id, status=row.status.value)
        return
    if row is None:
        row = StripeWebhookEvent(
            event_id=event_id,
            event_type=event_type,
            status=WebhookEventStatus.received,
            attempts=0,
        )
        session.add(row)
    row.attempts = (row.attempts or 0) + 1
    row.status = WebhookEventStatus.received
    await session.flush()

    handler = _WEBHOOK_HANDLERS.get(event_type)
    if handler is None:
        row.status = WebhookEventStatus.ignored
        row.processed_at = datetime.now(UTC)
        await session.flush()
        return

    try:
        await handler(session, _stripe_get(_stripe_get(event, "data"), "object"), settings)
    except Exception as exc:
        # Drop whatever the handler half-wrote, then persist the failure on its own
        # so the record survives the route's rollback; re-raise so Stripe retries.
        await session.rollback()
        failed = await session.get(StripeWebhookEvent, event_id)
        if failed is None:
            failed = StripeWebhookEvent(event_id=event_id, event_type=event_type, attempts=0)
            session.add(failed)
        failed.attempts = (failed.attempts or 0) + 1
        failed.status = WebhookEventStatus.failed
        failed.error = str(exc)[:500]
        if failed.attempts >= _WEBHOOK_ALERT_ATTEMPTS:
            await notification_service.notify_admins(
                session,
                type=NotificationType.webhook_processing_failed,
                title="Stripe webhook keeps failing",
                body=f"{event_type} {event_id} failed {failed.attempts} times: {failed.error}",
            )
        await session.commit()
        log.exception("stripe_webhook_handler_failed", event_id=event_id, event_type=event_type)
        raise

    row.status = WebhookEventStatus.processed
    row.processed_at = datetime.now(UTC)
    row.error = None
    await session.flush()


async def _on_checkout_completed(session: AsyncSession, obj: object, settings: Settings) -> None:
    await _handle_checkout_completed(session, obj, settings)


async def _on_checkout_expired(session: AsyncSession, obj: object, settings: Settings) -> None:
    await _handle_checkout_expired(session, obj)


async def _on_charge_refunded(session: AsyncSession, obj: object, settings: Settings) -> None:
    await _handle_charge_refunded(session, obj)


async def _on_account_updated(session: AsyncSession, obj: object, settings: Settings) -> None:
    await _handle_account_updated(session, obj)


async def _on_payment_intent_failed(session: AsyncSession, obj: object, settings: Settings) -> None:
    """A card was declined on a consultation checkout. The Checkout Session stays
    open for a retry, so only the seeker is told; no state changes."""
    booking_id = _stripe_get(_stripe_get(obj, "metadata") or {}, "booking_id")
    if not booking_id:
        return
    booking = await session.get(Booking, uuid.UUID(str(booking_id)))
    if booking is None:
        return
    await notification_service.notify(
        session,
        user_id=booking.seeker_id,
        type=NotificationType.payment_failed,
        title="Payment did not go through",
        body=f"Your card was declined for {booking.name}. You can try again from Appointments.",
        entity_type=NotificationEntityType.booking,
        entity_id=booking.id,
    )


async def _on_refund_updated(session: AsyncSession, obj: object, settings: Settings) -> None:
    """charge.refund.updated: a refund we created moved to failed/canceled on Stripe."""
    refund_id = str(_stripe_get(obj, "id") or "")
    status = str(_stripe_get(obj, "status") or "")
    if not refund_id or status not in ("failed", "canceled"):
        return
    row = (
        await session.execute(
            select(TransactionRefund).where(TransactionRefund.stripe_refund_id == refund_id)
        )
    ).scalar_one_or_none()
    if row is None or row.status == RefundStatus.failed:
        return
    row.status = RefundStatus.failed
    row.last_error = f"Stripe reported the refund as {status}"
    session.add(row)
    await session.flush()
    await notification_service.notify_admins(
        session,
        type=NotificationType.refund_failed,
        title="Refund failed on Stripe",
        body=f"Refund {refund_id} is {status}; the seeker has not received the money",
        entity_type=NotificationEntityType.transaction,
        entity_id=row.transaction_id,
    )


async def _on_transfer_reversed(session: AsyncSession, obj: object, settings: Settings) -> None:
    """transfer.reversed: reconcile a reversal made outside the refund engine (dashboard)."""
    transfer_id = str(_stripe_get(obj, "id") or "")
    txn = (
        await session.execute(
            select(Transaction).where(Transaction.stripe_transfer_id == transfer_id)
        )
    ).scalar_one_or_none()
    if txn is None:
        return
    reversed_cents = _stripe_get(obj, "amount_reversed")
    if isinstance(reversed_cents, (int, float)):
        known = as_float(txn.advisor_reversed_usd)
        live = round(int(reversed_cents) / 100, 2)
        if live > known:
            txn.advisor_reversed_usd = live
            session.add(txn)
            await _log_event(session, txn.id, TransactionEventType.transfer_reversed)
            await session.flush()


async def _on_subscription_event(session: AsyncSession, obj: object, settings: Settings) -> None:
    await subscription_service.on_subscription_event(session, obj, settings)


async def _on_invoice_paid(session: AsyncSession, obj: object, settings: Settings) -> None:
    await subscription_service.on_invoice_paid(session, obj, settings)


async def _on_invoice_payment_failed(
    session: AsyncSession, obj: object, settings: Settings
) -> None:
    await subscription_service.on_invoice_payment_failed(session, obj, settings)


async def _on_customer_updated(session: AsyncSession, obj: object, settings: Settings) -> None:
    await subscription_service.on_customer_updated(session, obj, settings)


async def _on_payment_method_attached(
    session: AsyncSession, obj: object, settings: Settings
) -> None:
    await subscription_service.on_payment_method_attached(session, obj, settings)


_WEBHOOK_ALERT_ATTEMPTS = 3


_WEBHOOK_HANDLERS: dict[str, Callable[[AsyncSession, object, Settings], Awaitable[None]]] = {
    "checkout.session.completed": _on_checkout_completed,
    "checkout.session.expired": _on_checkout_expired,
    "charge.refunded": _on_charge_refunded,
    "account.updated": _on_account_updated,
    "payment_intent.payment_failed": _on_payment_intent_failed,
    "charge.refund.updated": _on_refund_updated,
    "transfer.reversed": _on_transfer_reversed,
    "customer.subscription.created": _on_subscription_event,
    "customer.subscription.updated": _on_subscription_event,
    "customer.subscription.deleted": _on_subscription_event,
    "invoice.paid": _on_invoice_paid,
    "invoice.payment_failed": _on_invoice_payment_failed,
    # Billing Portal card changes land on the customer / payment method, not the
    # subscription, so the cached card summary refreshes from these too.
    "customer.updated": _on_customer_updated,
    "payment_method.attached": _on_payment_method_attached,
}


async def _handle_account_updated(session: AsyncSession, account: object) -> None:
    """Refresh an advisor's cached Connect readiness flags from an account.updated event."""
    account_id = str(_stripe_get(account, "id") or "")
    if not account_id:
        return
    profile = (
        await session.execute(
            select(AdvisorProfile).where(AdvisorProfile.stripe_account_id == account_id)
        )
    ).scalar_one_or_none()
    if profile is None:
        log.warning("webhook_account_updated_no_profile", account_id=account_id)
        return
    was_payouts_enabled = profile.stripe_payouts_enabled
    _sync_connect_flags(profile, account)
    session.add(profile)
    await session.flush()
    if not was_payouts_enabled and profile.stripe_payouts_enabled:
        await notification_service.notify(
            session,
            user_id=profile.user_id,
            type=NotificationType.connect_payouts_enabled,
            title="Stripe payouts enabled",
            body="Your Stripe account is verified — you can now receive payments",
            entity_type=NotificationEntityType.user,
            entity_id=profile.user_id,
        )
    log.info(
        "connect_account_updated",
        account_id=account_id,
        charges_enabled=profile.stripe_charges_enabled,
        payouts_enabled=profile.stripe_payouts_enabled,
    )


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


def _stripe_get(obj: object, key: str, default: object = None) -> object:
    """Read a field from a StripeObject or plain dict (webhook payload either way)."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    try:
        return obj[key]  # type: ignore[index]
    except (KeyError, TypeError, AttributeError):
        return default


async def _handle_checkout_completed(session: AsyncSession, cs: object, settings: Settings) -> None:
    if str(_stripe_get(cs, "mode") or "") == "subscription":
        await subscription_service.on_checkout_completed(session, cs, settings)
        return
    session_id = str(_stripe_get(cs, "id") or "")
    log.info("webhook_checkout_started", session_id=session_id)

    txn_result = await session.execute(
        select(Transaction).where(Transaction.stripe_checkout_session_id == session_id)
    )
    txn = txn_result.scalar_one_or_none()
    if txn is None:
        log.warning("webhook_checkout_no_txn", session_id=session_id)
        return

    log.info(
        "webhook_checkout_txn_found",
        session_id=session_id,
        txn_id=str(txn.id),
        current_status=txn.status.value,
        booking_id=str(txn.booking_id),
    )

    # Idempotent against duplicate webhook delivery — Stripe may deliver the same
    # event more than once. If we've already processed this checkout, do nothing
    # (re-running would re-arm the hold and re-send the receipt).
    if txn.status == TransactionStatus.succeeded:
        log.info("webhook_checkout_already_processed", session_id=session_id)
        return

    raw_pi = _stripe_get(cs, "payment_intent")
    pi_id: str | None = None
    if isinstance(raw_pi, str):
        pi_id = raw_pi
    elif raw_pi is not None:
        pi_id = str(_stripe_get(raw_pi, "id") or "") or None

    charge_id: str | None = None
    card_brand: str | None = None
    card_last4: str | None = None
    transfer_id: str | None = None
    app_fee_id: str | None = None
    stripe_fee_cents = 0
    balance_txn_id: str | None = None
    if pi_id:
        try:
            pi = await stripe.PaymentIntent.retrieve_async(
                pi_id, expand=["latest_charge", "latest_charge.balance_transaction"]
            )
            latest_charge = _stripe_get(pi, "latest_charge")
            if latest_charge:
                charge_id = (
                    latest_charge["id"]
                    if isinstance(latest_charge, dict)
                    else str(_stripe_get(latest_charge, "id") or latest_charge)
                )
                raw_transfer = _stripe_get(latest_charge, "transfer")
                transfer_id = (
                    str(_stripe_get(raw_transfer, "id") or raw_transfer) if raw_transfer else None
                )
                raw_fee = _stripe_get(latest_charge, "application_fee")
                app_fee_id = str(_stripe_get(raw_fee, "id") or raw_fee) if raw_fee else None
                # Stripe's own processing fee, from the charge's balance transaction.
                # Only an expanded object carries it; a bare id means "unknown" -> 0.
                raw_bt = _stripe_get(latest_charge, "balance_transaction")
                if raw_bt is not None and not isinstance(raw_bt, str):
                    fee_value = _stripe_get(raw_bt, "fee")
                    if isinstance(fee_value, (int, float)):
                        stripe_fee_cents = int(fee_value)
                    bt_id = _stripe_get(raw_bt, "id")
                    balance_txn_id = str(bt_id) if bt_id else None
                # Extract card brand + last-4 from charge payment_method_details.
                pmd = _stripe_get(latest_charge, "payment_method_details")
                if pmd is not None:
                    card = _stripe_get(pmd, "card")
                    if card is not None:
                        card_brand = str(_stripe_get(card, "brand") or None) or None
                        card_last4 = str(_stripe_get(card, "last4") or None) or None
        except stripe.StripeError as exc:
            log.warning("webhook_pi_retrieve_failed", pi_id=pi_id, error=str(exc))

    # What Stripe Tax added, if anything: the seeker paid price + tax, and every later
    # screen reads the tax, its label and jurisdiction from the row.
    tax = await tax_service.snapshot_for_session(cs, session_id)
    if tax.applies:
        price = advisor_earnings.consultation_usd(txn)
        txn.tax_usd = float(tax.amount_usd)
        txn.tax_rate = float(tax.rate) if tax.rate_percent > 0 else 0.0
        txn.tax_label = tax.label
        txn.tax_country = tax.country
        txn.tax_jurisdiction = tax.jurisdiction
        txn.amount_usd = (
            float(tax.total_usd) if tax.total_usd else round(price + float(tax.amount_usd), 2)
        )

    txn.status = TransactionStatus.succeeded
    txn.stripe_payment_intent_id = pi_id
    txn.stripe_charge_id = charge_id
    txn.card_brand = card_brand
    txn.card_last4 = card_last4
    txn.stripe_fee_usd = round(stripe_fee_cents / 100, 2)
    txn.stripe_balance_transaction_id = balance_txn_id
    txn.invoice_number = await _next_invoice_number(session)
    session.add(txn)
    await _log_event(session, txn.id, TransactionEventType.authorized)
    await _log_event(session, txn.id, TransactionEventType.completed)
    await _log_event(session, txn.id, TransactionEventType.invoice_generated)
    if txn.charge_model == ChargeModel.destination:
        # The advisor was paid inside the charge: nothing to hold, nothing to sweep.
        txn.stripe_transfer_id = transfer_id
        txn.stripe_application_fee_id = app_fee_id
        txn.transfer_status = TransferStatus.completed
        txn.transfer_after = None
        await _log_event(session, txn.id, TransactionEventType.transfer_completed)
        await _recover_stripe_fee(session, txn, stripe_fee_cents)
        await _stamp_payment_metadata(txn)
        if stripe_fee_cents <= 0 and balance_txn_id is None:
            # Stripe writes the charge's balance transaction a beat after the charge,
            # so at this instant it is sometimes not there yet. Two real payments on
            # 2026-10-08 recorded $0 that way. PM's rule: check again after 30 seconds.
            schedule_stripe_fee_check(txn.id)
    else:
        # Legacy separate-charge row: arm the hold for the sweep as before.
        txn.transfer_after = datetime.now(UTC) + timedelta(minutes=settings.PAYOUT_HOLD_MINUTES)
        txn.transfer_status = TransferStatus.pending
        await _log_event(session, txn.id, TransactionEventType.transfer_scheduled)

    booking = await session.get(Booking, txn.booking_id)
    if booking is not None:
        booking.payment_status = PaymentStatus.paid
        if booking.paid_at is None:
            booking.paid_at = datetime.now(UTC)
        if booking.status == BookingStatus.pending:
            booking.status = BookingStatus.confirmed
            booking.confirmed_at = datetime.now(UTC)
        session.add(booking)

    await session.flush()

    log.info(
        "payment_succeeded_db_updated",
        booking_id=str(txn.booking_id),
        amount_usd=txn.amount_usd,
        txn_status=txn.status.value,
        booking_payment_status=booking.payment_status.value if booking else None,
        invoice_number=txn.invoice_number,
    )

    if booking is not None:
        seeker = await session.get(User, booking.seeker_id)
        advisor = await session.get(User, booking.advisor_id)
        if seeker is not None:
            email_service.schedule_email(
                email_service.send_payment_receipt_email(
                    seeker.email,
                    seeker.full_name or seeker.email,
                    advisor.full_name if advisor and advisor.full_name else "Advisor",
                    name=booking.name,
                    amount_usd=txn.amount_usd,
                    invoice_number=f"{txn.invoice_number:08d}",
                    settings=settings,
                    consultation_usd=advisor_earnings.consultation_usd(txn),
                    tax_usd=as_float(txn.tax_usd),
                    tax_label=txn.tax_label,
                )
            )
        if advisor is not None:
            email_service.schedule_email(
                email_service.send_advisor_payment_notification_email(
                    advisor.email,
                    advisor.full_name or advisor.email,
                    seeker.full_name if seeker and seeker.full_name else "A client",
                    name=booking.name,
                    amount_usd=advisor_earnings.consultation_usd(txn),
                    payout_usd=advisor_earnings.advisor_net_earnings(txn),
                    invoice_number=f"{txn.invoice_number:08d}",
                    settings=settings,
                    platform_fee_usd=as_float(txn.commission_usd),
                    stripe_fee_usd=as_float(txn.stripe_fee_usd),
                )
            )
        await _notify_payment(
            session,
            txn,
            recipient_id=booking.seeker_id,
            type=NotificationType.payment_succeeded,
            title="Payment received",
            body=f"${txn.amount_usd:.2f} paid for {booking.name}",
            actor_id=booking.seeker_id,
        )
        await _notify_payment(
            session,
            txn,
            recipient_id=booking.advisor_id,
            type=NotificationType.payment_succeeded,
            title="Consultation paid",
            body=f"Your client paid ${txn.amount_usd:.2f} for {booking.name}",
            actor_id=booking.seeker_id,
        )
        # Notify advisor of the new booking request only after payment is confirmed
        from app.models.notification import NotificationEntityType

        await notification_service.notify(
            session,
            user_id=booking.advisor_id,
            type=NotificationType.booking_requested,
            title="New consultation request",
            body=f"Payment received for {booking.name}",
            entity_type=NotificationEntityType.booking,
            entity_id=booking.id,
            actor_id=booking.seeker_id,
        )
        if booking.status == BookingStatus.confirmed:
            await notification_service.notify(
                session,
                user_id=booking.seeker_id,
                type=NotificationType.booking_confirmed,
                title="Booking confirmed",
                body=f"Your {booking.name} session has been confirmed",
                entity_type=NotificationEntityType.booking,
                entity_id=booking.id,
                actor_id=booking.advisor_id,
            )
        await booking_meeting_service.maybe_provision_meeting(session, booking, settings)
    await _log_event(session, txn.id, TransactionEventType.receipt_sent)
    await _log_event(session, txn.id, TransactionEventType.closed)


async def _recover_stripe_fee(session: AsyncSession, txn: Transaction, fee_cents: int) -> None:
    """Pull Stripe's processing fee back from the advisor (QA 2026-10-08, section 9).

    On a destination charge the platform's balance pays Stripe's fee. The business rule
    is that the advisor bears it, so exactly the real fee is reversed off their transfer
    the moment the payment is confirmed. Advisor net = gross share - this fee; the
    platform keeps its full commission.

    Best effort on purpose: the seeker has paid and the advisor has been paid, so a
    failed recovery (the advisor already paid out, an account restriction) must not
    leave the booking unpaid. The fee is still recorded; ``stripe_fee_reversal_id``
    stays NULL, the failure is on the row and the timeline, and the refund engine then
    treats the gross share as still with the advisor.
    """
    if fee_cents <= 0 or not txn.stripe_transfer_id or txn.stripe_fee_reversal_id:
        return
    try:
        reversal = await stripe.Transfer.create_reversal_async(
            txn.stripe_transfer_id,
            amount=fee_cents,
            metadata={
                "transaction_id": str(txn.id),
                "booking_id": str(txn.booking_id),
                "reason": "stripe_processing_fee",
                "stripe_fee": f"{fee_cents / 100:.2f}",
            },
            idempotency_key=f"stripe_fee_{txn.id}",
        )
    except stripe.StripeError as exc:
        txn.transfer_last_error = f"Stripe fee recovery failed: {exc}"[:500]
        await _log_event(session, txn.id, TransactionEventType.transfer_failed)
        log.warning(
            "stripe_fee_recovery_failed",
            transaction_id=str(txn.id),
            transfer_id=txn.stripe_transfer_id,
            fee_cents=fee_cents,
            error=str(exc),
        )
        return
    txn.stripe_fee_reversal_id = str(reversal.id)
    await _log_event(session, txn.id, TransactionEventType.stripe_fee_recovered)


# Stripe's fee can be missing at ``checkout.session.completed``; it is read again
# after this many seconds, this many times, in the background (PM, 2026-10-09).
STRIPE_FEE_RETRY_SECONDS = 30
STRIPE_FEE_RETRY_ATTEMPTS = 3

_background_tasks: set[asyncio.Task[None]] = set()


def pending_background_tasks() -> list[asyncio.Task[None]]:
    """The fee checks still running (tests and shutdown await them)."""
    return [t for t in _background_tasks if not t.done()]


def schedule_stripe_fee_check(transaction_id: uuid.UUID) -> asyncio.Task[None] | None:
    """Look at the charge again later, in the background, for the fee we could not read.

    Fire-and-forget on the running loop, like ``email_service.schedule_email``; the
    webhook response is never delayed. Nothing here depends on the (switched-off)
    scheduler.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        log.warning("stripe_fee_check_no_loop", transaction_id=str(transaction_id))
        return None
    task = loop.create_task(_stripe_fee_check(transaction_id))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


async def _stripe_fee_check(transaction_id: uuid.UUID) -> None:
    """Wait, re-read the charge with its balance transaction, record and recover the fee.

    Each attempt opens its own session: the webhook's session is long gone. A Stripe
    error on one attempt is logged and the next attempt still runs; after the last
    attempt the row keeps $0 and says so in ``transfer_last_error`` so the admin sheet
    shows the fee as unknown rather than silently absent.
    """
    settings = get_settings()
    for attempt in range(1, STRIPE_FEE_RETRY_ATTEMPTS + 1):
        await asyncio.sleep(STRIPE_FEE_RETRY_SECONDS)
        try:
            async with async_session_factory() as session:
                txn = await session.get(Transaction, transaction_id)
                if txn is None:
                    return
                if not txn.stripe_charge_id:
                    continue  # the webhook's own write may not have landed yet
                if txn.stripe_balance_transaction_id or as_float(txn.stripe_fee_usd) > 0:
                    return  # someone else (a redelivered webhook) got there first
                await _init_stripe(session, settings)
                charge = await stripe.Charge.retrieve_async(
                    str(txn.stripe_charge_id), expand=["balance_transaction"]
                )
                raw_bt = _stripe_get(charge, "balance_transaction")
                fee_value = (
                    _stripe_get(raw_bt, "fee")
                    if raw_bt is not None and not isinstance(raw_bt, str)
                    else None
                )
                if not isinstance(fee_value, (int, float)) or int(fee_value) <= 0:
                    log.info(
                        "stripe_fee_still_unknown",
                        transaction_id=str(transaction_id),
                        attempt=attempt,
                    )
                    if attempt == STRIPE_FEE_RETRY_ATTEMPTS:
                        txn.transfer_last_error = (
                            f"Stripe fee unknown after {attempt} checks; "
                            "recorded as 0, not taken from the advisor"
                        )[:500]
                        session.add(txn)
                        await session.commit()
                    continue
                fee_cents = int(fee_value)
                txn.stripe_fee_usd = round(fee_cents / 100, 2)
                txn.stripe_balance_transaction_id = str(_stripe_get(raw_bt, "id") or "") or None
                session.add(txn)
                await _recover_stripe_fee(session, txn, fee_cents)
                await _stamp_payment_metadata(txn)
                await session.commit()
                log.info(
                    "stripe_fee_recorded_late",
                    transaction_id=str(transaction_id),
                    attempt=attempt,
                    fee_cents=fee_cents,
                )
                return
        except stripe.StripeError as exc:
            log.warning(
                "stripe_fee_check_failed",
                transaction_id=str(transaction_id),
                attempt=attempt,
                error=str(exc)[:200],
            )
        except Exception:  # noqa: BLE001 - a background check must never take the app down
            log.exception("stripe_fee_check_crashed", transaction_id=str(transaction_id))
            return


async def _stamp_payment_metadata(txn: Transaction) -> None:
    """Put the figures that were unknown at checkout time on the PaymentIntent, so the
    platform's Stripe shows tax, total, Stripe fee and advisor net next to the payment
    (QA section 8). Best effort: the books are already right."""
    if not txn.stripe_payment_intent_id:
        return
    try:
        await stripe.PaymentIntent.modify_async(
            txn.stripe_payment_intent_id,
            metadata={
                "tax_amount": f"{as_float(txn.tax_usd):.2f}",
                "tax_label": txn.tax_label or "",
                "tax_jurisdiction": txn.tax_jurisdiction or "",
                "total_amount": f"{as_float(txn.amount_usd):.2f}",
                "stripe_fee": f"{as_float(txn.stripe_fee_usd):.2f}",
                "advisor_net": f"{advisor_earnings.advisor_net_earnings(txn):.2f}",
            },
        )
    except stripe.StripeError as exc:
        log.warning("payment_metadata_stamp_failed", transaction_id=str(txn.id), error=str(exc))


async def _handle_checkout_expired(session: AsyncSession, cs: object) -> None:
    session_id = str(_stripe_get(cs, "id") or "")
    txn_result = await session.execute(
        select(Transaction).where(Transaction.stripe_checkout_session_id == session_id)
    )
    txn = txn_result.scalar_one_or_none()
    if txn is None:
        return
    # Idempotent against duplicate webhook delivery: only a still-pending checkout
    # can expire. If it already succeeded (a completed event won the race) or was
    # already marked failed, do nothing — re-running would re-notify the seeker.
    if txn.status != TransactionStatus.pending:
        log.info("webhook_checkout_expired_already_processed", session_id=session_id)
        return
    txn.status = TransactionStatus.failed
    session.add(txn)
    await _log_event(session, txn.id, TransactionEventType.failed)
    await _log_event(session, txn.id, TransactionEventType.closed)
    booking = await session.get(Booking, txn.booking_id)
    if booking is not None:
        # If the booking is still pending (advisor hasn't accepted), cancel it
        # to release the slot. If the advisor already accepted (confirmed), keep
        # the booking so the seeker can retry payment.
        if booking.status == BookingStatus.pending:
            booking.status = BookingStatus.cancelled
            booking.cancellation_reason = "Payment failed — checkout expired"
            session.add(booking)
            await _log_event(session, txn.id, TransactionEventType.failed)
            log.info(
                "booking_cancelled_payment_failed",
                booking_id=str(booking.id),
                session_id=session_id,
            )
        await _notify_payment(
            session,
            txn,
            recipient_id=booking.seeker_id,
            type=NotificationType.payment_failed,
            title="Checkout expired",
            body=f"Your payment for {booking.name} was not completed",
        )
        # Notify advisor if they were already notified of the request
        if booking.status == BookingStatus.cancelled:
            from app.services import notification_service

            await notification_service.notify(
                session,
                user_id=booking.advisor_id,
                type=NotificationType.booking_cancelled,
                title="Consultation request cancelled",
                body="The consultation request was cancelled due to a failed payment",
                entity_type=NotificationEntityType.booking,
                entity_id=booking.id,
            )
    await session.flush()
    log.info("checkout_expired", session_id=session_id)


async def _handle_charge_refunded(session: AsyncSession, charge: object) -> None:
    charge_id = str(_stripe_get(charge, "id") or "")
    txn_result = await session.execute(
        select(Transaction).where(Transaction.stripe_charge_id == charge_id)
    )
    txn = txn_result.scalar_one_or_none()
    if txn is None:
        log.warning("webhook_refund_no_txn", charge_id=charge_id)
        return

    amount_refunded_cents = _stripe_get(charge, "amount_refunded")
    refunded_amount_usd = (
        round(int(amount_refunded_cents) / 100, 2)
        if isinstance(amount_refunded_cents, (int, float))
        else as_float(txn.amount_usd)
    )
    # Tax and Stripe's fee never come back, so "fully refunded" is measured against
    # what could come back (QA 2026-10-08), the same rule the engine applies.
    is_full = Decimal(str(refunded_amount_usd)) >= refund_engine.refundable_total(txn)

    # Idempotent against duplicate webhook delivery and against a refund the engine
    # already settled (it sets these same fields): Stripe may redeliver
    # charge.refunded, and an engine refund also triggers one.
    # Only proceed when this event reflects *more* refunded than we've recorded
    # (e.g. a genuine partial→larger escalation from the dashboard); otherwise the
    # refund is already accounted for — don't re-notify the seeker or re-log events.
    already_refunded = as_float(txn.refunded_amount_usd)
    if (
        txn.status
        in (
            TransactionStatus.refunded,
            TransactionStatus.partially_refunded,
        )
        and refunded_amount_usd <= already_refunded
    ):
        log.info("webhook_refund_already_processed", charge_id=charge_id)
        return
    txn.status = TransactionStatus.refunded if is_full else TransactionStatus.partially_refunded
    txn.refunded_amount_usd = refunded_amount_usd
    txn.refunded_at = datetime.now(UTC)
    # A refund that arrives while the payout is still held cancels it (e.g. a refund
    # issued directly from the Stripe dashboard).
    if txn.transfer_status == TransferStatus.pending:
        txn.transfer_status = TransferStatus.cancelled
    session.add(txn)
    await _log_event(session, txn.id, TransactionEventType.refunded)
    if is_full:
        await _log_event(session, txn.id, TransactionEventType.closed)

    booking = await session.get(Booking, txn.booking_id)
    if booking is not None:
        booking.payment_status = PaymentStatus.refunded
        session.add(booking)
        await _notify_payment(
            session,
            txn,
            recipient_id=booking.seeker_id,
            type=NotificationType.payment_refunded,
            title="Payment refunded",
            body=f"${refunded_amount_usd:.2f} refunded for {booking.name}",
        )

    await session.flush()
    log.info("payment_refunded_via_webhook", booking_id=str(txn.booking_id))


async def refund_booking_payment(
    session: AsyncSession,
    booking_id: uuid.UUID,
    initiated_by: uuid.UUID | None,
    reason: str | None,
    settings: Settings,
    *,
    kind: str = "advisor_cancel",
) -> Transaction:
    """Refund the payment tied to a booking through the refund engine."""
    txn = (
        await session.execute(
            select(Transaction).where(Transaction.booking_id == booking_id).with_for_update()
        )
    ).scalar_one_or_none()
    if txn is None:
        raise NotFoundError("Transaction not found")
    config = await payment_config_service.get_config(session)
    plan = refund_engine.compute_refund(txn, kind, config)
    await refund_engine.execute_refund(session, txn, plan, initiated_by, reason, settings)
    await session.refresh(txn)
    return txn


async def auto_refund_booking_if_paid(
    session: AsyncSession,
    booking: Booking,
    actor_id: uuid.UUID | None,
    reason: str | None,
    settings: Settings,
    *,
    kind: str = "advisor_cancel",
) -> Decimal | None:
    """Best-effort refund when a paid booking is cancelled, rejected or expires.

    ``kind`` picks the policy: ``advisor_cancel`` refunds the advisor share (fee per
    the admin setting); ``rejection`` / ``expiry`` refund everything. Failures are
    recorded by the engine and never block the booking state change. Returns the
    amount refunded to the seeker, or None.
    """
    if booking.payment_status != PaymentStatus.paid or booking.price_usd <= 0:
        return None
    try:
        txn = (
            await session.execute(
                select(Transaction).where(Transaction.booking_id == booking.id).with_for_update()
            )
        ).scalar_one_or_none()
        if txn is None:
            raise NotFoundError("Transaction not found")
        config = await payment_config_service.get_config(session)
        plan = refund_engine.compute_refund(txn, kind, config)
        row = await refund_engine.execute_refund(session, txn, plan, actor_id, reason, settings)
    except (AppError, NotFoundError) as exc:
        code = exc.code if isinstance(exc, AppError) else "not_found"
        log.warning(
            "booking_auto_refund_failed", booking_id=str(booking.id), code=code, detail=str(exc)
        )
        return None
    except stripe.StripeError as exc:
        log.warning(
            "booking_auto_refund_failed",
            booking_id=str(booking.id),
            code="stripe_error",
            detail=str(exc),
        )
        return None
    log.info("booking_auto_refunded", booking_id=str(booking.id), kind=kind)
    return Decimal(str(row.refund_to_seeker_usd))


# After this many failed transfer attempts the sweep gives up and marks the txn
# transfer_status=failed so it needs manual intervention (funds stay on the platform).
_MAX_TRANSFER_ATTEMPTS = 5


async def run_due_transfers(session: AsyncSession, settings: Settings) -> int:
    """Sweep: transfer the advisor's share for payments whose hold window has elapsed.

    Selects succeeded transactions with ``transfer_status == pending`` and
    ``transfer_after <= now``, locking each row ``FOR UPDATE SKIP LOCKED`` so
    overlapping sweeps (or multiple app instances) never process the same row twice.
    Each transfer uses ``idempotency_key = txn.id`` so a retry after a crash between
    the Stripe call and the DB commit returns the same transfer instead of creating
    a second one. Returns the number of transfers completed this pass.
    """
    await _init_stripe(session, settings)

    now = datetime.now(UTC)
    due = (
        (
            await session.execute(
                select(Transaction)
                .where(
                    Transaction.charge_model == ChargeModel.separate_transfer,
                    Transaction.status == TransactionStatus.succeeded,
                    Transaction.transfer_status == TransferStatus.pending,
                    Transaction.transfer_after.is_not(None),
                    Transaction.transfer_after <= now,
                )
                .with_for_update(skip_locked=True)
                .limit(100)
            )
        )
        .scalars()
        .all()
    )

    completed = 0
    for txn in due:
        booking = await session.get(Booking, txn.booking_id)
        if booking is None:
            continue
        advisor_profile = (
            await session.execute(
                select(AdvisorProfile).where(AdvisorProfile.user_id == booking.advisor_id)
            )
        ).scalar_one_or_none()
        if advisor_profile is None or not advisor_profile.stripe_account_id:
            # Shouldn't happen (checkout gates on this), but never transfer without
            # a destination — leave pending and record the reason.
            txn.transfer_attempts += 1
            txn.transfer_last_error = "advisor has no connected account"
            session.add(txn)
            continue

        transfer_params: dict[str, object] = {
            "amount": int(round(txn.advisor_payout_usd * 100)),
            "currency": "usd",
            "destination": advisor_profile.stripe_account_id,
            "metadata": {"transaction_id": str(txn.id), "booking_id": str(txn.booking_id)},
        }
        # Tie the transfer to the original charge so it draws from those funds.
        if txn.stripe_charge_id:
            transfer_params["source_transaction"] = txn.stripe_charge_id
        try:
            # Same overloaded-kwargs stub mismatch as the checkout / refund calls; the
            # idempotency_key makes a retry after a mid-call crash return the same
            # transfer instead of creating a second one.
            transfer = await stripe.Transfer.create_async(
                **transfer_params,  # type: ignore[arg-type]
                idempotency_key=f"transfer_{txn.id}",
            )
        except stripe.StripeError as exc:
            txn.transfer_attempts += 1
            txn.transfer_last_error = str(exc)[:500]
            if txn.transfer_attempts >= _MAX_TRANSFER_ATTEMPTS:
                txn.transfer_status = TransferStatus.failed
                await _log_event(session, txn.id, TransactionEventType.transfer_failed)
                await _notify_payment(
                    session,
                    txn,
                    recipient_id=booking.advisor_id,
                    type=NotificationType.transfer_failed,
                    title="Payout transfer failed",
                    body=(
                        f"Your ${txn.advisor_payout_usd:.2f} payout could not be transferred — "
                        "support has been notified"
                    ),
                )
                await notification_service.notify_admins(
                    session,
                    type=NotificationType.transfer_failed,
                    title="Advisor transfer failed",
                    body=(
                        f"Transfer of ${txn.advisor_payout_usd:.2f} failed terminally "
                        f"after {txn.transfer_attempts} attempts"
                    ),
                    entity_type=NotificationEntityType.transaction,
                    entity_id=txn.id,
                )
                log.error(
                    "transfer_failed_terminal",
                    transaction_id=str(txn.id),
                    attempts=txn.transfer_attempts,
                    error=str(exc),
                )
            else:
                log.warning(
                    "transfer_attempt_failed",
                    transaction_id=str(txn.id),
                    attempts=txn.transfer_attempts,
                    error=str(exc),
                )
            session.add(txn)
            continue

        txn.stripe_transfer_id = transfer.id
        txn.transfer_status = TransferStatus.completed
        session.add(txn)
        await _log_event(session, txn.id, TransactionEventType.transfer_completed)
        await _notify_payment(
            session,
            txn,
            recipient_id=booking.advisor_id,
            type=NotificationType.transfer_completed,
            title="Earnings released",
            body=f"${txn.advisor_payout_usd:.2f} has been transferred to your Stripe account",
        )
        completed += 1
        log.info(
            "transfer_completed",
            transaction_id=str(txn.id),
            transfer_id=transfer.id,
            amount_usd=txn.advisor_payout_usd,
        )

    await session.flush()
    return completed


async def create_connect_account(
    session: AsyncSession,
    advisor_user: User,
    settings: Settings,
) -> AdvisorConnectStatus:
    await _init_stripe(session, settings)

    if advisor_user.role != UserRole.advisor:
        raise AppError("Only advisors can connect a Stripe account", code="wrong_role")

    profile = await _get_advisor_profile(session, advisor_user.id)

    if not profile.stripe_account_id:
        try:
            account = await stripe.Account.create_async(
                type="express",
                email=advisor_user.email,
                capabilities={
                    "card_payments": {"requested": True},
                    "transfers": {"requested": True},
                },
                metadata={"user_id": str(advisor_user.id)},
            )
        except stripe.StripeError as exc:
            # Most often the platform account has not finished its own Stripe setup,
            # so it cannot act as a Connect platform at all. The advisor can do
            # nothing about that, but they must be told rather than shown a 500.
            raise _connect_setup_failed(exc, advisor_id=str(advisor_user.id)) from exc
        profile.stripe_account_id = account.id
        session.add(profile)
        await session.flush()
        log.info(
            "stripe_connect_account_created", advisor_id=str(advisor_user.id), account_id=account.id
        )

    try:
        account_link = await stripe.AccountLink.create_async(
            account=profile.stripe_account_id,
            refresh_url=f"{settings.FRONTEND_URL}/advisor/connect/refresh",
            return_url=f"{settings.FRONTEND_URL}/advisor/connect/return",
            type="account_onboarding",
        )
    except stripe.StripeError as exc:
        raise _connect_setup_failed(exc, advisor_id=str(advisor_user.id)) from exc
    # Reflect any cached readiness for advisors resuming onboarding (the flags are
    # authoritatively refreshed by get_connect_status / the account.updated webhook).
    return AdvisorConnectStatus(
        stripe_account_id=profile.stripe_account_id,
        charges_enabled=profile.stripe_charges_enabled,
        payouts_enabled=profile.stripe_payouts_enabled,
        onboarding_complete=profile.stripe_details_submitted and profile.stripe_charges_enabled,
        onboarding_url=account_link.url,
        account_type="express",
    )


async def create_stripe_dashboard_url(
    session: AsyncSession,
    advisor_user_id: uuid.UUID,
    settings: Settings,
) -> str:
    """Return a URL the advisor can open to manage payouts in Stripe.

    Express connected accounts receive a single-use Express Dashboard login link.
    Standard (OAuth) accounts are sent to the public Stripe Dashboard — they sign
    in with their own Stripe credentials there.
    """
    await _init_stripe(session, settings)
    profile = await _get_advisor_profile(session, advisor_user_id)
    if not profile.stripe_account_id:
        raise NotFoundError("No Stripe account connected")

    try:
        account = await stripe.Account.retrieve_async(profile.stripe_account_id)
    except stripe.StripeError as exc:
        if _account_unusable(exc):
            raise AppError(
                "Your payout account is no longer connected. Please set it up again.",
                code="stripe_connect_account_unusable",
            ) from exc
        raise _connect_unavailable(exc, advisor_user_id=str(advisor_user_id)) from exc
    account_type = _stripe_get(account, "type", None)
    if account_type == "express":
        try:
            login_link = await stripe.Account.create_login_link_async(profile.stripe_account_id)
        except stripe.StripeError as exc:
            raise AppError(
                "Unable to open your Stripe dashboard. Please try again.",
                code="stripe_dashboard_unavailable",
            ) from exc
        url = getattr(login_link, "url", None)
        if not isinstance(url, str) or not url:
            raise AppError(
                "Unable to open your Stripe dashboard. Please try again.",
                code="stripe_dashboard_unavailable",
            )
        return url

    return STRIPE_STANDARD_DASHBOARD_URL


async def disconnect_connect_account(
    session: AsyncSession,
    advisor_user_id: uuid.UUID,
) -> None:
    """Clear the advisor's linked Stripe Connect account from the platform."""
    profile = await _get_advisor_profile(session, advisor_user_id)
    profile.stripe_account_id = None
    profile.stripe_charges_enabled = False
    profile.stripe_payouts_enabled = False
    profile.stripe_details_submitted = False
    zoom_connection_service.sync_stripe_connect_flag(profile)
    session.add(profile)
    await session.commit()
    log.info("stripe_connect_disconnected", advisor_id=str(advisor_user_id))


async def get_connect_status(
    session: AsyncSession,
    advisor_user_id: uuid.UUID,
    settings: Settings,
) -> AdvisorConnectStatus:
    await _init_stripe(session, settings)

    profile = await _get_advisor_profile(session, advisor_user_id)

    if not profile.stripe_account_id:
        return AdvisorConnectStatus(
            stripe_account_id=None,
            charges_enabled=False,
            onboarding_complete=False,
        )

    try:
        account = await stripe.Account.retrieve_async(profile.stripe_account_id)
    except stripe.StripeError as exc:
        if not _account_unusable(exc):
            raise _connect_unavailable(exc, advisor_user_id=str(advisor_user_id)) from exc
        # Stripe has answered: this account cannot be used from the configured
        # platform. Report it as not ready rather than failing the whole panel —
        # the advisor needs to see the state and re-onboard, not a retry button
        # for a call that cannot succeed.
        log.warning(
            "stripe_connect_account_unusable",
            advisor_user_id=str(advisor_user_id),
            stripe_account_id=profile.stripe_account_id,
            error=str(exc)[:200],
        )
        _mark_connect_unusable(profile)
        session.add(profile)
        await session.flush()
        return AdvisorConnectStatus(
            stripe_account_id=profile.stripe_account_id,
            charges_enabled=False,
            payouts_enabled=False,
            onboarding_complete=False,
        )

    # Refresh the cached readiness flags on every live status check so the checkout
    # gate stays current even between account.updated webhook deliveries.
    _sync_connect_flags(profile, account)
    session.add(profile)
    await session.flush()

    account_type = _stripe_get(account, "type", None)

    return AdvisorConnectStatus(
        stripe_account_id=profile.stripe_account_id,
        charges_enabled=profile.stripe_charges_enabled,
        payouts_enabled=profile.stripe_payouts_enabled,
        onboarding_complete=profile.stripe_details_submitted and profile.stripe_charges_enabled,
        account_type=account_type if isinstance(account_type, str) else None,
    )


async def get_advisor_earnings(
    session: AsyncSession,
    advisor_user_id: uuid.UUID,
) -> dict[str, object]:
    result = await session.execute(list_for_advisor_stmt(advisor_user_id))
    txns = list(result.scalars().all())

    # The five-line breakdown plus Total Refunded (QA 2026-10-08, sections 5 and 17),
    # over the same rows every other earnings surface uses: charged, transfer complete.
    sums = (
        await session.execute(
            select(
                func.coalesce(func.sum(advisor_earnings.CONSULTATION_USD), 0.0),
                func.coalesce(func.sum(Transaction.commission_usd), 0.0),
                func.coalesce(func.sum(Transaction.advisor_payout_usd), 0.0),
                func.coalesce(func.sum(Transaction.stripe_fee_usd), 0.0),
                func.coalesce(func.sum(Transaction.advisor_reversed_usd), 0.0),
                func.coalesce(func.sum(advisor_earnings.ADVISOR_NET_EARNINGS), 0.0),
            )
            .join(Booking, Booking.id == Transaction.booking_id)
            .where(
                Booking.advisor_id == advisor_user_id,
                *advisor_earnings.advisor_earnings_filters(),
            )
        )
    ).one()
    gross, platform_fee, advisor_gross, stripe_fee, reversed_, net = (float(v) for v in sums)

    return {
        "total_earned_usd": round(net, 2),
        "total_commission_paid_usd": round(platform_fee, 2),
        "transactions": txns,
        "total_gross_revenue_usd": round(gross, 2),
        "total_platform_fee_usd": round(platform_fee, 2),
        "total_advisor_gross_usd": round(advisor_gross, 2),
        "total_stripe_fee_usd": round(stripe_fee, 2),
        "total_refunded_usd": round(reversed_, 2),
    }


def list_for_advisor_stmt(
    advisor_id: uuid.UUID,
    *,
    q: str | None = None,
    service_ids: list[uuid.UUID] | None = None,
) -> Select[tuple[Transaction]]:
    """Non-archived transactions for an advisor's bookings (earnings / payments lists).

    ``q`` searches seeker name / email and the human-readable appointment id
    (accepts the ``#0000000`` display form or a bare number); ``service_ids``
    filters on the booking's snapshotted service type.
    """
    stmt = (
        select(Transaction)
        .join(Booking, Booking.id == Transaction.booking_id)
        .outerjoin(User, User.id == Booking.seeker_id)
        .where(Booking.advisor_id == advisor_id)
        .where(Transaction.is_archived.is_(False))
    )
    if q:
        needle = q.strip()
        pattern = f"%{needle}%"
        conditions = [
            User.full_name.ilike(pattern),
            User.email.ilike(pattern),
        ]
        digits = needle.lstrip("#").lstrip("0")
        if digits.isdigit():
            conditions.append(cast(Booking.appointment_number, String).like(f"%{digits}%"))
        stmt = stmt.where(or_(*conditions))
    if service_ids:
        stmt = stmt.where(Booking.service_id.in_(service_ids))
    return stmt.order_by(Transaction.created_at.desc())


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

    consultation = advisor_earnings.consultation_usd(txn)
    tax_amount = round(as_float(txn.tax_usd), 2)
    tax_rate_percent = round(as_float(txn.tax_rate) * 100, 2) if tax_amount > 0 else 0.0
    tax_label = txn.tax_label if tax_amount > 0 else None
    line_items = [
        InvoiceLineItem(
            description=booking.name,
            quantity=1,
            unit_price_usd=consultation,
            total_usd=consultation,
        )
    ]
    if perspective == "advisor":
        # The advisor's copy is their consultation only: the tax is the platform's.
        from_name = (advisor.full_name if advisor else None) or "Advisor"
        from_address = None
        if advisor_profile and advisor_profile.country_of_residence:
            from app.core.countries import country_name

            code = advisor_profile.country_of_residence
            from_address = country_name(code) or code
        subtotal = consultation
        tax = 0.0
        total = consultation
        tax_rate_percent, tax_label = 0.0, None
    else:
        # seeker / admin: Consultation + Tax = Total, exactly what was charged (QA §4).
        from_name = settings.EMAILS_FROM_NAME
        from_address = getattr(settings, "INVOICE_FROM_ADDRESS", None)
        subtotal = consultation
        tax = tax_amount
        total = round(as_float(txn.amount_usd), 2)

    # The invoice names the refunded amount only. It deliberately never itemises the
    # platform fee, the Stripe fee or a "retained" figure (PM, 2026-10-07): the status
    # badge ("Partially refunded") carries that fact, and none of it is the seeker's.
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
        tax_rate_percent=tax_rate_percent,
        tax_label=tax_label,
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
    service_ids: list[uuid.UUID] | None = None,
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
                Booking.name.ilike(pattern),
                cast(Transaction.invoice_number, String).ilike(pattern),
            )
        )
    if service_ids:
        stmt = stmt.where(Booking.service_id.in_(service_ids))
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
        display_id=_build_display_id(txn),
        advisor_id=booking.advisor_id,
        advisor_name=advisor.full_name if advisor else None,
        advisor_email=advisor.email if advisor else None,
        advisor_photo_url=resolve_media_url(
            advisor_profile.profile_photo_url if advisor_profile else None, settings
        ),
        service_id=booking.service_id,
        name=booking.name,
        created_at=txn.created_at,
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


_SEEKER_EXPORT_MAX_ROWS = 5_000

_SEEKER_CSV_HEADERS = (
    "Invoice ID",
    "Advisor",
    "Advisor Email",
    "Services",
    "Date",
    "Total Amount",
    "Status",
)


async def export_seeker_history_csv(
    session: AsyncSession,
    seeker_id: uuid.UUID,
    settings: Settings,
    *,
    q: str | None = None,
    service_ids: list[uuid.UUID] | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    sort: str = "-created_at",
) -> str:
    """CSV body for the seeker Payments Export button (same filters as history)."""
    stmt = list_for_seeker_stmt(
        seeker_id,
        q=q,
        service_ids=service_ids,
        date_from=date_from,
        date_to=date_to,
        sort=sort,
    ).limit(_SEEKER_EXPORT_MAX_ROWS)
    txns = list((await session.execute(stmt)).scalars().all())
    rows = [await seeker_payment_read(session, t, settings) for t in txns]

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(_SEEKER_CSV_HEADERS)
    for row in rows:
        writer.writerow(
            [
                row.invoice_id or "",
                row.advisor_name or "",
                row.advisor_email or "",
                row.name,
                row.created_at.strftime("%Y-%m-%d %H:%M:%S UTC"),
                f"{row.total_amount:.2f}",
                row.display_status,
            ]
        )
    return buf.getvalue()


async def seeker_payment_summary(
    session: AsyncSession,
    seeker_id: uuid.UUID,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
) -> SeekerPaymentSummaryRead:
    stmt = (
        select(Transaction)
        .join(Booking, Booking.id == Transaction.booking_id)
        .where(Booking.seeker_id == seeker_id)
        .where(Transaction.is_archived.is_(False))
    )
    if date_from is not None:
        start = datetime(date_from.year, date_from.month, date_from.day, tzinfo=UTC)
        stmt = stmt.where(Transaction.created_at >= start)
    if date_to is not None:
        end = datetime(date_to.year, date_to.month, date_to.day, tzinfo=UTC) + timedelta(days=1)
        stmt = stmt.where(Transaction.created_at < end)
    stmt = stmt.order_by(Transaction.created_at.desc())

    rows = list((await session.execute(stmt)).scalars().all())
    total_paid = Decimal("0")
    pending_amount = Decimal("0")
    refund_amount = Decimal("0")
    for t in rows:
        amount = Decimal(str(t.amount_usd))
        if t.status in (TransactionStatus.succeeded, TransactionStatus.partially_refunded):
            total_paid += amount
        if t.status == TransactionStatus.pending:
            pending_amount += amount
        if t.status in (TransactionStatus.refunded, TransactionStatus.partially_refunded):
            if t.refunded_amount_usd is not None:
                refund_amount += Decimal(str(t.refunded_amount_usd))
            elif t.status == TransactionStatus.refunded:
                refund_amount += amount
    last = rows[0] if rows else None
    return SeekerPaymentSummaryRead(
        total_paid_usd=float(round(total_paid, 2)),
        pending_amount_usd=float(round(pending_amount, 2)),
        refund_amount_usd=float(round(refund_amount, 2)),
        last_transaction_usd=float(round(Decimal(str(last.amount_usd)), 2)) if last else None,
    )


async def platform_payment_summary(session: AsyncSession) -> PaymentSummaryRead:
    # "Seeker Paid" answers one question -- how much did seekers hand over for
    # consultations -- so a refund never reduces it. Refunds have their own card
    # beside it, and `amount_usd` is never adjusted, so this figure only goes up.
    #
    # `refunded` is in the row set for that reason. Leaving it out meant a *full*
    # refund silently removed the original price while a partial one changed
    # nothing: $99 refunded off $100 moved the card by $0, the hundredth dollar
    # moved it by $100. It also made `Paid - Refunded` subtract across two
    # different row sets (QA bug 7); it now reconciles.
    #
    # `pending` and `failed` stay out: an abandoned or expired checkout is money
    # that never arrived.
    paid = (
        await session.execute(
            select(func.coalesce(func.sum(Transaction.amount_usd), 0.0)).where(
                Transaction.is_archived.is_(False),
                Transaction.status.in_(
                    (
                        TransactionStatus.succeeded,
                        TransactionStatus.partially_refunded,
                        TransactionStatus.refunded,
                    )
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
    # Platform Commission = fee collected minus fee returned (QA bug 7, second half,
    # 2026-10-07). Same row set as Seeker Paid: a full refund that kept the fee still
    # collected it, and a partial refund that returned part of the fee gives it back.
    commission = (
        await session.execute(
            select(
                func.coalesce(
                    func.sum(Transaction.commission_usd - Transaction.platform_fee_refunded_usd),
                    0.0,
                )
            ).where(*advisor_earnings.charged_filters())
        )
    ).scalar_one()
    # Tax is collected on every charged row and never refunded (QA 2026-10-08), so it
    # is summed over the same row set as Seeker Paid. It is held for the authority and
    # is not platform revenue; the Stripe fee card below is the advisors' cost.
    tax, stripe_fee = (
        await session.execute(
            select(
                func.coalesce(func.sum(Transaction.tax_usd), 0.0),
                func.coalesce(func.sum(Transaction.stripe_fee_usd), 0.0),
            ).where(*advisor_earnings.charged_filters())
        )
    ).one()
    # Advisor earnings: the advisor share of every completed transfer, less anything
    # reversed back off the connected account (QA bug 8). Two things differ from the
    # four figures above and both are deliberate.
    #
    # The row set includes `refunded`. The cards above count `succeeded` +
    # `partially_refunded` only, so a full refund drops the row out; subtracting the
    # reversal from a sum that never included the payout would report *negative*
    # earnings for a booking the advisor was simply never paid for. Counting the
    # payout and letting the reversal cancel it reads zero, which is the truth.
    #
    # Reversals are attributed by `refunded_at`, matching how Financial Analytics
    # attributes refunds. That keeps this figure equal to the "Advisor Earnings" card
    # on Financial Analytics (`analytics_service._finance_window_totals`) over the same
    # rows, so an admin cannot read two different earnings numbers off two screens.
    # The agreement is pinned by test_qa_advisor_earnings_finance_card_red.py.
    # Gross share less Stripe's fee, which the advisor bears (QA 2026-10-08).
    advisor_earned = (
        await session.execute(
            select(
                func.coalesce(
                    func.sum(Transaction.advisor_payout_usd - Transaction.stripe_fee_usd), 0.0
                )
            ).where(
                Transaction.is_archived.is_(False),
                Transaction.transfer_status == TransferStatus.completed,
                Transaction.status.in_(
                    (
                        TransactionStatus.succeeded,
                        TransactionStatus.partially_refunded,
                        TransactionStatus.refunded,
                    )
                ),
            )
        )
    ).scalar_one()
    advisor_reversed = (
        await session.execute(
            select(func.coalesce(func.sum(Transaction.advisor_reversed_usd), 0.0)).where(
                Transaction.is_archived.is_(False),
                Transaction.refunded_at.is_not(None),
            )
        )
    ).scalar_one()
    return PaymentSummaryRead(
        total_paid_usd=round(float(paid or 0), 2),
        total_refunded_usd=round(float(refunded or 0), 2),
        total_commission_usd=round(float(commission or 0), 2),
        total_tax_usd=round(float(tax or 0), 2),
        total_advisor_earnings_usd=round(
            float(advisor_earned or 0) - float(advisor_reversed or 0), 2
        ),
        total_stripe_fee_usd=round(float(stripe_fee or 0), 2),
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


def _refund_rule(r: TransactionRefund) -> Any:
    """The QA name of the rule a ledger row applied (section 20, "Refund Rule")."""
    kind = r.kind.value
    if kind == "advisor_cancel":
        return "seeker_keeps_fee" if r.fee_policy_refunded else "admin_keeps_fee"
    if kind in ("rejection", "expiry"):
        return "platform_fault"
    return "admin"


def refund_read(r: TransactionRefund) -> TransactionRefundRead:
    return TransactionRefundRead(
        id=r.id,
        transaction_id=r.transaction_id,
        kind=r.kind.value,
        status=r.status.value,
        refund_to_seeker_usd=as_float(r.refund_to_seeker_usd),
        advisor_reversed_usd=as_float(r.advisor_reversed_usd),
        platform_fee_refunded_usd=as_float(r.platform_fee_refunded_usd),
        fee_policy_refunded=r.fee_policy_refunded,
        refund_rule=_refund_rule(r),
        stripe_refund_id=r.stripe_refund_id,
        stripe_reversal_id=r.stripe_reversal_id,
        reason=r.reason,
        initiated_by=r.initiated_by,
        last_error=r.last_error,
        created_at=r.created_at,
    )


def refunds_stmt(status: str | None) -> Select[tuple[TransactionRefund]]:
    """Platform-wide refund ledger, newest first; ``status`` narrows it."""
    stmt = select(TransactionRefund).order_by(TransactionRefund.created_at.desc())
    if status is not None:
        stmt = stmt.where(TransactionRefund.status == RefundStatus(status))
    return stmt


async def refund_reads(
    session: AsyncSession, transaction_id: uuid.UUID
) -> list[TransactionRefundRead]:
    rows = (
        (
            await session.execute(
                select(TransactionRefund)
                .where(TransactionRefund.transaction_id == transaction_id)
                .order_by(TransactionRefund.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [refund_read(r) for r in rows]


def webhook_events_stmt(status: str | None = None) -> Select[tuple[StripeWebhookEvent]]:
    stmt = select(StripeWebhookEvent).order_by(StripeWebhookEvent.received_at.desc())
    if status:
        stmt = stmt.where(StripeWebhookEvent.status == WebhookEventStatus(status))
    return stmt


async def retry_webhook_event(
    session: AsyncSession, event_id: str, settings: Settings
) -> StripeWebhookEvent:
    """Admin retry: fetch the event from Stripe by id and run it through the dispatcher."""
    row = await session.get(StripeWebhookEvent, event_id)
    if row is None:
        raise NotFoundError("Webhook event not found")
    if row.status in (WebhookEventStatus.processed, WebhookEventStatus.ignored):
        return row
    await _init_stripe(session, settings)
    event = await stripe.Event.retrieve_async(event_id)
    await process_event(session, event, settings)
    refreshed = await session.get(StripeWebhookEvent, event_id)
    return refreshed or row


async def finance_read(
    session: AsyncSession, txn: Transaction, settings: Settings
) -> TransactionFinanceRead:
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
    advisor_profile = (
        await session.execute(
            select(AdvisorProfile).where(AdvisorProfile.user_id == booking.advisor_id)
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
        tax_label=txn.tax_label,
        tax_country=txn.tax_country,
        tax_jurisdiction=txn.tax_jurisdiction,
        stripe_fee_usd=as_float(txn.stripe_fee_usd),
        stripe_fee_recovered=bool(txn.stripe_fee_reversal_id),
        stripe_fee_reversal_id=txn.stripe_fee_reversal_id,
        stripe_balance_transaction_id=txn.stripe_balance_transaction_id,
        advisor_net_usd=advisor_earnings.advisor_net_earnings(txn),
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
        charge_model=txn.charge_model.value,
        application_fee_usd=as_float(txn.application_fee_usd),
        advisor_reversed_usd=as_float(txn.advisor_reversed_usd),
        platform_fee_refunded_usd=as_float(txn.platform_fee_refunded_usd),
        refunds=await refund_reads(session, txn.id),
        seeker_id=booking.seeker_id,
        seeker_name=seeker.full_name if seeker else None,
        seeker_email=seeker.email if seeker else None,
        advisor_id=booking.advisor_id,
        advisor_name=advisor.full_name if advisor else None,
        advisor_email=advisor.email if advisor else None,
        service_id=booking.service_id,
        name=booking.name,
        scheduled_start=booking.scheduled_start,
        invoice_id=format_invoice_id(txn.invoice_number),
        display_id=_build_display_id(txn),
        display_status=display_status(txn),
        seeker_phone=seeker_profile.phone if seeker_profile else None,
        advisor_phone=advisor_profile.phone if advisor_profile else None,
        seeker_country=seeker_profile.country_of_residence if seeker_profile else None,
        seeker_photo_url=resolve_media_url(
            seeker_profile.profile_photo_url if seeker_profile else None, settings
        ),
        advisor_photo_url=resolve_media_url(
            advisor_profile.profile_photo_url if advisor_profile else None, settings
        ),
        card_brand=txn.card_brand,
        card_last4=txn.card_last4,
        transfer_status=txn.transfer_status.value if txn.transfer_status else None,
        admin_note=txn.admin_note,
    )


async def advisor_earnings_payment_read(
    session: AsyncSession, txn: Transaction, booking: Booking, seeker: User | None
) -> TransactionAdvisorRead:
    from app.models.seeker_profile import SeekerProfile

    seeker_profile = None
    if seeker is not None:
        seeker_profile = (
            await session.execute(select(SeekerProfile).where(SeekerProfile.user_id == seeker.id))
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
        seeker_email=seeker.email if seeker else None,
        service_id=booking.service_id,
        name=booking.name,
        scheduled_start=booking.scheduled_start,
        appointment_id=format_appointment_id(booking.appointment_number),
        invoice_id=format_invoice_id(txn.invoice_number),
        display_id=_build_display_id(txn),
        display_status=display_status(txn),
        seeker_photo_url=seeker_profile.profile_photo_url if seeker_profile else None,
        tax_label=txn.tax_label,
        tax_country=txn.tax_country,
        tax_jurisdiction=txn.tax_jurisdiction,
        stripe_fee_usd=as_float(txn.stripe_fee_usd),
        platform_fee_usd=txn.commission_usd,
        consultant_fee_usd=txn.advisor_payout_usd,
        advisor_gross_usd=as_float(txn.advisor_payout_usd),
        advisor_reversed_usd=as_float(txn.advisor_reversed_usd),
        net_amount_usd=advisor_earnings.advisor_net_earnings(txn),
    )


async def resend_receipt(session: AsyncSession, txn: Transaction, settings: Settings) -> None:
    if txn.status not in _INVOICE_ELIGIBLE_STATUSES or txn.invoice_number is None:
        raise AppError("Receipt is only available for a paid transaction", code="not_paid")
    booking = await session.get(Booking, txn.booking_id)
    if booking is None:
        raise NotFoundError("Booking not found")
    seeker = await session.get(User, booking.seeker_id)
    advisor = await session.get(User, booking.advisor_id)
    if seeker is None:
        raise NotFoundError("Seeker not found")
    email_service.schedule_email(
        email_service.send_payment_receipt_email(
            seeker.email,
            seeker.full_name or "",
            (advisor.full_name if advisor and advisor.full_name else "Advisor"),
            name=booking.name,
            amount_usd=txn.amount_usd,
            invoice_number=format_invoice_id(txn.invoice_number) or f"{txn.invoice_number:08d}",
            settings=settings,
        )
    )


async def list_events(session: AsyncSession, transaction_id: uuid.UUID) -> list[TransactionEvent]:
    result = await session.execute(
        select(TransactionEvent)
        .where(TransactionEvent.transaction_id == transaction_id)
        .order_by(TransactionEvent.occurred_at)
    )
    return list(result.scalars().all())
