"""Refund calculation and execution (EPIC 04, PAY-105).

Amounts always come from the transaction's stored split, never from the current
settings. The only policy read at refund time is the admin's "platform fee refund
behaviour", and only for an advisor cancellation.

Why two Stripe calls: on a destination charge ``reverse_transfer=True`` reverses the
transfer *proportionally* to the refunded amount. Refunding the advisor share of 85
out of 100 with that flag pulls back 85% of 85 and leaves the platform paying the
rest. Issuing the seeker refund with ``reverse_transfer=False`` and then an explicit
``Transfer.create_reversal`` for exactly the advisor share gives the document's
numbers: seeker +85, advisor -85, platform keeps its fee.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

import stripe
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import AppError, ConflictError
from app.models.booking import Booking, BookingRefundStatus, PaymentStatus
from app.models.notification import NotificationEntityType, NotificationType
from app.models.transaction import Transaction, TransactionStatus, TransferStatus
from app.models.transaction_event import TransactionEvent, TransactionEventType
from app.models.transaction_refund import RefundKind, RefundStatus, TransactionRefund
from app.models.user import User
from app.services import email_service, notification_service, stripe_config_service
from app.services.payment_config_service import PaymentConfig

log = structlog.get_logger()

_CENT = Decimal("0.01")
_PLATFORM_FAULT = (RefundKind.expiry, RefundKind.rejection)


def _money(value: object) -> Decimal:
    return Decimal(str(value or 0)).quantize(_CENT, rounding=ROUND_HALF_UP)


def _cents(value: Decimal) -> int:
    return int((value * 100).to_integral_value(rounding=ROUND_HALF_UP))


class ReversalUnavailable(Exception):
    """A reversal is due but the advisor's transfer cannot be found on Stripe."""


def _field(obj: object, key: str) -> object:
    """Read a field from a StripeObject or a plain dict."""
    if isinstance(obj, dict):
        return obj.get(key)
    try:
        return obj[key]  # type: ignore[index]
    except (KeyError, TypeError, AttributeError):
        return getattr(obj, key, None)


async def _ensure_transfer_id(txn: Transaction) -> str | None:
    """The transfer id captured at checkout, or recovered from the charge.

    ``_handle_checkout_completed`` is allowed to lose the PaymentIntent lookup (it logs
    ``webhook_pi_retrieve_failed`` and carries on), so a destination charge can sit with
    a NULL ``stripe_transfer_id`` even though the advisor was paid. The charge always
    knows its transfer; ask it before giving up.
    """
    if txn.stripe_transfer_id:
        return str(txn.stripe_transfer_id)
    if not txn.stripe_charge_id:
        return None
    charge = await stripe.Charge.retrieve_async(str(txn.stripe_charge_id))
    raw = _field(charge, "transfer")
    transfer_id = str(_field(raw, "id") or raw) if raw else None
    if transfer_id:
        txn.stripe_transfer_id = transfer_id
    return transfer_id


@dataclass(frozen=True, slots=True)
class RefundPlan:
    kind: str
    refund_to_seeker_usd: Decimal
    advisor_reversed_usd: Decimal
    platform_fee_refunded_usd: Decimal
    fee_policy_refunded: bool
    # What the seeker does not get back: tax, Stripe's fee and (per policy) the commission.
    retained_usd: Decimal = Decimal("0.00")


def _fee_total(txn: object) -> Decimal:
    """The platform commission on this payment: the one part of the seeker's money that
    the fee policy decides about. Tax and Stripe's fee are never on the table."""
    return _money(getattr(txn, "commission_usd", 0))


def refundable_total(txn: object) -> Decimal:
    """Everything a seeker can ever get back on this payment.

    Tax is never reversed and Stripe's processing fee is never reversed (QA 2026-10-08,
    section 24), so ``amount - tax - stripe_fee`` is the ceiling: commission plus the
    advisor's net. A payment is ``refunded`` when this much has gone back, and
    ``partially_refunded`` while the platform still holds its commission.
    """
    return _money(
        _money(txn.amount_usd)  # type: ignore[attr-defined]
        - _money(getattr(txn, "tax_usd", 0))
        - _money(getattr(txn, "stripe_fee_usd", 0))
    )


def _stripe_fee_taken(txn: object) -> Decimal:
    """The Stripe fee only counts as taken from the advisor once Stripe confirmed the
    reversal; if that recovery failed the advisor still holds the gross share."""
    if getattr(txn, "stripe_fee_reversal_id", None):
        return _money(getattr(txn, "stripe_fee_usd", 0))
    return Decimal(0)


def compute_refund(
    txn: object,
    kind: str | RefundKind,
    config: PaymentConfig,
) -> RefundPlan:
    """Pure calculation. ``txn`` only needs the stored split attributes.

    Only the system refunds: an advisor cancellation (share back, fee per policy) or a
    platform fault (rejection, expiry: everything back). Admin-initiated refunds were
    removed on 2026-10-07 as a business decision; their enum values remain only so
    historical ledger rows still load.
    """
    kind = RefundKind(kind)
    remaining_total = refundable_total(txn) - _money(getattr(txn, "refunded_amount_usd", 0))
    remaining_advisor = (
        _money(txn.advisor_payout_usd)  # type: ignore[attr-defined]
        - _stripe_fee_taken(txn)
        - _money(getattr(txn, "advisor_reversed_usd", 0))
    )
    remaining_fee = _fee_total(txn) - _money(getattr(txn, "platform_fee_refunded_usd", 0))
    remaining_advisor = max(Decimal(0), remaining_advisor)
    remaining_fee = max(Decimal(0), remaining_fee)
    if remaining_total <= 0:
        raise AppError("Nothing left to refund on this payment", code="not_refundable")

    policy_refunds_fee = config.platform_fee_refund_behavior == "refunded"

    if kind == RefundKind.advisor_cancel:
        advisor = remaining_advisor
        fee = remaining_fee if policy_refunds_fee else Decimal(0)
        seeker = advisor + fee
        fee_policy = policy_refunds_fee
    elif kind in _PLATFORM_FAULT:
        advisor, fee = remaining_advisor, remaining_fee
        seeker = advisor + fee
        fee_policy = True
    else:
        raise AppError("Refunds are issued by the system only", code="not_refundable")

    seeker = _money(seeker)
    if seeker <= 0:
        # e.g. a second advisor cancel under "retained": the advisor share is already
        # back and the commission is not on offer, so there is nothing to send.
        raise AppError("Nothing left to refund on this payment", code="not_refundable")
    if seeker > remaining_total:
        raise AppError(
            "Refund would exceed what is left of the payment", code="refund_amount_too_large"
        )
    return RefundPlan(
        kind=kind.value,
        refund_to_seeker_usd=seeker,
        advisor_reversed_usd=_money(advisor),
        platform_fee_refunded_usd=_money(fee),
        fee_policy_refunded=fee_policy,
        retained_usd=_money(_money(txn.amount_usd) - seeker),  # type: ignore[attr-defined]
    )


async def _log_event(session: AsyncSession, txn_id: uuid.UUID, event: TransactionEventType) -> None:
    session.add(
        TransactionEvent(transaction_id=txn_id, event_type=event, occurred_at=datetime.now(UTC))
    )
    await session.flush()


async def _fail(
    session: AsyncSession,
    row: TransactionRefund,
    txn: Transaction,
    exc: Exception,
) -> None:
    row.status = RefundStatus.failed
    row.last_error = str(exc)[:500]
    booking = await session.get(Booking, txn.booking_id)
    if booking is not None:
        booking.refund_status = BookingRefundStatus.failed
        session.add(booking)
    session.add(row)
    await _log_event(session, txn.id, TransactionEventType.refund_failed)
    await notification_service.notify_admins(
        session,
        type=NotificationType.refund_failed,
        title="Refund failed",
        body=(
            f"Refund of ${row.refund_to_seeker_usd} on transaction {txn.id} failed: "
            f"{row.last_error}"
        ),
        entity_type=NotificationEntityType.transaction,
        entity_id=txn.id,
    )
    await session.flush()
    log.error(
        "refund_failed", transaction_id=str(txn.id), refund_id=str(row.id), error=row.last_error
    )


async def execute_refund(
    session: AsyncSession,
    txn: Transaction,
    plan: RefundPlan,
    initiated_by: uuid.UUID | None,
    reason: str | None,
    settings: Settings,
) -> TransactionRefund:
    """Record, then call Stripe (refund, then reversal), then update the ledger."""
    keys = await stripe_config_service.effective_keys(session, settings)
    if not keys.secret_key:
        raise AppError("Payment processing is not configured", code="stripe_not_configured")
    stripe.api_key = keys.secret_key

    if txn.status not in (TransactionStatus.succeeded, TransactionStatus.partially_refunded):
        raise AppError(
            "Only succeeded or partially-refunded transactions can be refunded",
            code="not_refundable",
        )
    if not (txn.stripe_payment_intent_id or txn.stripe_charge_id):
        raise AppError("No Stripe charge found for this transaction", code="no_charge")
    pending = (
        await session.execute(
            select(TransactionRefund).where(
                TransactionRefund.transaction_id == txn.id,
                TransactionRefund.status == RefundStatus.pending,
            )
        )
    ).scalar_one_or_none()
    if pending is not None:
        raise ConflictError("A refund is already in progress", code="refund_in_progress")

    row = TransactionRefund(
        transaction_id=txn.id,
        kind=RefundKind(plan.kind),
        refund_to_seeker_usd=plan.refund_to_seeker_usd,
        advisor_reversed_usd=plan.advisor_reversed_usd,
        platform_fee_refunded_usd=plan.platform_fee_refunded_usd,
        fee_policy_refunded=plan.fee_policy_refunded,
        status=RefundStatus.pending,
        reason=reason,
        initiated_by=initiated_by,
    )
    session.add(row)
    await session.flush()
    await _log_event(session, txn.id, TransactionEventType.refund_requested)

    booking = await session.get(Booking, txn.booking_id)
    if booking is not None:
        booking.refund_status = BookingRefundStatus.pending
        session.add(booking)

    try:
        await _run_stripe_steps(txn, row)
    except (stripe.StripeError, ReversalUnavailable) as exc:
        await _fail(session, row, txn, exc)
        raise AppError("Refund could not be processed", code="refund_failed") from exc

    await _settle(session, txn, row, initiated_by=initiated_by, reason=reason, settings=settings)
    await session.refresh(row)
    log.info(
        "refund_executed",
        transaction_id=str(txn.id),
        refund_id=str(row.id),
        kind=plan.kind,
        seeker=str(plan.refund_to_seeker_usd),
        advisor_reversed=str(plan.advisor_reversed_usd),
        fee_refunded=str(plan.platform_fee_refunded_usd),
    )
    return row


async def _settle(
    session: AsyncSession,
    txn: Transaction,
    row: TransactionRefund,
    *,
    initiated_by: uuid.UUID | None,
    reason: str | None,
    settings: Settings,
) -> None:
    """Ledger, booking, notification and email once Stripe has both steps.

    Shared by the first attempt and by ``retry_refund`` so a refund that failed
    half-way lands in exactly the same end state once it succeeds.
    """
    row.last_error = None
    # Only a reversal Stripe confirmed goes into the books. The advisor's connected
    # balance is the one figure they can check for themselves, and it must agree.
    reversed_amount = _money(row.advisor_reversed_usd) if row.stripe_reversal_id else Decimal(0)
    row.advisor_reversed_usd = reversed_amount
    txn.refunded_amount_usd = float(
        _money(txn.refunded_amount_usd) + _money(row.refund_to_seeker_usd)
    )
    txn.advisor_reversed_usd = float(_money(txn.advisor_reversed_usd) + reversed_amount)
    txn.platform_fee_refunded_usd = float(
        _money(txn.platform_fee_refunded_usd) + _money(row.platform_fee_refunded_usd)
    )
    fully = _money(txn.refunded_amount_usd) >= refundable_total(txn)
    txn.status = TransactionStatus.refunded if fully else TransactionStatus.partially_refunded
    txn.refunded_at = datetime.now(UTC)
    txn.refunded_by = initiated_by
    txn.refund_reason = reason
    txn.updated_by = initiated_by
    if txn.transfer_status == TransferStatus.pending:
        # Legacy separate-charge row still in its hold window: nothing was transferred.
        txn.transfer_status = TransferStatus.cancelled
    session.add(txn)
    session.add(row)
    await _log_event(session, txn.id, TransactionEventType.refunded)
    if row.stripe_reversal_id:
        await _log_event(session, txn.id, TransactionEventType.transfer_reversed)
    if fully:
        await _log_event(session, txn.id, TransactionEventType.closed)

    booking = await session.get(Booking, txn.booking_id)
    if booking is not None:
        booking.payment_status = PaymentStatus.refunded
        booking.refund_status = BookingRefundStatus.refunded
        session.add(booking)
        amount = _money(row.refund_to_seeker_usd)
        await notification_service.notify(
            session,
            user_id=booking.seeker_id,
            type=NotificationType.booking_refund_issued,
            title="Refund issued",
            body=f"${amount} refunded for {booking.name}",
            entity_type=NotificationEntityType.transaction,
            entity_id=txn.id,
            actor_id=initiated_by,
        )
        seeker = await session.get(User, booking.seeker_id)
        if seeker is not None:
            email_service.schedule_email(
                email_service.send_refund_issued_email(
                    to=seeker.email,
                    full_name=seeker.full_name or seeker.email,
                    booking_name=booking.name,
                    amount_usd=float(amount),
                    settings=settings,
                )
            )
    await session.flush()


async def _run_stripe_steps(txn: Transaction, row: TransactionRefund) -> None:
    """The two calls, each skipped when its id is already recorded (resumable).

    The reversal is checked *before* the seeker refund is issued: a refund that cannot
    be completed must not half-run, and a reversal that never happened is never
    recorded. Silently skipping it (as before 2026-10-07) left the advisor's Stripe
    balance untouched while every internal figure said the money had come back.
    """
    if txn.transfer_status != TransferStatus.completed:
        # Legacy separate-charge row whose hold never released: the advisor never
        # received the share, so there is nothing to claw back and nothing to record.
        row.advisor_reversed_usd = Decimal(0)
    reversal_due = _money(row.advisor_reversed_usd) > 0 and row.stripe_reversal_id is None
    transfer_id: str | None = None
    if reversal_due:
        transfer_id = await _ensure_transfer_id(txn)
        if not transfer_id:
            raise ReversalUnavailable(
                "The advisor's transfer could not be found on Stripe, so the advisor share "
                "cannot be reversed; the seeker refund was not issued"
            )

    if row.stripe_refund_id is None:
        params: dict[str, object] = {
            "amount": _cents(_money(row.refund_to_seeker_usd)),
            "reverse_transfer": False,
            "refund_application_fee": False,
            "metadata": {
                "transaction_id": str(txn.id),
                "refund_id": str(row.id),
                "kind": row.kind.value,
            },
        }
        if txn.stripe_payment_intent_id:
            params["payment_intent"] = txn.stripe_payment_intent_id
        else:
            params["charge"] = txn.stripe_charge_id
        refund = await stripe.Refund.create_async(
            **params,  # type: ignore[arg-type]
            idempotency_key=f"refund_{row.id}",
        )
        row.stripe_refund_id = str(refund.id)
        row.status = RefundStatus.refunded

    if reversal_due and transfer_id:
        reversal = await stripe.Transfer.create_reversal_async(
            transfer_id,
            amount=_cents(_money(row.advisor_reversed_usd)),
            metadata={"transaction_id": str(txn.id), "refund_id": str(row.id)},
            idempotency_key=f"reversal_{row.id}",
        )
        row.stripe_reversal_id = str(reversal.id)
    row.status = RefundStatus.reversed


async def retry_refund(
    session: AsyncSession,
    refund_id: uuid.UUID,
    settings: Settings,
    *,
    initiated_by: uuid.UUID | None = None,
) -> TransactionRefund:
    """Re-run the missing Stripe step(s) of a failed refund with the same keys.

    Only ``failed`` rows are retried; a step Stripe already applied is skipped
    because its id is on the row. On success the ledger, booking, notification
    and email are applied exactly as on a first-time success.
    """
    row = await session.get(TransactionRefund, refund_id)
    if row is None:
        raise AppError("Refund not found", code="not_found")
    if row.status != RefundStatus.failed:
        raise ConflictError("Only failed refunds can be retried", code="refund_not_failed")
    txn = await session.get(Transaction, row.transaction_id)
    if txn is None:
        raise AppError("Transaction not found", code="not_found")
    keys = await stripe_config_service.effective_keys(session, settings)
    if not keys.secret_key:
        raise AppError("Payment processing is not configured", code="stripe_not_configured")
    stripe.api_key = keys.secret_key
    try:
        await _run_stripe_steps(txn, row)
    except (stripe.StripeError, ReversalUnavailable) as exc:
        await _fail(session, row, txn, exc)
        raise AppError("Refund could not be processed", code="refund_failed") from exc
    await _settle(
        session,
        txn,
        row,
        initiated_by=initiated_by or row.initiated_by,
        reason=row.reason,
        settings=settings,
    )
    await session.refresh(row)
    log.info("refund_retried", transaction_id=str(txn.id), refund_id=str(row.id))
    return row
