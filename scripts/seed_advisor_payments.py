"""Seed Advisor Payments FE demo data for a single target advisor (PRD §3.10).

Creates several seekers + bookings + transactions on the target advisor's
bookings so these endpoints return real rows out of the box::

    GET  /api/v1/advisors/me/payments
    GET  /api/v1/advisors/me/earnings
    GET  /api/v1/advisors/me/payouts/preview?amount_usd=...
    POST /api/v1/advisors/me/payouts
    GET  /api/v1/payments/{id}/invoice?perspective=advisor

The mix covers every display status the FE renders:

  - succeeded / paid      (several — carry ``invoice_number``, drive earnings)
  - pending               (checkout created, not confirmed)
  - refunded              (full refund) + invoice
  - partially_refunded    (has invoice too)

Each transaction gets a full ``transaction_events`` history so the admin
timeline/logs views also work. Because succeeded payouts accrue and no payout
requests are seeded, ``available_balance_usd`` is comfortably > 0 for the
Request Payout button.

Run with::

    uv run python -m scripts.seed_advisor_payments
    ADVISOR_USER_ID=<uuid> uv run python -m scripts.seed_advisor_payments

Idempotent: re-running clears the seeded seekers/bookings/transactions first.
Seeker password: TestPass123!
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.security import hash_password
from app.db.session import async_session_factory, engine
from app.models.advisor_profile import AdvisorProfile
from app.models.booking import Booking, BookingStatus, PaymentStatus
from app.models.seeker_profile import SeekerProfile
from app.models.transaction import Transaction, TransactionStatus
from app.models.transaction_event import TransactionEvent, TransactionEventType
from app.models.user import User, UserRole, VerificationStatus
from app.services import booking_service

logger = get_logger(__name__)

PASSWORD = "TestPass123!"
EMAIL_PREFIX = "advisor.payments.seed."

# Target advisor whose /advisors/me/* endpoints the FE is testing.
DEFAULT_ADVISOR_USER_ID = uuid.UUID("da37a676-127b-48a3-8fde-707d7e9df438")

COMMISSION_RATE = 0.15
TAX_RATE = 0.08

_SUCCESS_STEPS: tuple[TransactionEventType, ...] = (
    TransactionEventType.authorized,
    TransactionEventType.completed,
    TransactionEventType.invoice_generated,
    TransactionEventType.receipt_sent,
)


def _happy_path_steps(start: datetime) -> list[tuple[TransactionEventType, datetime]]:
    steps: list[tuple[TransactionEventType, datetime]] = [
        (TransactionEventType.initiated, start),
    ]
    cursor = start
    for event_type in _SUCCESS_STEPS:
        cursor += timedelta(seconds=45)
        steps.append((event_type, cursor))
    steps.append((TransactionEventType.closed, cursor + timedelta(seconds=20)))
    return steps


def _refund_path_steps(start: datetime) -> list[tuple[TransactionEventType, datetime]]:
    steps = _happy_path_steps(start)
    refund_at = steps[-1][1] + timedelta(days=1, hours=2)
    steps.append((TransactionEventType.refunded, refund_at))
    steps.append((TransactionEventType.closed, refund_at + timedelta(seconds=15)))
    return steps


def _pending_steps(start: datetime) -> list[tuple[TransactionEventType, datetime]]:
    return [(TransactionEventType.initiated, start)]


async def _clear_prior(session: AsyncSession, advisor_id: uuid.UUID) -> int:
    """Remove previously seeded seekers and their bookings/transactions on this advisor."""
    users = (
        (await session.execute(select(User).where(User.email.like(f"{EMAIL_PREFIX}%"))))
        .scalars()
        .all()
    )
    ids = [u.id for u in users]
    booking_ids = (
        (
            await session.execute(
                select(Booking.id).where(
                    Booking.advisor_id == advisor_id,
                    Booking.seeker_id.in_(ids) if ids else Booking.seeker_id.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    if booking_ids:
        txn_ids = (
            (
                await session.execute(
                    select(Transaction.id).where(Transaction.booking_id.in_(booking_ids))
                )
            )
            .scalars()
            .all()
        )
        if txn_ids:
            await session.execute(
                delete(TransactionEvent).where(TransactionEvent.transaction_id.in_(txn_ids))
            )
        await session.execute(delete(Transaction).where(Transaction.booking_id.in_(booking_ids)))
        await session.execute(delete(Booking).where(Booking.id.in_(booking_ids)))
    if ids:
        await session.execute(delete(SeekerProfile).where(SeekerProfile.user_id.in_(ids)))
        await session.execute(delete(User).where(User.id.in_(ids)))
    await session.flush()
    return len(ids)


async def _make_seeker(
    session: AsyncSession, *, email: str, full_name: str, password_hash: str
) -> User:
    user = User(
        email=email,
        full_name=full_name,
        hashed_password=password_hash,
        role=UserRole.seeker,
        is_active=True,
        email_verified_at=datetime.now(UTC) - timedelta(days=30),
        verification_status=VerificationStatus.approved,
    )
    session.add(user)
    await session.flush()
    session.add(
        SeekerProfile(
            user_id=user.id,
            country_of_residence="CA",
            nationality="PK",
            intended_visa_type="study",
            intended_destination="CA",
        )
    )
    await session.flush()
    return user


async def _next_invoice_number(session: AsyncSession) -> int:
    max_invoice = await session.scalar(select(func.max(Transaction.invoice_number)))
    return (max_invoice or 3000) + 1


async def _create_txn(
    session: AsyncSession,
    *,
    seeker: User,
    advisor_id: uuid.UUID,
    when: datetime,
    amount_usd: float,
    service_type: str,
    status: TransactionStatus,
    payment_status: PaymentStatus,
    tag: str,
    refunded_amount_usd: float | None = None,
) -> Transaction:
    commission_usd = round(amount_usd * COMMISSION_RATE, 2)
    tax_usd = round(amount_usd * TAX_RATE, 2)
    advisor_payout_usd = round(amount_usd - commission_usd - tax_usd, 2)

    booking = Booking(
        seeker_id=seeker.id,
        advisor_id=advisor_id,
        appointment_number=await booking_service._next_appointment_number(session),
        service_type=service_type,
        duration_minutes=45,
        price_usd=amount_usd,
        scheduled_start=when + timedelta(days=3),
        scheduled_end=when + timedelta(days=3, minutes=45),
        status=(
            BookingStatus.pending
            if status == TransactionStatus.pending
            else BookingStatus.confirmed
        ),
        payment_status=payment_status,
        created_by=seeker.id,
    )
    session.add(booking)
    await session.flush()

    has_invoice = status in (
        TransactionStatus.succeeded,
        TransactionStatus.refunded,
        TransactionStatus.partially_refunded,
    )
    invoice_number = await _next_invoice_number(session) if has_invoice else None

    is_refunded = status in (TransactionStatus.refunded, TransactionStatus.partially_refunded)

    txn = Transaction(
        booking_id=booking.id,
        stripe_checkout_session_id=f"cs_ap_{tag}_{uuid.uuid4().hex[:10]}",
        stripe_payment_intent_id=(
            None if status == TransactionStatus.pending else f"pi_ap_{tag}_{uuid.uuid4().hex[:10]}"
        ),
        stripe_charge_id=(
            None
            if status in (TransactionStatus.pending, TransactionStatus.failed)
            else f"ch_ap_{tag}_{uuid.uuid4().hex[:10]}"
        ),
        amount_usd=amount_usd,
        commission_rate=COMMISSION_RATE,
        commission_usd=commission_usd,
        tax_rate=TAX_RATE,
        tax_usd=tax_usd,
        advisor_payout_usd=advisor_payout_usd,
        payment_method="card",
        status=status,
        invoice_number=invoice_number,
        refunded_at=(when + timedelta(days=1, hours=2)) if is_refunded else None,
        refunded_amount_usd=(
            refunded_amount_usd
            if refunded_amount_usd is not None
            else (amount_usd if status == TransactionStatus.refunded else None)
        ),
        refund_reason="Seed demo refund" if is_refunded else None,
        created_by=seeker.id,
    )
    txn.created_at = when
    session.add(txn)
    await session.flush()

    if status == TransactionStatus.pending:
        steps = _pending_steps(when)
    elif is_refunded:
        steps = _refund_path_steps(when)
    else:
        steps = _happy_path_steps(when)
    for event_type, occurred_at in steps:
        session.add(
            TransactionEvent(transaction_id=txn.id, event_type=event_type, occurred_at=occurred_at)
        )
    await session.flush()
    return txn


# (name, email-slug, amount, service_type, status, payment_status, tag)
_SEEKERS: tuple[tuple[str, str, float, str, TransactionStatus, PaymentStatus, str], ...] = (
    (
        "Amara Okafor",
        "amara",
        180.0,
        "immigration_specialist",
        TransactionStatus.succeeded,
        PaymentStatus.paid,
        "s1",
    ),
    (
        "Diego Fernandez",
        "diego",
        250.0,
        "immigration_specialist",
        TransactionStatus.succeeded,
        PaymentStatus.paid,
        "s2",
    ),
    (
        "Mei Lin",
        "mei",
        120.0,
        "career_coach",
        TransactionStatus.succeeded,
        PaymentStatus.paid,
        "s3",
    ),
    (
        "Yusuf Demir",
        "yusuf",
        95.0,
        "resume_writer",
        TransactionStatus.pending,
        PaymentStatus.unpaid,
        "p1",
    ),
    (
        "Priya Nair",
        "priya",
        150.0,
        "career_coach",
        TransactionStatus.pending,
        PaymentStatus.unpaid,
        "p2",
    ),
    (
        "Lucas Moreau",
        "lucas",
        200.0,
        "immigration_specialist",
        TransactionStatus.refunded,
        PaymentStatus.refunded,
        "r1",
    ),
    (
        "Sofia Rossi",
        "sofia",
        160.0,
        "recruiter",
        TransactionStatus.partially_refunded,
        PaymentStatus.refunded,
        "r2",
    ),
)


async def seed_advisor_payments(advisor_id: uuid.UUID) -> list[str]:
    lines: list[str] = []
    password_hash = hash_password(PASSWORD)
    now = datetime.now(UTC)

    async with async_session_factory() as session:
        advisor = await session.get(User, advisor_id)
        if advisor is None or advisor.role != UserRole.advisor:
            raise SystemExit(f"Target advisor {advisor_id} not found or is not an advisor")
        profile = (
            await session.execute(
                select(AdvisorProfile).where(AdvisorProfile.user_id == advisor_id)
            )
        ).scalar_one_or_none()
        if profile is None:
            raise SystemExit(f"Advisor {advisor_id} has no advisor_profile")

        cleared = await _clear_prior(session, advisor_id)
        lines.append(f"cleared_prior_seekers={cleared}")

        invoice_txn_id: uuid.UUID | None = None
        total_advisor_payout = 0.0
        days_ago = 2
        for name, slug, amount, service_type, status, pay_status, tag in _SEEKERS:
            seeker = await _make_seeker(
                session,
                email=f"{EMAIL_PREFIX}{slug}@globlejump.test",
                full_name=name,
                password_hash=password_hash,
            )
            when = now - timedelta(days=days_ago)
            days_ago += 3
            partial = (
                round(amount * 0.4, 2) if status == TransactionStatus.partially_refunded else None
            )
            txn = await _create_txn(
                session,
                seeker=seeker,
                advisor_id=advisor_id,
                when=when,
                amount_usd=amount,
                service_type=service_type,
                status=status,
                payment_status=pay_status,
                tag=tag,
                refunded_amount_usd=partial,
            )
            if status == TransactionStatus.succeeded:
                total_advisor_payout += txn.advisor_payout_usd
                if invoice_txn_id is None:
                    invoice_txn_id = txn.id
            lines.append(
                f"txn seeker={name!r} status={status.value} amount=${amount:.2f} "
                f"service={service_type} txn_id={txn.id}"
                + (f" invoice=INV-{txn.invoice_number:08d}" if txn.invoice_number else "")
            )

        await session.commit()

    lines.append(f"advisor_user_id={advisor_id}")
    balance = round(total_advisor_payout, 2)
    lines.append(f"available_balance_usd~={balance} (succeeded payouts, none requested)")
    lines.append(f"seeker_password={PASSWORD}")
    if invoice_txn_id:
        lines.append(f"invoice_url=/api/v1/payments/{invoice_txn_id}/invoice?perspective=advisor")
    return lines


async def main() -> None:
    raw = os.environ.get("ADVISOR_USER_ID")
    advisor_id = uuid.UUID(raw) if raw else DEFAULT_ADVISOR_USER_ID
    try:
        for line in await seed_advisor_payments(advisor_id):
            print(line)
        print()
        print("GET  /api/v1/advisors/me/payments")
        print("GET  /api/v1/advisors/me/earnings")
        print("GET  /api/v1/advisors/me/payouts/preview?amount_usd=50")
        print("POST /api/v1/advisors/me/payouts   {amount, method, note}")
        print("GET  /api/v1/payments/{id}/invoice?perspective=advisor")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
